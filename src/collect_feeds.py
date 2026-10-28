"""新闻采集：
  tier 2 官方新闻室 / 通讯社 RSS（抓正文）
  tier 3 媒体 RSS（抓正文）+ Google News 查询（把 news.google.com 跳转解码回原文，再抓正文）
"""
import json, re, time, urllib.parse
from datetime import datetime, timezone

import feedparser, requests

from common import (SETTINGS, connect, sync_entities, load_entities, Prefilter, article_body, http_get, BROWSER_UA,
                    insert_article, sha1, days_ago, load_yaml, log, set_source_status, start_run, finish_run)

BODY_MAX = SETTINGS["fetch_body_max_chars"]
BODY_MIN = SETTINGS["body_min_chars"]
PAYWALLED = ("bloomberg.com", "wsj.com", "ft.com", "theinformation.com", "nytimes.com", "economist.com", "barrons.com")


def entry_date(e):
    for k in ("published_parsed", "updated_parsed"):
        tp = e.get(k)
        if tp:
            return datetime(*tp[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
    return None


# ---------- Google News 跳转解码 ----------
_GN_SESSION = requests.Session()
_GN_SESSION.headers["User-Agent"] = BROWSER_UA


def decode_gnews(url):
    """news.google.com/rss/articles/<id> → 原文 URL（batchexecute 方法）。失败返回 None。"""
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return None
    gid = m.group(1)
    try:
        r = _GN_SESSION.get(f"https://news.google.com/articles/{gid}", timeout=20)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sg and ts):
            return None
        req = json.dumps([[["Fbv4je", json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                                                   None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                                                   gid, int(ts.group(1)), sg.group(1)]), None, "generic"]]])
        p = _GN_SESSION.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                             headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                             data={"f.req": req}, timeout=20)
        body = p.text.split("\n\n", 1)[1]
        arr = json.loads(body)[0][2]
        return json.loads(arr)[1]
    except Exception as ex:
        log.debug("gnews decode failed %s: %s", gid[:20], ex)
        return None


def fetch_body(url):
    if any(d in url for d in PAYWALLED):
        return ""
    r = http_get(url, timeout=20, retries=0)
    if r is None or "html" not in r.headers.get("content-type", ""):
        return ""
    txt = article_body(r.text)
    return txt[:BODY_MAX] if len(txt) >= BODY_MIN else ""


def publisher_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return re.sub(r"^(www|m|amp)\.", "", host)
    except Exception:
        return None


def process_feed(con, pf, tier, kind, name, url, since, resolve=False, max_items=80):
    r = http_get(url)
    if r is None:
        set_source_status(con, name, tier, False, 0, 0, "feed unavailable")
        return 0
    feed = feedparser.parse(r.content)
    if not feed.entries:
        set_source_status(con, name, tier, False, 0, 0, "empty feed")
        return 0
    new = fetched = 0
    for e in feed.entries[:max_items]:
        link = e.get("link") or ""
        if not link:
            continue
        aid = sha1(link)
        if con.execute("SELECT 1 FROM articles WHERE id=?", (aid,)).fetchone():
            continue
        pub = entry_date(e)
        if pub and pub < since:
            continue
        fetched += 1
        title = (e.get("title") or "").strip()
        summary = article_body(e.get("summary", "") or "")
        if kind == "gnews" and summary.strip() == title.strip():
            summary = ""
        src = (e.get("source") or {}).get("title") if kind == "gnews" else None
        # 用标题 + 摘要预筛，命中实体才去抓正文
        hits, score, ok = pf.run(title + "\n" + summary)
        body, orig = "", None
        if hits:
            target = link
            if resolve and "news.google.com" in link:
                orig = decode_gnews(link)
                target = orig or link
            if "news.google.com" not in target:
                body = fetch_body(target)
            if body:
                hits, score, ok = pf.run(title + "\n" + body)
        text = body or summary
        publisher = src or publisher_of(orig or link)
        row = dict(id=aid, tier=tier, source_kind=kind, source_name=name, url=link, orig_url=orig, publisher=publisher,
                   title=title, published_at=pub, text=text, has_body=1 if body else 0,
                   entity_hits=json.dumps(hits), score=score, prefilter_hit=1 if ok else 0)
        if insert_article(con, row):
            new += 1
            if ok:
                log.info("%s [%s] %s | hits=%s body=%s", "命中" if body else "标题", name, title[:70], hits, bool(body))
        con.commit()
    set_source_status(con, name, tier, True, fetched, new)
    return new


def collect(con):
    run = start_run(con, "collect_feeds")
    ents = load_entities(con)
    pf = Prefilter(ents)
    src = load_yaml("sources.yaml")
    since = days_ago(SETTINGS["feed_lookback_days"])
    stats = {}
    try:
        for f in src.get("newsrooms", []):
            stats[f["name"]] = process_feed(con, pf, 2, "newsroom", f["name"], f["url"], since)
        for f in src.get("media_rss", []):
            stats[f["name"]] = process_feed(con, pf, 3, "media_rss", f["name"], f["url"], since)
        g = src.get("google_news", {})
        lb = SETTINGS.get("gnews_lookback", "when:4d")
        for q in g.get("queries", []):
            qs = urllib.parse.quote(f"{q['q']} {lb}".strip())
            hl, gl, ceid = q.get("hl", "en-US"), q.get("gl", "US"), q.get("ceid", "US:en")
            url = f"https://news.google.com/rss/search?q={qs}&hl={hl}&gl={gl}&ceid={ceid}"
            stats["gnews:" + q["name"]] = process_feed(con, pf, 3, "gnews", "gnews:" + q["name"], url, since,
                                                      resolve=SETTINGS.get("resolve_gnews_links", True))
            time.sleep(1)
        finish_run(con, run, stats)
    except Exception as ex:
        finish_run(con, run, stats, False, str(ex)[:500])
        raise
    total = sum(stats.values())
    log.info("新闻采集完成：新增 %d  %s", total, {k: v for k, v in stats.items() if v})
    return total


if __name__ == "__main__":
    con = connect()
    sync_entities(con)
    collect(con)
