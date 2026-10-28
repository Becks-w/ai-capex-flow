"""合并、可信度分级、取代、人工 overrides。
边的 tier：confirmed（有 tier 1/2 来源）> reported（媒体正文且置信 ≥ 阈值）> candidate（仅标题 / 低置信）。
"""
import re, json
from datetime import datetime, timedelta, timezone

from common import SETTINGS, KINDS, load_yaml, log, now_iso

STATUS_RANK = {"rumored": 0, "announced": 1, "signed": 2, "completed": 3, "amended": 3, "cancelled": 4}


def _amount_ok(a, b, tol):
    if a and b:
        return abs(a - b) / max(a, b) <= tol
    return True


def merge_window(kind):
    """债务是有日期的事件（定价 / 签约），同一发行人几个月后再发一笔相近规模很常见，只在短窗口内合并。"""
    return SETTINGS.get("merge_window_days_debt", 21) if kind == "debt" else SETTINGS["merge_window_days"]


def find_edge(con, kind, f, t, amount, announced=None):
    tol = SETTINGS["merge_amount_tolerance"]
    # 已删除的边也参与匹配（排在活边之后）：被 merge 掉的记法再被新闻抽出时，挂到合并目标上；
    # 被 delete 掉的错误记法再出现时，挂回那条已删除的边，不再新建。否则每次重抽都会复活一条重复。
    rows = con.execute("""SELECT * FROM edges WHERE kind=? AND from_entity=? AND to_entity=? AND distinct_deal=0
                          ORDER BY (review='deleted'), last_seen DESC""", (kind, f, t)).fetchall()
    win = merge_window(kind)
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
        return _merge_target(con, r)
    return None


def _merge_target(con, r, hops=0):
    """r 若是「merged into N」的已删除边，沿链找到仍存活的目标边；普通删除边原样返回（保持 deleted）。"""
    if r["review"] != "deleted" or hops > 5:
        return r
    m = re.match(r"merged into #?(\d+)", r["review_note"] or "")
    if not m:
        return r
    t = con.execute("SELECT * FROM edges WHERE id=?", (int(m.group(1)),)).fetchone()
    return _merge_target(con, t, hops + 1) if t else r


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
    elif body_srcs >= 1 and len(pubs) >= 2 and e["confidence"] >= SETTINGS["reported_min_confidence"]:
        tier = "reported"          # 媒体报道要两家独立来源的正文互相印证才计入
    else:
        tier = "candidate"
    if e["review"] == "confirmed":
        tier = "confirmed"
    con.execute("UPDATE edges SET tier=? WHERE id=?", (tier, edge_id))
    return tier


def upsert_edge(con, ev, f, t, source, seed=0, distinct=False):
    """ev: dict（模型事件或种子）；source: dict(url,title,publisher,date,tier,quote,article_id)。返回 edge_id。"""
    kind = ev["kind"]
    amount = ev.get("amount_usd") or None
    if distinct:
        # 独立交易：只认同类型、同双方、同宣布日期且同样标记为独立的既有边，重复导入不新建
        ex = con.execute("""SELECT * FROM edges WHERE kind=? AND from_entity=? AND to_entity=? AND distinct_deal=1
                            AND announced=? AND review!='deleted' ORDER BY id LIMIT 1""", (kind, f, t, ev.get("announced"))).fetchone()
    else:
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
                             announced, maturity_date, status, confidence, title_zh, title_en, summary_zh, seed, distinct_deal)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (kind, f, t, amount, ev.get("amount_text"), ev.get("instrument"), ev.get("term"), ev.get("capacity"),
                           ev.get("announced"), ev.get("maturity_date"), st, conf, ev.get("title_zh"), ev.get("title_en"),
                           ev.get("summary_zh"), seed, 1 if distinct else 0))
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
                con.execute("UPDATE edges SET review='confirmed', review_note=?, superseded_by=NULL WHERE id=?", (o.get("note"), r["id"]))
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


def _merge_into(con, keep, drop):
    con.execute("UPDATE OR IGNORE edge_sources SET edge_id=? WHERE edge_id=?", (keep, drop))
    con.execute("DELETE FROM edge_sources WHERE edge_id=?", (drop,))
    con.execute("UPDATE events SET edge_id=? WHERE edge_id=?", (keep, drop))
    con.execute("UPDATE edges SET superseded_by=? WHERE superseded_by=?", (keep, drop))
    k = con.execute("SELECT * FROM edges WHERE id=?", (keep,)).fetchone(); d = con.execute("SELECT * FROM edges WHERE id=?", (drop,)).fetchone()
    sets, vals = [], []
    for col in ("amount_usd", "amount_text", "instrument", "term", "capacity", "announced", "maturity_date", "title_zh", "title_en", "summary_zh"):
        if not k[col] and d[col]:
            sets.append(f"{col}=?"); vals.append(d[col])
    if STATUS_RANK.get(d["status"], 0) > STATUS_RANK.get(k["status"], 0) and d["status"] != "cancelled":
        sets.append("status=?"); vals.append(d["status"])
    if (d["confidence"] or 0) > (k["confidence"] or 0):
        sets.append("confidence=?"); vals.append(d["confidence"])
    if d["seed"]:
        sets.append("seed=1")
    if sets:
        con.execute(f"UPDATE edges SET {', '.join(sets)} WHERE id=?", vals + [keep])
    con.execute("UPDATE edges SET review='deleted', review_note=? WHERE id=?", (f"merged into #{keep}", drop))


