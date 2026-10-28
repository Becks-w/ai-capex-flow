"""合并、可信度分级、取代、人工 overrides。
边的 tier：confirmed（有 tier 1/2 来源）> reported（媒体正文且置信 ≥ 阈值）> candidate（仅标题 / 低置信）。
"""
import json
from datetime import datetime, timedelta, timezone

from common import SETTINGS, KINDS, load_yaml, log, now_iso

STATUS_RANK = {"rumored": 0, "announced": 1, "signed": 2, "completed": 3, "amended": 3, "cancelled": 4}


def _amount_ok(a, b, tol):
    if a and b:
        return abs(a - b) / max(a, b) <= tol
    return True


def find_edge(con, kind, f, t, amount, announced=None):
    tol = SETTINGS["merge_amount_tolerance"]
    rows = con.execute("""SELECT * FROM edges WHERE kind=? AND from_entity=? AND to_entity=? AND review!='deleted'
                          ORDER BY last_seen DESC""", (kind, f, t)).fetchall()
    win = SETTINGS["merge_window_days"]
    for r in rows:
        if not _amount_ok(amount, r["amount_usd"], tol):
            continue
        if announced and r["announced"]:
            try:
                d = abs((datetime.fromisoformat(announced[:10]) - datetime.fromisoformat(r["announced"][:10])).days)
                if d > win:
                    continue
            except ValueError:
                pass
        return r
    return None


def add_source(con, edge_id, url, title, publisher, date, tier, quote=None, article_id=None):
    if not url:
        return
    con.execute("""INSERT OR IGNORE INTO edge_sources(edge_id, article_id, url, title, publisher, date, tier, quote)
                   VALUES(?,?,?,?,?,?,?,?)""", (edge_id, article_id, url, (title or "")[:300], publisher, date, tier, (quote or "")[:300]))


def recompute_tier(con, edge_id):
    e = con.execute("SELECT * FROM edges WHERE id=?", (edge_id,)).fetchone()
    srcs = con.execute("SELECT * FROM edge_sources WHERE edge_id=?", (edge_id,)).fetchall()
    tiers = [s["tier"] for s in srcs]
    body_srcs = 0
    for s in srcs:
        if s["article_id"]:
            a = con.execute("SELECT has_body FROM articles WHERE id=?", (s["article_id"],)).fetchone()
            if a and a["has_body"]:
                body_srcs += 1
        else:
            body_srcs += 1                      # 种子来源视为有正文（人工核实）
    pubs = {(s["publisher"] or s["url"].split("/")[2]) for s in srcs}
    if any(t <= 2 for t in tiers):
        tier = "confirmed"
    elif body_srcs >= 1 and (e["confidence"] >= SETTINGS["reported_min_confidence"] or len(pubs) >= 2):
        tier = "reported"
    else:
        tier = "candidate"
    if e["review"] == "confirmed":
        tier = "confirmed"
    con.execute("UPDATE edges SET tier=? WHERE id=?", (tier, edge_id))
    return tier


