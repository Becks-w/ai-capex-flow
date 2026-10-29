"""流程编排：collect → extract → export → publish。launchd 每 3 小时调用 run.sh。"""
import argparse, subprocess, time
from datetime import datetime, timezone

from common import ROOT, SETTINGS, connect, sync_entities, log
import collect_edgar, collect_feeds, extract, export, notify


def publish():
    """docs/ 有变化就 commit + push（远端未配置时只 commit）。"""
    site = SETTINGS.get("site_dir", "docs")
    st = subprocess.run(["git", "status", "--porcelain", site], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not st:
        log.info("docs/ 无变化，不推送")
        return False
    subprocess.run(["git", "add", site], cwd=ROOT, check=True)
    msg = f"Publish {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True)
    remotes = subprocess.run(["git", "remote"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not remotes:
        log.info("已 commit（未配置远端，未 push）")
        return True
    p = subprocess.run(["git", "push", "-q"], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        log.error("push 失败：%s", p.stderr[-300:])
        return False
    log.info("已 push：%s", msg)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-collect", action="store_true")
    ap.add_argument("--skip-extract", action="store_true")
    ap.add_argument("--skip-publish", action="store_true")
    ap.add_argument("--skip-notify", action="store_true")
    ap.add_argument("--max", type=int, help="本次最多抽取几篇")
    ap.add_argument("--edgar-days", type=int)
    a = ap.parse_args()
    t0 = time.time()
    con = connect()
    sync_entities(con)
    if not a.skip_collect:
        try:
            collect_edgar.collect(con, a.edgar_days)
        except Exception as ex:
            log.error("EDGAR 采集失败：%s", ex)
        try:
            collect_feeds.collect(con)
        except Exception as ex:
            log.error("新闻采集失败：%s", ex)
    if not a.skip_extract:
        extract.extract(con, a.max)
    export.export(con)
    if not a.skip_publish:
        publish()
    if not a.skip_notify:
        try:
            notify.after_run(con)
        except Exception as ex:
            log.error("Telegram 投递失败：%s", ex)
    log.info("RUN 完成，用时 %.0f 秒", time.time() - t0)


if __name__ == "__main__":
    main()
