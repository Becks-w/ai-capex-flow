"""共用：配置、数据库、HTML 转文本、预筛、HTTP、实体归一。"""
import hashlib, json, re, sqlite3, sys, time, logging
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests, yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DB_PATH = ROOT / "data" / "ledger.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("acf")


def load_yaml(name):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


SETTINGS = load_yaml("settings.yaml")
KINDS = ["order", "investment", "debt", "lease", "guarantee", "vendor_financing"]
KIND_ZH = {"order": "订单", "investment": "投资", "debt": "债务", "lease": "租赁融资", "guarantee": "担保兜底",
           "vendor_financing": "供应商融资"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ---------- DB ----------
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")       # 采集 / 抽取 / 导出可并行读写
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript((ROOT / "schema" / "schema.sql").read_text(encoding="utf-8"))
    return con


def sync_entities(con):
    """config/entities.yaml → entities 表（新增 / 更新，不删）。"""
    cfg = load_yaml("entities.yaml")
    for e in cfg["entities"]:
        con.execute(
            """INSERT INTO entities(id, name, name_zh, grp, ticker, country, aliases, pooled)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, name_zh=excluded.name_zh, grp=excluded.grp,
               ticker=excluded.ticker, country=excluded.country, aliases=excluded.aliases, pooled=excluded.pooled, auto_added=0""",
            (e["id"], e["name"], e.get("name_zh"), e["group"], e.get("ticker"), e.get("country"),
             json.dumps(e.get("aliases", []), ensure_ascii=False), 1 if e.get("pooled") else 0))
    con.commit()
    return cfg


def load_entities(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM entities ORDER BY id")]
    for r in rows:
        r["aliases"] = json.loads(r["aliases"] or "[]")
    return rows


def start_run(con, stage):
    cur = con.execute("INSERT INTO runs(stage) VALUES(?)", (stage,))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id, stats, ok=True, error=None):
    con.execute("UPDATE runs SET finished_at=?, stats=?, ok=?, error=? WHERE id=?",
                (now_iso(), json.dumps(stats, ensure_ascii=False), 1 if ok else 0, error, run_id))
    con.commit()


def set_source_status(con, name, tier, ok, fetched=0, added=0, error=None):
    con.execute("""INSERT INTO source_status(name, tier, last_run, ok, fetched, added, error) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET tier=excluded.tier, last_run=excluded.last_run, ok=excluded.ok,
                   fetched=excluded.fetched, added=excluded.added, error=excluded.error""",
                (name, tier, now_z(), 1 if ok else 0, fetched, added, (error or "")[:300] or None))
    con.commit()


# ---------- 文本 ----------
class _Stripper(HTMLParser):
    SKIP = {"script", "style", "head", "noscript", "svg", "nav", "footer", "header", "aside", "form"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "td", "section", "article", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    s = _Stripper()
    try:
        s.feed(html)
    except Exception:
        pass
    text = "".join(s.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def article_body(html):
    """从整页 HTML 里取正文：优先 <article>，否则取段落最密集的部分，再否则整页。"""
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.S | re.I)
    if m:
        t = html_to_text(m.group(1))
        if len(t) > 600:
            return t
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
    if paras:
        t = "\n".join(html_to_text(p) for p in paras)
        t = "\n".join(l for l in t.split("\n") if len(l.strip()) > 40)
        if len(t) > 600:
            return t
    return html_to_text(html)


# ---------- 预筛 ----------
class Prefilter:
    def __init__(self, entities):
        kw = load_yaml("keywords.yaml")
        mk = lambda words: re.compile(r"\b(" + "|".join(re.escape(k) for k in words) + r")\b", re.I)
        self.strong_re = mk(kw["strong_keywords"])
        self.weak_re = mk(kw["weak_keywords"])
        self.amount_re = re.compile(kw["amount_regex"], re.I)
        self.entity_res = []
        for e in entities:
            aliases = e["aliases"] if isinstance(e["aliases"], list) else json.loads(e["aliases"])
            if not aliases:
                continue
            pat = r"(?<![A-Za-z0-9])(" + "|".join(re.escape(a) for a in aliases) + r")(?![A-Za-z0-9])"
            self.entity_res.append((e["id"], re.compile(pat)))

    def run(self, text):
        hits = [eid for eid, rx in self.entity_res if rx.search(text)]
        strong = len({m.group(1).lower() for m in self.strong_re.finditer(text)})
        weak = len({m.group(1).lower() for m in self.weak_re.finditer(text)})
        amounts = len(self.amount_re.findall(text))
        score = 2 * strong + weak + 2 * min(amounts, 3)
        ok = bool(hits) and (strong >= 1 or (weak >= 1 and amounts >= 1) or (weak >= 2 and len(hits) >= 2))
        return hits, score, ok


# ---------- HTTP ----------
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


def http_get(url, headers=None, timeout=25, retries=1, allow_redirects=True):
    h = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6"}
    if headers:
        h.update(headers)
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=h, timeout=timeout, allow_redirects=allow_redirects)
            if r.status_code == 200:
                return r
            if r.status_code in (401, 403, 404, 410, 451):
                return None
            log.warning("GET %s -> %s", url[:100], r.status_code)
        except requests.RequestException as ex:
            log.warning("GET %s failed: %s", url[:100], str(ex)[:120])
        time.sleep(1.5 * (i + 1))
    return None


def insert_article(con, row):
    base = dict(orig_url=None, publisher=None, edgar_form=None, edgar_items=None, edgar_entity=None)
    base.update(row)
    cur = con.execute(
        """INSERT OR IGNORE INTO articles(id, tier, source_kind, source_name, url, orig_url, publisher, title, published_at,
           text, has_body, entity_hits, score, prefilter_hit, edgar_form, edgar_items, edgar_entity)
           VALUES(:id,:tier,:source_kind,:source_name,:url,:orig_url,:publisher,:title,:published_at,
           :text,:has_body,:entity_hits,:score,:prefilter_hit,:edgar_form,:edgar_items,:edgar_entity)""", base)
    return cur.rowcount == 1


# ---------- 实体归一 ----------
def slug(name):
    return "NEW_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:40]


POOLED_MAP = {"bondholders": "BONDHOLDERS", "bond investors": "BONDHOLDERS", "noteholders": "BONDHOLDERS",
              "public investors": "PUBLIC_INVESTORS", "public equity investors": "PUBLIC_INVESTORS",
              "private-round investors": "PRIVATE_ROUND", "private round investors": "PRIVATE_ROUND", "investors": "PRIVATE_ROUND",
              "lender syndicate": "LENDER_SYNDICATE", "bank syndicate": "LENDER_SYNDICATE", "banks": "LENDER_SYNDICATE",
              "private credit lenders": "PRIVATE_CREDIT", "private credit": "PRIVATE_CREDIT",
              "undisclosed": "UNDISCLOSED", "undisclosed customer": "UNDISCLOSED", "undisclosed counterparty": "UNDISCLOSED"}


# 模型给的 NEW:名字 常有变体，用规则归到同一实体（先于别名匹配）
NAME_RULES = [
    (re.compile(r"SPV|XPU|XPV", re.I), re.compile(r"Broadcom|Anthropic|XPU|XPV", re.I), "ANTHROPIC_XPU_SPV"),
    (re.compile(r"Beignet", re.I), None, "BEIGNET"),
    (re.compile(r"Naver", re.I), None, "NAVER"),
    (re.compile(r"Abilene", re.I), re.compile(r"JV|joint venture|Crusoe", re.I), "ABILENE_JV"),
    (re.compile(r"Stargate", re.I), None, "STARGATE"),
]


def resolve_entity(con, pf, raw, cache, allow_new=True):
    """模型给的 id 或 NEW:Name → entities.id。未知实体自动新增为隔离状态（auto_added=1，页面不画）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in cache:
        return cache[raw]
    row = con.execute("SELECT id FROM entities WHERE id=?", (raw,)).fetchone()
    if row:
        cache[raw] = row["id"]
        return row["id"]
    name = raw[4:].strip() if raw.upper().startswith("NEW:") else raw
    key = name.lower().strip()
    if key in POOLED_MAP:
        cache[raw] = POOLED_MAP[key]
        return POOLED_MAP[key]
    for r1, r2, eid in NAME_RULES:
        if r1.search(name) and (r2 is None or r2.search(name)) and con.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone():
            cache[raw] = eid
            return eid
    hits, _, _ = pf.run(name)
    if len(hits) == 1:
        cache[raw] = hits[0]
        return hits[0]
    row = con.execute("SELECT id FROM entities WHERE lower(name)=lower(?) OR lower(name_zh)=lower(?)", (name, name)).fetchone()
    if row:
        cache[raw] = row["id"]
        return row["id"]
    if not allow_new:
        return None
    eid = slug(name)
    con.execute("INSERT OR IGNORE INTO entities(id, name, grp, aliases, auto_added) VALUES(?,?,?,?,1)",
                (eid, name, "other", json.dumps([name], ensure_ascii=False)))
    log.info("新实体（隔离）%s = %s", eid, name)
    cache[raw] = eid
    return eid
