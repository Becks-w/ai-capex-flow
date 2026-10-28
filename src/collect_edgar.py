"""EDGAR 采集（tier 1）：
  A. submissions API：名单内有 ticker 的实体，拉 8-K / 6-K / 424B / 10-Q / 10-K，抓主文件 + EX-99。
  B. 全文检索 API（efts）：按关键词扫全部申报人，补名单外的对手方（如数据中心 SPV、地区银行）。
"""
import json, re, time
from urllib.parse import quote

from common import (SETTINGS, ROOT, connect, sync_entities, load_entities, Prefilter, html_to_text, http_get,
                    insert_article, days_ago, log, set_source_status, start_run, finish_run)

SEC_HDR = {"User-Agent": SETTINGS["sec_user_agent"], "Accept-Encoding": "gzip, deflate"}
CIK_CACHE = ROOT / "data" / "cik_cache.json"
SLEEP = 0.12


def sec_get(url, **kw):
    time.sleep(SLEEP)
    return http_get(url, headers=SEC_HDR, **kw)


def resolve_ciks(entities):
    cache = {}
    if CIK_CACHE.exists() and time.time() - CIK_CACHE.stat().st_mtime < 7 * 86400:
        cache = json.loads(CIK_CACHE.read_text())
    missing = [e["ticker"] for e in entities if e.get("ticker") and e["ticker"] not in cache]
    if missing:
        r = sec_get("https://www.sec.gov/files/company_tickers.json")
        if r is not None:
            m = {v["ticker"]: v["cik_str"] for v in r.json().values()}
            for t in missing:
                if t in m:
                    cache[t] = m[t]
            CIK_CACHE.write_text(json.dumps(cache))
    return cache


def filing_text(cik, acc_nodash, primary_doc, form):
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"
    docs = [primary_doc]
    idx = sec_get(f"{base}/index.json")
    if idx is not None:
        try:
            for it in idx.json()["directory"]["item"]:
                n = it["name"]
                if re.search(r"ex[-_]?99|ex[-_]?10", n, re.I) and n.lower().endswith((".htm", ".html")) and n not in docs:
                    docs.append(n)
        except Exception as ex:
            log.warning("index.json %s: %s", acc_nodash, ex)
    parts = []
    limit = SETTINGS["max_article_chars"]
    for d in docs[:4]:
        r = sec_get(f"{base}/{d}")
        if r is None:
            continue
        txt = html_to_text(r.text)
        if form.startswith("8-K") and d == primary_doc:
            m = re.search(r"\bItem\s+\d\.\d\d", txt)
            if m:
                txt = txt[m.start():]
        if form in ("10-Q", "10-K"):
            # 只保留和承诺相关的段落：RPO / 商定义务 / 资本开支 / 租赁 / 债务
            keep = []
            for para in txt.split("\n"):
                if re.search(r"remaining performance obligation|purchase commitment|capital expenditure|lease commitment|"
                             r"take-or-pay|senior notes|credit facility|term loan|gigawatt|data center", para, re.I):
                    keep.append(para.strip())
            txt = "\n".join(keep)
        parts.append(f"=== {d} ===\n" + txt[: limit])
    return "\n\n".join(parts)[: limit * 2], f"{base}/{primary_doc}"


