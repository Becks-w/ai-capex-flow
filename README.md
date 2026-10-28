# ai-capital-flows — AI 订单与债务图谱

谁给谁下单、谁投资谁、谁欠谁的钱。自动盯 SEC 文件与新闻，`claude -p` 抽取（订阅额度，不走 API），两级核实后入账，
导出到 `docs/` 由 GitHub Pages 公开展示。

- 页面：`docs/index.html`，公开地址 https://becks-w.github.io/ai-capex-flow/ ；仓库 https://github.com/Becks-w/ai-capex-flow（Pages：main 分支 `/docs` 目录）
- 数据：`docs/data/graph.json`，每日存档 `docs/data/archive/YYYY-MM-DD.json`（页面加 `?snapshot=YYYY-MM-DD` 回看）
- 账本：`data/ledger.sqlite`（不进 git）

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
