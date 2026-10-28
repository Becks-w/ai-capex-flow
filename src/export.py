"""导出 docs/data/graph.json（页面读取）+ 每日存档 docs/data/archive/YYYY-MM-DD.json。"""
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from common import ROOT, SETTINGS, KINDS, KIND_ZH, connect, load_yaml, now_z, log
from confirm import apply_overrides, recompute_all

SITE = ROOT / SETTINGS.get("site_dir", "docs")


def counted(e):
    return e["tier"] in ("confirmed", "reported") and e["status"] != "cancelled" and not e["superseded_by"] and e["review"] != "deleted"


def export(con):
    apply_overrides(con)
    recompute_all(con)
    cfg = load_yaml("entities.yaml")
    groups = cfg["groups"]
    ents = {r["id"]: dict(r) for r in con.execute("SELECT * FROM entities")}
    edges = [dict(r) for r in con.execute("SELECT * FROM edges WHERE review!='deleted' ORDER BY COALESCE(amount_usd,0) DESC, id")]
    srcs = defaultdict(list)
    for s in con.execute("SELECT * FROM edge_sources ORDER BY tier, date DESC"):
        srcs[s["edge_id"]].append({"url": s["url"], "title": s["title"], "publisher": s["publisher"], "date": s["date"], "tier": s["tier"]})
    keep_since = (datetime.now(timezone.utc) - timedelta(days=SETTINGS["candidate_keep_days"])).strftime("%Y-%m-%d")

    stats_in = defaultdict(lambda: defaultdict(float)); stats_out = defaultdict(lambda: defaultdict(float))
    cnt_in = defaultdict(lambda: defaultdict(int)); cnt_out = defaultdict(lambda: defaultdict(int))
    cand_cnt = defaultdict(int)
    out_edges, out_cands = [], []
    for e in edges:
        f, t = e["from_entity"], e["to_entity"]
        quarantined = ents.get(f, {}).get("auto_added") or ents.get(t, {}).get("auto_added")
        row = {k: e[k] for k in ("id", "kind", "from_entity", "to_entity", "amount_usd", "amount_text", "instrument", "term", "capacity",
                                 "announced", "maturity_date", "status", "tier", "confidence", "title_zh", "title_en", "summary_zh",
                                 "first_seen", "last_seen", "superseded_by", "seed", "review")}
        row["sources"] = srcs.get(e["id"], [])
        row["kind_zh"] = KIND_ZH.get(e["kind"], e["kind"])
        if counted(e) and not quarantined:
            out_edges.append(row)
            v = e["amount_usd"] or 0
            stats_out[f][e["kind"]] += v; stats_in[t][e["kind"]] += v
            cnt_out[f][e["kind"]] += 1; cnt_in[t][e["kind"]] += 1
        elif (e["tier"] == "candidate" or quarantined) and e["status"] != "cancelled" and (e["last_seen"] or "")[:10] >= keep_since:
            row["quarantined"] = bool(quarantined)
            out_cands.append(row)
            cand_cnt[f] += 1; cand_cnt[t] += 1
        elif e["status"] == "cancelled" or e["superseded_by"]:
            row["cancelled"] = True
            out_edges.append(row)      # 保留显示但不计入总额

    out_ents = []
    for eid, en in ents.items():
        if en["auto_added"]:
            continue
        deg = sum(cnt_in[eid].values()) + sum(cnt_out[eid].values())
        if deg == 0 and cand_cnt[eid] == 0 and en["pooled"]:
            continue
        out_ents.append({
            "id": eid, "name": en["name"], "name_zh": en["name_zh"] or en["name"], "group": en["grp"],
            "group_zh": groups.get(en["grp"], {}).get("zh", en["grp"]), "ticker": en["ticker"], "country": en["country"],
            "pooled": bool(en["pooled"]),
            "stats": {"inflow_usd": {k: stats_in[eid][k] for k in KINDS}, "outflow_usd": {k: stats_out[eid][k] for k in KINDS},
                      "inflow_count": {k: cnt_in[eid][k] for k in KINDS}, "outflow_count": {k: cnt_out[eid][k] for k in KINDS},
                      "inflow_total_usd": sum(stats_in[eid].values()), "outflow_total_usd": sum(stats_out[eid].values()),
                      "degree": deg, "candidates": cand_cnt[eid]}})
    snaps = [dict(r) for r in con.execute("SELECT entity_id, metric, period, value_usd, value_text, summary_zh, article_id, created_at FROM snapshots ORDER BY created_at DESC")]

    by_kind = {}
    for k in KINDS:
        rows = [e for e in out_edges if e["kind"] == k and not e.get("cancelled")]
        by_kind[k] = {"zh": KIND_ZH[k], "count": len(rows), "usd": sum(e["amount_usd"] or 0 for e in rows),
                      "undisclosed": sum(1 for e in rows if not e["amount_usd"])}
    news = [dict(r) for r in con.execute("""SELECT id, tier, source_name, publisher, title, COALESCE(orig_url, url) AS url, published_at, status, entity_hits
                                             FROM articles WHERE tier IN (2,3) AND prefilter_hit=1 ORDER BY published_at DESC, fetched_at DESC LIMIT ?""",
                                          (SETTINGS["news_feed_items"],))]
    filings = [dict(r) for r in con.execute("""SELECT id, source_name, publisher, title, url, published_at, edgar_form, edgar_items, edgar_entity, status, entity_hits
                                                FROM articles WHERE tier=1 ORDER BY published_at DESC, fetched_at DESC LIMIT ?""",
                                             (SETTINGS["filings_feed_items"],))]
    for a in news + filings:
        a["entity_hits"] = json.loads(a["entity_hits"] or "[]")
    sources = {r["name"]: dict(r) for r in con.execute("SELECT * FROM source_status")}
    runs = [dict(r) for r in con.execute("SELECT stage, started_at, finished_at, stats, ok, error FROM runs ORDER BY id DESC LIMIT 12")]
    art_counts = dict(con.execute("SELECT status, COUNT(*) FROM articles GROUP BY status").fetchall())

    doc = {
        "generated_at": now_z(),
        "monitor": {"sources": sources, "recent_runs": runs, "article_counts": art_counts,
                    "pending_extraction": con.execute("SELECT COUNT(*) FROM articles WHERE status='new' AND prefilter_hit=1").fetchone()[0]},
        "groups": groups,
        "entities": out_ents,
        "edges": out_edges,
        "candidates": out_cands,
        "snapshots": snaps,
        "news": news,
        "filings": filings,
        "stats": {"entities": len(out_ents), "edges": len([e for e in out_edges if not e.get("cancelled")]), "candidates": len(out_cands),
                  "by_kind": by_kind, "total_usd": sum(v["usd"] for v in by_kind.values()),
                  "latest_announced": max((e["announced"] or "" for e in out_edges), default=None)},
        "notes": {"zh": "总额只统计有一手来源（SEC 文件 / 公司公告）或媒体正文核实的交易，已取消或被取代的不计；机器从标题抽出的候选单独列出。每笔只计一次，供应链不抵消。仅作描述，不构成投资建议。"},
    }
    (SITE / "data" / "archive").mkdir(parents=True, exist_ok=True)
    blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    (SITE / "data" / "graph.json").write_text(blob, encoding="utf-8")
    (SITE / "data" / "archive" / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".json")).write_text(blob, encoding="utf-8")
    log.info("graph.json：%d 实体，%d 边，%d 候选，%d 快照，%d 新闻，%d 文件；总额 $%.0fB",
             len(out_ents), doc["stats"]["edges"], len(out_cands), len(snaps), len(news), len(filings), doc["stats"]["total_usd"] / 1e9)
    return doc


if __name__ == "__main__":
    export(connect())
