# becky-ai-capex-flow — AI 订单与债务图谱

谁给谁下单、谁投资谁、谁欠谁的钱。自动盯 SEC 文件与新闻，`claude -p` 抽取（订阅额度，不走 API），两级核实后入账，
导出到 `docs/` 由 GitHub Pages 公开展示。

**维护人**：Becky（[@Becks-w](https://github.com/Becks-w)）。2026-10-30 从个人仓库 `Becks-w/ai-capex-flow` 转入 heyneww，本机目录名仍叫 `ai-capital-flows`。
代码改动请走 PR；数据纠错（确认 / 删除 / 合并某笔交易）不用改代码，改 `config/overrides.yaml` 即可，见「人工校对」。

- 页面：`docs/index.html`，公开地址 https://heyneww.github.io/becky-ai-capex-flow/ ；仓库 https://github.com/heyneww/becky-ai-capex-flow（Pages：main 分支 `/docs` 目录）
- 数据：`docs/data/graph.json`，每日存档 `docs/data/archive/YYYY-MM-DD.json`（页面加 `?snapshot=YYYY-MM-DD` 回看）
- 账本：`data/ledger.sqlite`（不进 git）
- `docs/` 下的内容由 Becky 本机的定时任务每 3 小时自动提交（提交信息 `Publish …`），不要手改；改页面逻辑请改 `docs/app.js` / `docs/style.css` 后走 PR

## 同事如何跑起来

仓库里没有账本和凭据，clone 下来后要自己准备：

1. **Python 3.11+**，三个第三方包：`pip3 install requests feedparser pyyaml`。
2. **`claude` CLI 已登录且有订阅**：抽取步骤调 `claude -p`，走订阅额度，不需要 API key。没装的话只能跑采集和导出（`--skip-extract`）。
3. **SEC 要求 User-Agent 带联系邮箱**：把 `config/settings.yaml` 第 2 行 `sec_user_agent` 的邮箱换成自己的，否则 EDGAR 会拒绝。
4. **Telegram 可选**：想收日报就在仓库外建 `~/.ai-capital-flows/.env`，写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`；不建则 `notify.py` 自动跳过。
5. **账本从零开始**：第一次跑 `python3 src/seed_import.py` 导入 `seeds/` 的人工核实交易，再 `python3 src/collect_edgar.py 30` 回填一个月 EDGAR，然后 `python3 src/run.py --skip-publish` 本地看效果。
6. **不要直接 `./run.sh`**：它最后一步会 `git push` 到本仓库 main，正式发布只由维护人的机器跑。本地验证一律加 `--skip-publish`。

`launchd/` 里的 plist 写死了 `/Users/beckywang/...` 路径，仅供维护人本机使用；别的机器要定时跑需改路径。

## 流程

```
采集  ──►  预筛  ──►  抽取（claude -p）  ──►  合并 / 分级  ──►  导出 docs/  ──►  git push
tier1 EDGAR submissions + 全文检索        events 表          edges + edge_sources     graph.json
tier2 公司新闻室 / 通讯社 RSS（全文）                           confirmed / reported /   archive/
tier3 媒体 RSS + Google News（解码回原文抓正文）                 candidate
```

**可信度分级**：有 tier 1/2 来源 → `confirmed`；只有媒体正文且置信 ≥ 0.7（或两家独立媒体）→ `reported`；仅标题 / 低置信 → `candidate`。
首页总额只算前两级，候选单独列在页尾，等一手来源或正文出现后自动升级。已取消 / 被取代的保留显示但不计入。
机器新发现的实体先隔离（`auto_added=1`），涉及它的边进候选区；确认后加进 `config/entities.yaml` 即可。

## 跑一次

```bash
./run.sh                                   # 采集 + 抽取 + 导出 + 推送，日志 logs/run.log
python3 src/run.py --skip-publish          # 本地看效果，不推送
python3 src/run.py --skip-collect --max 20 # 只抽 20 篇
python3 src/collect_edgar.py 30            # 回填 30 天 EDGAR
python3 src/seed_import.py                 # 导入 seeds/*.yaml 的人工核实交易
python3 src/export.py                      # 只导出
```

定时（每 3 小时）：`cp launchd/com.becks.ai-capital-flows.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.becks.ai-capital-flows.plist`

本地预览：`python3 -m http.server 8765 --directory docs` 然后打开 http://localhost:8765

## Telegram 投递

每轮 `run.sh` 结束后由 `src/notify.py` 自动跑（`--skip-notify` 可关）：

- **日报**：本地时间过了 `digest_hour`（10 点）后的第一轮 run 发当天日报——新计入交易、候选升级为计入、≥10 亿美元的大额候选、新快照、分项累计。每条带一手来源链接。
- **即时提醒**：任一轮 run 新计入 ≥ `alert_min_usd`（100 亿美元）的交易，当场推一条。
- 凭据在仓库外 `~/.ai-capital-flows/.env`（`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`），不进 config。状态在 `data/notify_state.json`（已 gitignore 的 data/ 目录）。

```bash
python3 src/notify.py --digest --dry-run        # 看看日报长什么样，不发
python3 src/notify.py --digest                  # 立即发日报
python3 src/notify.py --baseline                # 大批回填后重记基线，避免下次日报刷屏
```

## 目录

| 路径 | 作用 |
|---|---|
| `config/entities.yaml` | 实体主表（约 120 家）：id、中英文名、板块、ticker、别名、pooled 汇总对手方 |
| `config/sources.yaml` | 新闻室 / 媒体 RSS、Google News 查询 |
| `config/settings.yaml` | SEC UA、回看天数、全文检索关键词、抽取批量、合并容差、分级阈值 |
| `config/keywords.yaml` | 预筛强 / 弱关键词与金额正则 |
| `config/overrides.yaml` | 人工校对：confirm / delete / set / supersede / merge，导出时套用 |
| `schema/schema.sql` | SQLite 表结构 |
| `schema/extraction.json` | 抽取输出 JSON Schema（传给 `claude --json-schema`） |
| `seeds/` | 人工 / 研究会话核实的种子交易 |
| `src/collect_edgar.py` | EDGAR submissions（8-K / 6-K / 424B / 10-Q / 10-K）+ 全文检索 |
| `src/collect_feeds.py` | 新闻室、媒体 RSS、Google News（含链接解码与正文抓取） |
| `src/extract.py` | 分批送 `claude -p`，实体归一，写 events，再合并进 edges |
| `src/confirm.py` | 合并规则、可信度分级、取代、overrides |
| `src/export.py` | 导出 graph.json + 存档 + 监控状态 |
| `src/run.py` | 编排 + git 推送 |
| `docs/` | 静态站：index.html / app.js / style.css / data/ |

## 边的方向

| kind | from | to |
|---|---|---|
| order 订单 | 买方 / 付钱方 | 卖方 / 供给方 |
| investment 投资 | 投资人 | 被投方（含收购、认股权证、参与股权发行） |
| debt 债务 | 借款人 | 贷款人（债券 → BONDHOLDERS，未具名银团 → LENDER_SYNDICATE） |
| lease 租赁融资 | 承租 / 用设备的一方 | 出租 / 买设备再租回的出资方 |
| guarantee 担保兜底 | 担保方 | 受益方 |
| vendor_financing 供应商融资 | 提供融资的供应商 | 客户 |

状态：rumored → announced → signed → completed，amended 改金额，cancelled 不计入。合并：同类型、同双方、400 天内、金额差 ≤ 20% 视为同一笔，来源累加、置信取高。

## 人工校对

改 `config/overrides.yaml`（见文件内示例），下次导出生效；所有校对进 git 历史。候选区里确认属实的边，用 `confirm`；重复用 `merge`；LOI 被正式交易取代用 `supersede`。

## 已知限制

- Bloomberg / WSJ / FT / The Information 付费墙不抓正文，只能靠标题成为候选，等公司公告或 SEC 文件确认。
- Google News 链接解码依赖 Google 的内部接口，若失效则退化为标题级。
- `claude -p` 占订阅额度；`extract_max_per_run` 限制单次篇数，积压在后续运行里消化。
- Mac 休眠时 launchd 顺延；需要严格准点可把采集部分挪到 GitHub Actions（抽取仍需本机订阅）。
