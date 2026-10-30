"""Telegram 投递：每日账本日报 + 大额新交易即时提醒。

凭据放仓库外 ~/.ai-capital-flows/.env（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），
不进 config/（config 已随公开仓库推送）。状态记在 data/notify_state.json：
  last_digest  上次日报时间（UTC）
  known        上次日报时各计入边的 tier，用来识别「新计入」和「候选升级」
  alerted      已即时提醒过的边 id
用法：
  python3 src/notify.py --digest            立即发日报（无论几点）
  python3 src/notify.py --digest --dry-run  只打印不发
  python3 src/notify.py --alerts            只跑大额提醒
  python3 src/notify.py --baseline          只记基线不发（首次接入 / 大批回填后用）
  python3 src/notify.py --digest --since-hours 12   测试：只看最近 12 小时
run.py 每轮结束会调 after_run()：先发大额提醒，再看是否到日报时间。
"""
import argparse, json, html, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from common import ROOT, SETTINGS, KINDS, KIND_ZH, connect, log
from export import counted, is_facility

STATE = ROOT / "data" / "notify_state.json"
ENV_FILES = [Path.home() / ".ai-capital-flows" / ".env", Path.home() / ".follow-builders" / ".env"]
SITE_URL = SETTINGS.get("site_url", "https://heyneww.github.io/becky-ai-capex-flow/")
DIGEST_HOUR = int(SETTINGS.get("digest_hour", 10))
ALERT_MIN = float(SETTINGS.get("alert_min_usd", 10e9))
CAND_MIN = float(SETTINGS.get("digest_candidate_min_usd", 1e9))
MAX_LIST = int(SETTINGS.get("digest_max_items", 12))
COUNTED_TIERS = ("confirmed", "reported")


# ---------- 凭据 / 状态 ----------
def load_env():
    env = {}
    for f in ENV_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    return token, chat


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_digest": None, "known": {}, "alerted": []}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 格式 ----------
def fmt_usd(v):
    if not v:
        return "金额未披露"
    if v >= 1e12:
        return f"{v / 1e12:.2f}万亿美元"
    if v >= 1e9:
        return f"{v / 1e8:.0f}亿美元"
    if v >= 1e8:
        return f"{v / 1e8:.1f}亿美元"
    return f"{v / 1e4:.0f}万美元"


def esc(s):
    return html.escape(str(s or ""), quote=False)


def ename(ents, eid):
    en = ents.get(eid)
    return (en["name_zh"] or en["name"]) if en else eid


def best_source(con, edge_id):
    r = con.execute("SELECT url, publisher FROM edge_sources WHERE edge_id=? ORDER BY tier, date DESC LIMIT 1", (edge_id,)).fetchone()
    return (r["url"], r["publisher"]) if r else (None, None)


def edge_line(con, ents, e, tag=None):
    title = e["title_zh"] or f'{ename(ents, e["from_entity"])} → {ename(ents, e["to_entity"])}'
    url, pub = best_source(con, e["id"])
    head = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
    bits = [KIND_ZH.get(e["kind"], e["kind"]), fmt_usd(e["amount_usd"])]
    if is_facility(e):
        bits.append("额度")
    if e["tier"] == "reported":
        bits.append("媒体核实")
    if tag:
        bits.append(tag)
    return f"• {head}\n   {esc(' · '.join(bits))}"


# ---------- 发送 ----------
def send(text, dry_run=False):
    token, chat = load_env()
    if dry_run:
        print(text); print("-" * 40, len(text), "chars")
        return True
    if not token or not chat:
        log.warning("Telegram 未配置（缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），跳过投递")
        return False
    ok = True
    for chunk in split_chunks(text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.load(r)
        except Exception as ex:
            body = getattr(ex, "read", lambda: b"")()
            log.error("Telegram 发送失败：%s %s", ex, body[:200])
            ok = False
    return ok


def split_chunks(text, limit=3900):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur); cur = ""
        cur += (line + "\n")
    if cur.strip():
        out.append(cur)
    return out


# ---------- 内容 ----------
def load_edges(con):
    ents = {r["id"]: dict(r) for r in con.execute("SELECT * FROM entities")}
    edges = [dict(r) for r in con.execute("SELECT * FROM edges WHERE review NOT IN ('deleted','duplicate') ORDER BY COALESCE(amount_usd,0) DESC, id")]
    for e in edges:
        e["quarantined"] = bool(ents.get(e["from_entity"], {}).get("auto_added") or ents.get(e["to_entity"], {}).get("auto_added"))
    return ents, edges


def live(e):
    return counted(e) and not e["quarantined"]


