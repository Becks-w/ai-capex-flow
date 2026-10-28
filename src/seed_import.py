"""导入 seeds/*.yaml 的人工核实交易（seed=1）。可重复运行：按合并规则去重，来源累加。"""
import glob, sys
from pathlib import Path

import yaml

from common import ROOT, connect, sync_entities, load_entities, Prefilter, log, resolve_entity
from confirm import upsert_edge, find_edge, recompute_tier


def key_of(d):
    return f"{d['kind']}|{d['from']}|{d['to']}|{d.get('announced')}"


def run(paths=None):
    con = connect()
    sync_entities(con)
    pf = Prefilter(load_entities(con))
    files = paths or sorted(glob.glob(str(ROOT / "seeds" / "*.yaml")))
    cache, n, ids_by_key = {}, 0, {}
    pending_supersedes = []
    for fp in files:
        data = yaml.safe_load(Path(fp).read_text(encoding="utf-8")) or {}
        for d in data.get("deals") or []:
            f = resolve_entity(con, pf, d["from"], cache)
            t = resolve_entity(con, pf, d["to"], cache)
            if not f or not t or f == t:
                log.warning("跳过（实体无法归一）：%s", d.get("title_en"))
                continue
            srcs = d.get("sources") or []
            if not srcs:
                log.warning("跳过（无来源）：%s", d.get("title_en"))
                continue
            ev = dict(d)
            ev["confidence"] = d.get("confidence") or (0.95 if min(s.get("tier", 3) for s in srcs) <= 2 else 0.8)
            eid = upsert_edge(con, ev, f, t, None, seed=1, distinct=bool(d.get("distinct")))
            for s in srcs:
                from confirm import add_source
                add_source(con, eid, s.get("url"), s.get("title"), s.get("publisher"), s.get("date"), s.get("tier", 3))
            recompute_tier(con, eid)
            ids_by_key[key_of(d)] = eid
            if d.get("supersedes"):
                pending_supersedes.append((eid, d["supersedes"]))
            n += 1
    for eid, key in pending_supersedes:
        old = ids_by_key.get(key)
        if not old:
            parts = key.split("|")
            if len(parts) == 4:
                r = find_edge(con, parts[0], parts[1], parts[2], None, parts[3])
                old = r["id"] if r else None
        if old:
            con.execute("UPDATE edges SET superseded_by=?, status='cancelled' WHERE id=?", (eid, old))
            log.info("取代：#%d → #%d", old, eid)
    con.commit()
    log.info("种子导入完成：%d 笔", n)
    return n


if __name__ == "__main__":
    run(sys.argv[1:] or None)
