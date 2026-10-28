# 种子交易

`seed_deals.yaml` 由研究会话人工 / Claude 逐笔核实后录入，`python3 src/seed_import.py` 导入账本（seed=1）。
每笔至少一个一手来源（SEC 文件或公司新闻稿，tier 1/2）；没有一手来源的要两家独立媒体（tier 3）。
来源 URL 必须实际打开过并确认内容与交易一致。

格式：

```yaml
deals:
  - kind: order                    # order | investment | debt | lease | guarantee | vendor_financing
    from: OPENAI                   # config/entities.yaml 里的 id；order=买方 investment=投资人 debt=借款人 lease=承租方 guarantee=担保方 vendor_financing=出资的供应商
    to: ORCL                       # order=卖方 investment=被投方 debt=贷款人（BONDHOLDERS / LENDER_SYNDICATE / PRIVATE_CREDIT） lease=出租/出资方 guarantee=受益方
    amount_usd: 300000000000       # 没有就 null
    amount_text: "$300B over about five years"
    instrument: compute_contract   # 见 schema/extraction.json 的 instrument 说明
    term: "~5 years starting 2027"
    capacity: "4.5 GW"
    announced: "2025-09-10"
    status: signed                 # rumored | announced | signed | completed | amended | cancelled
    title_zh: "OpenAI 与甲骨文签 3000 亿美元算力合同"
    title_en: "OpenAI signs $300B compute contract with Oracle"
    summary_zh: "一两句：内容、期限、条件、与其他交易的关系"
    supersedes: null               # 可选：被本笔取代的交易 key（kind|from|to|announced）
    sources:
      - {url: "https://...", title: "...", publisher: "Oracle", date: "2025-09-10", tier: 2}
```
