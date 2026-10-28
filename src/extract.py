"""抽取：预筛命中的文章分批送 claude -p（订阅额度），按 schema 抽事件，归一实体，写 events，再合并进 edges。"""
import argparse, json, os, re, shutil, subprocess, time

from common import (ROOT, SETTINGS, KINDS, connect, sync_entities, load_entities, Prefilter, log, now_iso,
                    resolve_entity, start_run, finish_run)
from confirm import upsert_edge, upsert_snapshot

SCHEMA = (ROOT / "schema" / "extraction.json").read_text(encoding="utf-8")

PROMPT_HEAD = """You are a financial analyst extracting MONEY-COMMITMENT events between AI-infrastructure companies
(hyperscalers, AI labs, neoclouds, chip/memory vendors, server makers, data-center developers, power producers, lenders and investors)
from SEC filings, press releases and news.

## Entity list (use these canonical ids)
{entities}
Pooled counterparties (use these ids when the counterparty is a market, not a named company):
BONDHOLDERS (public bond/notes buyers), PUBLIC_INVESTORS (public share / ATM / IPO buyers), PRIVATE_ROUND (unnamed round investors),
LENDER_SYNDICATE (unnamed bank group), PRIVATE_CREDIT (unnamed private-credit lenders), UNDISCLOSED (unnamed customer/counterparty).

## Kinds and direction
- order: from = buyer/payer of compute, colocation, chips, servers, power or capacity; to = seller/provider. (Cloud contracts, GPU purchases, PPAs.)
- investment: from = investor; to = investee. Includes equity stakes, funding rounds, acquisitions, warrants, JV equity,
  and participation in an equity offering ("X will invest $3B in Y's equity offering" -> investment X -> Y).
- debt: from = borrower; to = lender. Bonds -> BONDHOLDERS; unnamed banks -> LENDER_SYNDICATE; named bank -> that bank.
- lease: from = lessee (user of the GPUs / site); to = lessor or the financier that buys the equipment and leases it back.
- guarantee: from = guarantor / backstop provider; to = beneficiary whose obligation is guaranteed.
- vendor_financing: from = the vendor extending credit or financing to its customer; to = the customer.
- guidance: single-company figure (capex guidance, RPO/backlog, total debt, total compute commitments). from = the company, to = "".

## Rules
1. Extract only concrete commitments of money or capacity: a signed/announced contract, a financing, an investment, a lease, a guarantee, or a disclosed guidance figure. Skip stock moves, analyst views, product launches, partnerships with no money or capacity attached, and routine earnings numbers (revenue, EPS).
2. If one article covers several distinct commitments (e.g. a press release with an investment AND a chip deployment with stated size), output each as its own event. An intent "to expand deployments" with no amount or capacity is NOT an event.
3. Counterparty not in the list -> "NEW:<Official company name>". Never invent a counterparty.
4. amount_usd: total USD value as a number (e.g. $17.4 billion -> 17400000000). "up to $X" -> X and say "up to" in amount_text. Per-year figures: multiply by the term when the term is stated, else keep the per-year number and say so. Non-USD: convert approximately and keep the original in amount_text. Not stated -> null.
5. status: completed = closed / priced / funded / acquisition completed; signed = definitive agreement; announced = official announcement, not yet definitive; rumored = media citing sources, "in talks"; amended = explicitly changes an earlier deal (give the new total); cancelled = terminated or superseded.
6. confidence: 0.9+ for a filing or the company's own press release stating the figure; 0.7–0.85 for a reputable outlet's full article; <= 0.5 for [TITLE ONLY] articles — for those, extract only if the title itself states who, what and how much.
7. announced = the deal's announcement date (YYYY-MM-DD); use the article date if the article announces it.
8. quote = verbatim sentence (<= 250 chars) that states the commitment. title_zh ≤ 30 个汉字；summary_zh 一两句中文，说明内容、期限、条件、与已有交易的关系；title_en one line.
9. verdicts: exactly one per article id, relevant=false when no event was extracted, with a one-line reason.
10. Aggregates are guidance, not deals: "total compute commitments of $X", "total lease obligations", "capex of $X" -> kind=guidance
    (instrument compute_commitments / other / capex), never an order/lease to UNDISCLOSED.
11. A financing structure is ONE deal: an SPV that borrows from banks and buys chips to lease to a customer is two edges at most —
    debt SPV -> lenders, and lease customer -> SPV. Do not also emit vendor_financing / order variants of the same money.
12. xAI has been a SpaceX subsidiary since Feb 2026: for compute deals with xAI/SpaceX after that date use SPACEX as the entity.

## Articles
"""