def upsert_edge(con, ev, f, t, source, seed=0):
    """ev: dict（模型事件或种子）；source: dict(url,title,publisher,date,tier,quote,article_id)。返回 edge_id。"""
    kind = ev["kind"]
    amount = ev.get("amount_usd") or None
    ex = find_edge(con, kind, f, t, amount, ev.get("announced"))
    st = ev.get("status") or "announced"
    conf = float(ev.get("confidence") or (0.95 if seed else 0.5))
    if ex:
        sets, vals = ["last_seen=?"], [now_iso()]
        if amount and (not ex["amount_usd"] or st == "amended" or (seed and not ex["seed"])):
            sets += ["amount_usd=?", "amount_text=?"]; vals += [amount, ev.get("amount_text")]
        for col in ("instrument", "term", "capacity", "announced", "maturity_date", "title_zh", "title_en", "summary_zh"):
            if ev.get(col) and (not ex[col] or seed):
                sets.append(f"{col}=?"); vals.append(ev[col])
        if STATUS_RANK.get(st, 0) > STATUS_RANK.get(ex["status"], 0):
            sets.append("status=?"); vals.append(st)
        if conf > (ex["confidence"] or 0):
            sets.append("confidence=?"); vals.append(conf)
            if ev.get("summary_zh"):
                sets.append("summary_zh=?"); vals.append(ev["summary_zh"])
        if seed:
            sets.append("seed=1")
        vals.append(ex["id"])
        con.execute(f"UPDATE edges SET {', '.join(sets)} WHERE id=?", vals)
        eid = ex["id"]
    else:
        cur = con.execute("""INSERT INTO edges(kind, from_entity, to_entity, amount_usd, amount_text, instrument, term, capacity,
                             announced, maturity_date, status, confidence, title_zh, title_en, summary_zh, seed)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (kind, f, t, amount, ev.get("amount_text"), ev.get("instrument"), ev.get("term"), ev.get("capacity"),
                           ev.get("announced"), ev.get("maturity_date"), st, conf, ev.get("title_zh"), ev.get("title_en"),
                           ev.get("summary_zh"), seed))
        eid = cur.lastrowid
    if source:
        add_source(con, eid, source.get("url"), source.get("title"), source.get("publisher"), source.get("date"),
                   source.get("tier", 3), source.get("quote"), source.get("article_id"))
    recompute_tier(con, eid)
    return eid


def upsert_snapshot(con, ev, entity, article_id=None):
    metric = ev.get("instrument") or "other"
    if metric not in ("capex", "rpo", "backlog", "total_debt", "compute_commitments", "other"):
        metric = "other"
    period = ev.get("period") or ev.get("announced") or ""
    con.execute("""INSERT INTO snapshots(entity_id, metric, period, value_usd, value_text, summary_zh, article_id)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_id, metric, period) DO UPDATE SET
                   value_usd=COALESCE(excluded.value_usd, value_usd), value_text=excluded.value_text,
                   summary_zh=excluded.summary_zh, article_id=excluded.article_id""",
                (entity, metric, period, ev.get("amount_usd"), ev.get("amount_text"), ev.get("summary_zh"), article_id))


# ---------- overrides ----------
def _match(con, m):
    if "id" in m:
        return con.execute("SELECT * FROM edges WHERE id=?", (m["id"],)).fetchall()
    q, vals = "SELECT * FROM edges WHERE 1=1", []
    for k in ("kind", "from", "to", "announced"):
        if k in m:
            col = {"from": "from_entity", "to": "to_entity"}.get(k, k)
            q += f" AND {col}=?"; vals.append(m[k])
    return con.execute(q, vals).fetchall()


def apply_overrides(con):
    ov = load_yaml("overrides.yaml") or {}
    n = 0
    for o in ov.get("overrides") or []:
        rows = _match(con, o.get("match", {}))
        if not rows:
            log.warning("override 未匹配：%s", o)
            continue
        act = o.get("action")
        for r in rows:
            if act == "confirm":
                con.execute("UPDATE edges SET review='confirmed', review_note=? WHERE id=?", (o.get("note"), r["id"]))
            elif act == "delete":
                con.execute("UPDATE edges SET review='deleted', review_note=? WHERE id=?", (o.get("note"), r["id"]))
            elif act == "set":
                fields = o.get("fields") or {}
                cols = [c for c in fields if c in ("amount_usd", "amount_text", "status", "announced", "instrument", "term",
                                                   "capacity", "title_zh", "title_en", "summary_zh", "from_entity", "to_entity", "kind")]
                if cols:
                    con.execute(f"UPDATE edges SET {', '.join(c + '=?' for c in cols)}, review='corrected', review_note=? WHERE id=?",
                                [fields[c] for c in cols] + [o.get("note"), r["id"]])
            elif act == "supersede":
                con.execute("UPDATE edges SET superseded_by=?, status='cancelled', review_note=? WHERE id=?",
                            (o.get("by"), o.get("note"), r["id"]))
            elif act == "merge":
                keep = o.get("into")
                con.execute("UPDATE OR IGNORE edge_sources SET edge_id=? WHERE edge_id=?", (keep, r["id"]))
                con.execute("UPDATE events SET edge_id=? WHERE edge_id=?", (keep, r["id"]))
                con.execute("UPDATE edges SET review='deleted', review_note=? WHERE id=?", (f"merged into {keep}; " + (o.get("note") or ""), r["id"]))
                recompute_tier(con, keep)
            n += 1
            if act != "merge":
                recompute_tier(con, r["id"])
    con.commit()
    return n


def recompute_all(con):
    for r in con.execute("SELECT id FROM edges").fetchall():
        recompute_tier(con, r["id"])
    con.commit()