def collect_submissions(con, lookback_days=None):
    ents = load_entities(con)
    pf = Prefilter(ents)
    ciks = resolve_ciks(ents)
    since = days_ago(lookback_days or SETTINGS["edgar_lookback_days"])
    forms = set(SETTINGS["edgar_forms"])
    want_items = set(SETTINGS["edgar_8k_items"])
    always = set(SETTINGS["edgar_always_items"])
    new = fetched = 0
    for e in ents:
        t = e.get("ticker")
        if not t or t not in ciks:
            continue
        if e.get("grp") == "financier":
            continue        # 银行 / 私募自己的融资文件（424B2 结构性票据等）是噪音；它们的 AI 放贷在借款人文件或新闻里出现
        cik = int(ciks[t])
        con.execute("UPDATE entities SET cik=? WHERE id=?", (cik, e["id"]))
        con.commit()
        r = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if r is None:
            continue
        rec = r.json()["filings"]["recent"]
        for i in range(len(rec["form"])):
            if rec["filingDate"][i] < since:
                break
            form = rec["form"][i]
            if form not in forms:
                continue
            items = {x.strip() for x in (rec["items"][i] or "").split(",") if x.strip()}
            if form.startswith("8-K") and not (items & want_items):
                continue
            acc = rec["accessionNumber"][i]
            if con.execute("SELECT 1 FROM articles WHERE id=?", (acc,)).fetchone():
                continue
            fetched += 1
            text, url = filing_text(cik, acc.replace("-", ""), rec["primaryDocument"][i], form)
            hits, score, ok = pf.run(text)
            if e["id"] not in hits:
                hits.append(e["id"])
            hit = 1 if (items & always) or ok else 0
            if form in ("10-Q", "10-K") and len(text) < 300:
                hit = 0
            title = f"{e['name']} {form}" + (f" ({', '.join(sorted(items))})" if items else "") + f" {rec['filingDate'][i]}"
            row = dict(id=acc, tier=1, source_kind="edgar", source_name="EDGAR", url=url, publisher="SEC EDGAR",
                       title=title, published_at=rec["filingDate"][i], text=text, has_body=1,
                       entity_hits=json.dumps(hits), score=score, prefilter_hit=hit,
                       edgar_form=form, edgar_items=",".join(sorted(items)) or None, edgar_entity=e["id"])
            if insert_article(con, row):
                new += 1
                log.info("EDGAR %s hit=%s score=%s", title, hit, score)
            con.commit()        # 每条都提交，不在网络请求期间持有写锁
    set_source_status(con, "edgar_submissions", 1, True, fetched, new)
    log.info("EDGAR submissions：新增 %d", new)
    return new


def collect_fulltext(con, lookback_days=None):
    """EDGAR 全文检索：关键词命中的 8-K / 424B / 6-K，不限名单。"""
    ents = load_entities(con)
    pf = Prefilter(ents)
    since = days_ago(lookback_days or SETTINGS["edgar_lookback_days"])
    until = days_ago(-1)
    new = fetched = 0
    err = None
    for q in SETTINGS.get("edgar_fulltext_queries", []):
        url = (f"https://efts.sec.gov/LATEST/search-index?q={quote(q)}&dateRange=custom&startdt={since}&enddt={until}"
               f"&forms=8-K,6-K,424B5,424B2,424B3")
        r = sec_get(url)
        if r is None:
            err = f"query failed: {q[:40]}"
            continue
        try:
            hits_ = r.json()["hits"]["hits"]
        except Exception as ex:
            err = str(ex)[:100]
            continue
        for h in hits_[:40]:
            s = h["_source"]
            acc_doc = h["_id"]                     # "0001193125-26-434271:d148323d8k.htm"
            acc, doc = acc_doc.split(":", 1)
            if con.execute("SELECT 1 FROM articles WHERE id=?", (acc,)).fetchone():
                continue
            ciks = s.get("ciks") or []
            if not ciks:
                continue
            cik = int(ciks[0])
            fetched += 1
            form = s.get("form") or s.get("file_type") or "8-K"
            text, url_ = filing_text(cik, acc.replace("-", ""), doc, form)
            ehits, score, ok = pf.run(text)
            if not ehits:
                continue                           # 名单实体都没出现，跳过
            names = s.get("display_names") or []
            company = re.sub(r"\s*\(.*$", "", names[0]) if names else f"CIK {cik}"
            title = f"{company} {form} {s.get('file_date')}（全文检索：{q[:30]}）"
            row = dict(id=acc, tier=1, source_kind="edgar_fts", source_name="EDGAR 全文检索", url=url_, publisher="SEC EDGAR",
                       title=title, published_at=s.get("file_date"), text=text, has_body=1,
                       entity_hits=json.dumps(ehits), score=score, prefilter_hit=1 if ok else 0,
                       edgar_form=form, edgar_items=None, edgar_entity=None)
            if insert_article(con, row):
                new += 1
                log.info("EDGAR-FTS %s hits=%s", title, ehits)
            con.commit()
    set_source_status(con, "edgar_fulltext", 1, err is None, fetched, new, err)
    log.info("EDGAR 全文检索：新增 %d", new)
    return new


def collect(con, lookback_days=None):
    run = start_run(con, "collect_edgar")
    try:
        a = collect_submissions(con, lookback_days)
        b = collect_fulltext(con, lookback_days)
        finish_run(con, run, {"submissions_new": a, "fulltext_new": b})
        return a + b
    except Exception as ex:
        finish_run(con, run, {}, False, str(ex)[:500])
        raise


if __name__ == "__main__":
    import sys
    con = connect()
    sync_entities(con)
    collect(con, int(sys.argv[1]) if len(sys.argv) > 1 else None)