def entity_block(entities):
    lines = []
    for e in entities:
        if e.get("auto_added") or e.get("pooled"):
            continue
        lines.append(f"- {e['id']} | {e['name']} | {e['grp']} | aka: {', '.join(e['aliases'][:4])}")
    return "\n".join(lines)


def article_block(a, max_chars):
    body = (a["text"] or "").strip()
    tag = "" if a["has_body"] else " [TITLE ONLY]"
    src = a["source_name"] + (f" / {a['publisher']}" if a["publisher"] else "")
    hdr = f"### article_id={a['id']}{tag}\nsource: {src} (tier {a['tier']}) | date: {a['published_at']} | url: {a['orig_url'] or a['url']}\ntitle: {a['title']}\n"
    return hdr + (body[:max_chars] + "\n" if body else "") + "\n"


def claude_bin():
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def call_claude(prompt, model, timeout):
    cmd = [claude_bin(), "-p", "--model", model, "--output-format", "json", "--json-schema", SCHEMA,
           "--tools", "", "--max-turns", "3"]
    t0 = time.time()
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {p.stderr[-500:]}")
    out = p.stdout[p.stdout.find("{"):]
    d = json.loads(out)
    so = d.get("structured_output")
    if not so:
        raise RuntimeError(f"no structured_output: subtype={d.get('subtype')} result={str(d.get('result'))[:300]}")
    return so, d.get("total_cost_usd"), ms


def _retry_count(note):
    m = re.search(r"retry:(\d+)", note or "")
    return int(m.group(1)) if m else 0