def build_digest(con, st, since_hours=None):
    ents, edges = load_edges(con)
    known = st.get("known", {})
    since = st.get("last_digest") or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    if since_hours:
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%d %H:%M:%S")
    first_digest = not st.get("last_digest") or bool(since_hours)

    new_cnt, upgraded = [], []
    for e in edges:
        if not live(e):
            continue
        prev = known.get(str(e["id"]))
        if first_digest:
            if e["first_seen"] >= since:
                new_cnt.append(e)
        elif prev is None:
            new_cnt.append(e)
        elif prev not in COUNTED_TIERS:
            upgraded.append(e)
    cands = [e for e in edges if e["tier"] == "candidate" and e["status"] != "cancelled" and e["first_seen"] >= since and (e["amount_usd"] or 0) >= CAND_MIN]
    snaps, seen = [], set()
    for r in con.execute("SELECT s.*, e.name_zh, e.name FROM snapshots s LEFT JOIN entities e ON e.id=s.entity_id WHERE s.created_at >= ? ORDER BY COALESCE(s.value_usd,0) DESC", (since,)):
        if (r["entity_id"], r["metric"]) not in seen:
            seen.add((r["entity_id"], r["metric"])); snaps.append(dict(r))

    by_kind = {k: (0.0, 0) for k in KINDS}
    for e in edges:
        if live(e) and e["status"] != "cancelled":
            v, n = by_kind[e["kind"]]
            by_kind[e["kind"]] = (v + (e["amount_usd"] or 0), n + 1)

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"<b>AI 订单与债务账本 · {today} 日报</b>",
             f"统计窗口：{since[:16]} UTC 起 · <a href=\"{SITE_URL}\">看图谱</a>", ""]

    def section(title, rows, tag=None):
        if not rows:
            return
        lines.append(f"<b>{title}（{len(rows)}）</b>")
        for e in rows[:MAX_LIST]:
            lines.append(edge_line(con, ents, e, tag))
        if len(rows) > MAX_LIST:
            lines.append(f"   …另有 {len(rows) - MAX_LIST} 笔，见网站")
        lines.append("")

    section("新计入交易", new_cnt)
    section("候选升级为计入", upgraded, "升级")
    section(f"大额候选（≥{fmt_usd(CAND_MIN)}，待核实）", cands, "候选")
    if snaps:
        lines.append(f"<b>指引 / 快照（{len(snaps)}）</b>")
        for s in snaps[:MAX_LIST]:
            nm = s["name_zh"] or s["name"] or s["entity_id"]
            lines.append(f"• {esc(nm)} {esc(s['metric'])} {esc(s['period'] or '')}：{esc(s['value_text'] or fmt_usd(s['value_usd']))}")
        lines.append("")
    if not (new_cnt or upgraded or cands or snaps):
        lines.append("本期无新计入交易、无升级、无大额候选。"); lines.append("")

    lines.append("<b>分项累计（计入口径）</b>")
    for k in KINDS:
        v, n = by_kind[k]
        if n:
            lines.append(f"{KIND_ZH[k]} {fmt_usd(v)} / {n} 笔")
    pend = con.execute("SELECT COUNT(*) FROM articles WHERE status='new' AND prefilter_hit=1").fetchone()[0]
    lines.append(f"\n候选区 {sum(1 for e in edges if e['tier'] == 'candidate' and e['status'] != 'cancelled')} 笔 · 待抽取文章 {pend} 篇")

    new_known = {str(e["id"]): e["tier"] for e in edges if live(e)}
    return "\n".join(lines), new_known


def build_alerts(con, st):
    ents, edges = load_edges(con)
    alerted = set(st.get("alerted", []))
    known = st.get("known", {})
    if not st.get("last_digest"):
        return None, []          # 还没基线（没发过日报 / 没 --baseline），不提醒，否则会把历史回填全推出去
    hits = [e for e in edges if live(e) and (e["amount_usd"] or 0) >= ALERT_MIN and e["id"] not in alerted
            and known.get(str(e["id"])) not in COUNTED_TIERS and e["first_seen"] >= st["last_digest"]]
    if not hits:
        return None, []
    lines = [f"<b>⚡ 大额新计入交易（≥{fmt_usd(ALERT_MIN)}）</b>", ""]
    for e in hits:
        lines.append(edge_line(con, ents, e))
        if e["summary_zh"]:
            lines.append(f"   {esc(e['summary_zh'][:200])}")
    lines.append(f"\n<a href=\"{SITE_URL}\">看图谱</a>")
    return "\n".join(lines), [e["id"] for e in hits]


# ---------- 入口 ----------
def baseline(con):
    """只记状态不发消息：把当前所有计入边记为已知，之后只报新增。"""
    st = load_state()
    _, new_known = build_digest(con, st)
    st["last_digest"] = utcnow(); st["known"] = new_known
    save_state(st)
    log.info("Telegram 基线已记录：%d 条计入边", len(new_known))


def run_digest(con, dry_run=False, since_hours=None):
    st = load_state()
    text, new_known = build_digest(con, st, since_hours)
    if send(text, dry_run) and not dry_run:
        st["last_digest"] = utcnow(); st["known"] = new_known
        save_state(st)
        log.info("Telegram 日报已发（%d 字）", len(text))


def run_alerts(con, dry_run=False):
    st = load_state()
    text, ids = build_alerts(con, st)
    if not text:
        return
    if send(text, dry_run) and not dry_run:
        st["alerted"] = sorted(set(st.get("alerted", [])) | set(ids))[-500:]
        save_state(st)
        log.info("Telegram 大额提醒已发：%s", ids)


def after_run(con):
    """run.py 每轮末尾调用：即时提醒 + 到点日报。"""
    token, chat = load_env()
    if not token or not chat:
        return
    try:
        run_alerts(con)
    except Exception as ex:
        log.error("Telegram 提醒失败：%s", ex)
    st = load_state()
    last = (st.get("last_digest") or "")[:10]
    now_local = datetime.now()
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if now_local.hour >= DIGEST_HOUR and last != today_utc:
        try:
            run_digest(con)
        except Exception as ex:
            log.error("Telegram 日报失败：%s", ex)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--alerts", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="只记基线不发消息")
    ap.add_argument("--since-hours", type=int, help="日报统计窗口改为最近 N 小时（测试用）")
    a = ap.parse_args()
    con = connect()
    if a.baseline:
        baseline(con)
    elif a.alerts:
        run_alerts(con, a.dry_run)
    else:
        run_digest(con, a.dry_run, a.since_hours)