def dedupe(con):
    """实体重新映射或多来源抽取后，把同类型、同双方、金额相近、日期相近的边合并（保留较早的 id）。"""
    tol = SETTINGS["merge_amount_tolerance"]
    rows = con.execute("SELECT * FROM edges WHERE review!='deleted' AND distinct_deal=0 ORDER BY kind, from_entity, to_entity, id").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["kind"], r["from_entity"], r["to_entity"]), []).append(r)
    merged = 0
    for key, rs in groups.items():
        kept = []
        win = merge_window(key[0])
        for r in rs:
            target = None
            for k in kept:
                if not _amount_ok(r["amount_usd"], k["amount_usd"], tol):
                    continue
                if r["announced"] and k["announced"]:
                    try:
                        if abs((datetime.fromisoformat(r["announced"][:10]) - datetime.fromisoformat(k["announced"][:10])).days) > win:
                            continue
                    except ValueError:
                        pass
                target = k; break
            if target:
                _merge_into(con, target["id"], r["id"]); merged += 1
            else:
                kept.append(r)
        for k in kept:
            recompute_tier(con, k["id"])
    con.commit()
    if merged:
        log.info("去重合并 %d 条边", merged)
    return merged


def _tier_rank(t):
    return {"confirmed": 2, "reported": 1}.get(t, 0)


def cluster_duplicates(con):
    """同一笔交易被不同媒体用不同类型 / 对手方写出来（如 SPV 融资写成 lease / debt / vendor_financing）。
       规则（只和已选定的主记录比，不传递）：
         a) 双方相同（不分方向、不分类型）且金额差 ≤ 2% → 同一笔；
         b) 共享一方、金额差 ≤ 5%、宣布日期相差 ≤ 10 天 → 同一笔，但两边的另一方都是汇总对手方且不同（债券市场 vs 股市）时不算。
       主记录优先：种子 > 可信度层级 > 来源数 > 较早 id。其余标 review='duplicate'，不计入总额；overrides 里 confirm 可救回。每次导出重算。"""
    con.execute("UPDATE edges SET review='pending', superseded_by=NULL, review_note=NULL WHERE review='duplicate'")
    pooled = {r["id"] for r in con.execute("SELECT id FROM entities WHERE pooled=1")}
    rows = [dict(r) for r in con.execute("SELECT * FROM edges WHERE review NOT IN ('deleted','confirmed') AND distinct_deal=0 AND amount_usd IS NOT NULL AND amount_usd>0 AND superseded_by IS NULL")]
    for r in rows:
        r["nsrc"] = con.execute("SELECT COUNT(*) FROM edge_sources WHERE edge_id=?", (r["id"],)).fetchone()[0]
    rows.sort(key=lambda r: (-r["seed"], -_tier_rank(r["tier"]), -r["nsrc"], r["id"]))
    def close_dates(a, b, days):
        if not a or not b:
            return True
        try:
            return abs((datetime.fromisoformat(a[:10]) - datetime.fromisoformat(b[:10])).days) <= days
        except ValueError:
            return True
    def same_deal(a, b):
        pa, pb = {a["from_entity"], a["to_entity"]}, {b["from_entity"], b["to_entity"]}
        diff = abs(a["amount_usd"] - b["amount_usd"]) / max(a["amount_usd"], b["amount_usd"])
        if a["kind"] == "debt" and b["kind"] == "debt" and not close_dates(a["announced"], b["announced"], merge_window("debt")):
            return False  # 同一发行人隔几个月再发一笔同规模债券，不是同一笔
        if pa == pb and diff <= 0.02:
            return True
        shared = pa & pb
        if shared and shared <= pooled and (pa - shared) != (pb - shared):
            return False  # 只共享一个汇总对手方（债券市场 / 银团）而借款人不同，不是同一笔（英伟达 $25B 债券 vs SpaceX $25B 债券）
        if (a["kind"] == "guarantee") != (b["kind"] == "guarantee"):
            return False  # 担保是交易的衍生承诺，不与订单 / 债务按共享一方聚类（如博通 $43B 担保 vs Anthropic $42B 可转票据）
        if shared and diff <= 0.05 and close_dates(a["announced"], b["announced"], 10):
            oa, ob = pa - shared, pb - shared
            if oa and ob and oa <= pooled and ob <= pooled and oa != ob:
                return False
            return True
        return False
    primaries, n = [], 0
    for r in rows:
        hit = next((p for p in primaries if same_deal(p, r)), None)
        if hit is None:
            primaries.append(r); continue
        con.execute("UPDATE edges SET review='duplicate', superseded_by=?, review_note=? WHERE id=?",
                    (hit["id"], f"同一交易的另一记法，主记录 #{hit['id']}", r["id"]))
        for s in con.execute("SELECT * FROM edge_sources WHERE edge_id=?", (r["id"],)).fetchall():
            add_source(con, hit["id"], s["url"], s["title"], s["publisher"], s["date"], s["tier"], s["quote"], s["article_id"])
        n += 1
    for p_ in primaries:
        recompute_tier(con, p_["id"])
    con.commit()
    if n:
        log.info("同一交易聚类：%d 条标为重复记法", n)
    return n


def recompute_all(con):
    for r in con.execute("SELECT id FROM edges").fetchall():
        recompute_tier(con, r["id"])
    con.commit()
