"""一次性历史回填（债务）：对名单内发行人拉 2025-01-01 起的 424B2 / 424B5（发债说明书补充）、
8-K item 2.03（新增直接财务义务）、6-K（外国发行人，按债务关键词预筛），写入 articles 供抽取。
用法：python3 src/backfill_debt.py [--since 2025-01-01] [--tickers GOOGL,AMZN,...]
"""
import argparse, json, re
from common import connect, sync_entities, load_entities, Prefilter, log, insert_article, set_source_status
from collect_edgar import resolve_ciks, filing_text, sec_get

DEFAULT_TICKERS = ["GOOGL", "AMZN", "META", "NVDA", "ORCL", "MSFT", "CRWV", "NBIS", "IREN", "CORZ", "WULF", "CIFR",
                   "APLD", "HUT", "GLXY", "BTDR", "RIOT", "MRVL", "SMCI", "EQIX", "DLR"]
FORMS = {"424B2", "424B5", "8-K", "8-K/A", "6-K"}
DEBT_RE = re.compile(r"senior notes|convertible (senior )?notes|notes offering|pricing of|credit facility|term loan|"
                     r"delayed draw|debt financing|bond|aggregate principal amount|indenture", re.I)


def all_filings(cik):
    """submissions API 的 recent 只有最近 1000 条，旧的在 filings.files 里分页。"""
    r = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if r is None:
        return []
    d = r.json()
    recs = [d["filings"]["recent"]]
    for f in d["filings"].get("files", []):
        rr = sec_get(f"https://data.sec.gov/submissions/{f['name']}")
        if rr is not None:
            recs.append(rr.json())
    rows = []
    for rec in recs:
        for i in range(len(rec["form"])):
            rows.append(dict(form=rec["form"][i], date=rec["filingDate"][i], acc=rec["accessionNumber"][i],
                             doc=rec["primaryDocument"][i], items=rec["items"][i] or ""))
    return rows


def run(since, tickers):
    con = connect(); sync_entities(con)
    ents = load_entities(con); pf = Prefilter(ents)
    ciks = resolve_ciks(ents)
    by_t = {e["ticker"]: e for e in ents if e.get("ticker")}
    new = fetched = 0
    for t in tickers:
        e = by_t.get(t)
        if not e or t not in ciks:
            log.warning("跳过 %s：无实体或 CIK", t); continue
        cik = int(ciks[t])
        rows = [r for r in all_filings(cik) if r["date"] >= since and r["form"] in FORMS]
        log.info("%s：%d 份候选文件", t, len(rows))
        for r in rows:
            items = {x.strip() for x in r["items"].split(",") if x.strip()}
            if r["form"].startswith("8-K") and "2.03" not in items:
                continue
            if con.execute("SELECT 1 FROM articles WHERE id=?", (r["acc"],)).fetchone():
                continue
            fetched += 1
            text, url = filing_text(cik, r["acc"].replace("-", ""), r["doc"], r["form"])
            if r["form"] == "6-K" and not DEBT_RE.search(text[:6000]):
                continue
            hits, score, _ = pf.run(text)
            if e["id"] not in hits:
                hits.append(e["id"])
            title = f"{e['name']} {r['form']}" + (f" ({', '.join(sorted(items))})" if items else "") + f" {r['date']}"
            row = dict(id=r["acc"], tier=1, source_kind="edgar", source_name="EDGAR backfill", url=url, publisher="SEC EDGAR",
                       title=title, published_at=r["date"], text=text, has_body=1, entity_hits=json.dumps(hits),
                       score=score, prefilter_hit=1, edgar_form=r["form"], edgar_items=",".join(sorted(items)) or None,
                       edgar_entity=e["id"])
            if insert_article(con, row):
                new += 1; log.info("回填 %s", title)
            con.commit()
    set_source_status(con, "edgar_backfill", 1, True, fetched, new)
    log.info("回填完成：抓取 %d，新增 %d", fetched, new)
    return new


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    a = ap.parse_args()
    run(a.since, [x.strip() for x in a.tickers.split(",") if x.strip()])