def process_batch(con, arts, entities, pf, run_id, stats):
    ids = [a["id"] for a in arts]
    prompt = PROMPT_HEAD.format(entities=entity_block(entities)) + "".join(article_block(a, SETTINGS["max_article_chars"]) for a in arts)
    try:
        out, cost, ms = call_claude(prompt, SETTINGS["claude_model"], SETTINGS["claude_timeout_sec"])
    except Exception as ex:
        log.error("批次失败 %s: %s", ids, ex)
        con.execute("INSERT INTO extraction_log(run_id, article_ids, model, ok, error) VALUES(?,?,?,0,?)",
                    (run_id, json.dumps(ids), SETTINGS["claude_model"], str(ex)[:1000]))
        for a in arts:
            n = _retry_count(a["note"]) + 1
            st = "error" if n >= 3 else "new"
            con.execute("UPDATE articles SET status=?, note=? WHERE id=?", (st, f"retry:{n} {str(ex)[:200]}", a["id"]))
        con.commit()
        return False
    con.execute("INSERT INTO extraction_log(run_id, article_ids, model, raw_output, cost_usd, duration_ms) VALUES(?,?,?,?,?,?)",
                (run_id, json.dumps(ids), SETTINGS["claude_model"], json.dumps(out, ensure_ascii=False), cost, ms))
    verdicts = {v["article_id"]: v for v in out.get("verdicts", [])}
    events = out.get("events", [])
    if not verdicts and not events:
        # 整批空返回：当作失败重试，避免把有正文的文章吞掉
        log.warning("批次空返回 %s，重试", ids)
        for a in arts:
            n = _retry_count(a["note"]) + 1
            con.execute("UPDATE articles SET status=?, note=? WHERE id=?", ("skipped" if n >= 2 else "new", f"retry:{n} empty output", a["id"]))
        con.commit()
        return True
    by_art = {a["id"]: a for a in arts}
    cache, extracted = {}, set()
    for ev in events:
        aid = ev.get("article_id")
        if aid not in by_art:
            continue
        a = by_art[aid]
        kind = ev.get("kind")
        f = resolve_entity(con, pf, ev.get("from_entity"), cache)
        if not f:
            continue
        if kind == "guidance":
            upsert_snapshot(con, ev, f, aid)
            stats["snapshots"] += 1
            extracted.add(aid)
            continue
        if kind not in KINDS:
            continue
        t = resolve_entity(con, pf, ev.get("to_entity"), cache)
        if not t or t == f:
            continue
        if not a["has_body"]:
            ev["confidence"] = min(float(ev.get("confidence") or 0.5), 0.5)
        cur = con.execute("""INSERT INTO events(article_id, kind, from_entity, to_entity, amount_usd, amount_text, instrument, term,
                             capacity, announced, maturity_date, period, status, confidence, title_zh, title_en, summary_zh, quote)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (aid, kind, f, t, ev.get("amount_usd"), ev.get("amount_text"), ev.get("instrument"), ev.get("term"),
                           ev.get("capacity"), ev.get("announced") or a["published_at"], ev.get("maturity_date"), ev.get("period"),
                           ev.get("status"), ev.get("confidence"), ev.get("title_zh"), ev.get("title_en"), ev.get("summary_zh"), ev.get("quote")))
        ev.setdefault("announced", a["published_at"])
        src = dict(url=a["orig_url"] or a["url"], title=a["title"], publisher=a["publisher"], date=a["published_at"],
                   tier=a["tier"], quote=ev.get("quote"), article_id=aid)
        before = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        eid = upsert_edge(con, ev, f, t, src)
        after = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        con.execute("UPDATE events SET edge_id=? WHERE id=?", (eid, cur.lastrowid))
        stats["edges_new" if after > before else "edges_updated"] += 1
        extracted.add(aid)
        log.info("%s #%d %s %s→%s %s | %s", "新边" if after > before else "更新", eid, kind, f, t, ev.get("amount_text") or "?", ev.get("title_zh"))
    for a in arts:
        st = "extracted" if a["id"] in extracted else "skipped"
        con.execute("UPDATE articles SET status=?, note=? WHERE id=?", (st, (verdicts.get(a["id"]) or {}).get("reason"), a["id"]))
    stats["articles"] += len(arts)
    con.commit()
    log.info("批次 %d 篇，抽出 %d 篇，%.0f ms，估算 $%.3f（订阅额度内）", len(arts), len(extracted), ms, cost or 0)
    return True


def extract(con, max_articles=None):
    run_id = start_run(con, "extract")
    entities = load_entities(con)
    pf = Prefilter(entities)
    limit = max_articles or SETTINGS["extract_max_per_run"]
    arts = con.execute("""SELECT * FROM articles WHERE status='new' AND prefilter_hit=1
                          ORDER BY tier ASC, has_body DESC, published_at DESC LIMIT ?""", (limit,)).fetchall()
    stats = {"articles": 0, "edges_new": 0, "edges_updated": 0, "snapshots": 0}
    if not arts:
        log.info("没有待抽取文章")
        finish_run(con, run_id, stats)
        return stats
    bs = SETTINGS["extract_batch_size"]
    log.info("待抽取 %d 篇，每批 %d", len(arts), bs)
    for i in range(0, len(arts), bs):
        process_batch(con, arts[i:i + bs], entities, pf, run_id, stats)
    finish_run(con, run_id, stats)
    log.info("抽取完成：%s", stats)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int)
    a = ap.parse_args()
    con = connect()
    sync_entities(con)
    extract(con, a.max)
