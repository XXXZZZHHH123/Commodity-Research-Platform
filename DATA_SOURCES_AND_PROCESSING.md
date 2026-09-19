# 数据来源与处理清单

> 适用仓库：`research-workbench`
> 复核日期：2026-09-19
> 说明：本文根据当前仓库中的 Python 脚本、数据文件和 GitHub Actions 工作流整理。它描述的是“已经实现的数据管线”，不保证每个外部接口在任意时间都可用。

## 1. 数据分层约定

本项目中的数据建议按以下层级理解和存储：

| 层级 | 含义 | 典型来源 |
|---|---|---|
| L1 官方 | 政府、央行、监管机构直接发布 | FRED、BLS、BEA、美国财政部、CFTC、纽约联储、ECB、BOJ、BoE、BoC |
| L2 市场/交易所 | 交易所、行情服务或权威市场数据 | CME、Yahoo Finance、iFinD |
| L3 第三方汇编 | 第三方终端、财经日历、媒体和数据聚合商 | 东方财富、金十、Forex Factory、先讯、CS 财经、OCMacro |
| DERIVED 衍生 | 使用原始序列计算出来的指标 | 同比、差分、利差、分位数、Z-score、净流动性、金银比 |
| MANUAL 人工 | 人工或 AI 辅助写入，不是自动数据抓取结果 | `views.json`、`forecasts.json`、`research_notes.json`、`geopolitics.json` |

注意：仓库注释中个别层级标注不够严格。例如 iFinD 是商业行情终端，更适合归入 L2；东方财富宏观库是对官方数据的第三方汇编，应归入 L3，而不是原始官方源。

## 2. 数据源总表

| 数据源 | 类型 | 接口/域名 | 鉴权 | 负责脚本 | 主要产物 |
|---|---|---|---|---|---|
| FRED | L1 官方分发平台 | `fred.stlouisfed.org/graph/fredgraph.csv` | 无 | `fetch_macro.py`、`fetch_extra.py`、`fetch_liquidity.py` | `chart_series.js`、`macro_series.json` |
| FRED REST 网关 | L1，专有环境中转 | `igo_open_data` / `agent-gw` | 专有运行环境 | `repair_fred_via_gw.py` | 修复 `chart_series.js` 中缺失的 FRED 序列 |
| Yahoo Finance | L2 行情聚合 | `query1.finance.yahoo.com` | 无，但可能限流 | `fetch_macro.py`、`fetch_live.py`、`fetch_cftc.py` | 历史行情、实时快照、ETF 份额 |
| 美国财政部 FiscalData | L1 官方 | `api.fiscaldata.treasury.gov` | 无 | `fetch_macro.py`、`fetch_extra.py` | 联邦债务、美债拍卖 |
| 美国财政部 TIC | L1 官方 | `ticdata.treasury.gov` | 无 | `fetch_extra.py` | 海外持有美债数据 |
| BLS | L1 官方 | `api.bls.gov` | 可无 Key；有 Key 配额更高 | `fetch_release.py` | 非农、CPI 分项 |
| BEA | L1 官方 | `apps.bea.gov/api` | `BEA_KEY` | `fetch_release.py` | GDP、PCE 分项贡献 |
| CFTC | L1 官方 | `cftc.gov/files/dea/history` | 无 | `fetch_cftc.py` | COT 非商业净持仓 |
| 纽约联储 | L1 官方 | `markets.newyorkfed.org` | 无 | `fetch_liquidity.py`、`fetch_extra.py` | SRF、一级交易商持仓 |
| 纽约联储 ACM | L1 官方研究数据 | `newyorkfed.org/.../ACMTermPremium.xls` | 无 | `fetch_extra.py` | 10Y 期限溢价 |
| ECB Data Portal | L1 官方 | `data-api.ecb.europa.eu` | 无 | `fetch_liquidity.py` | 欧元区 M3 |
| 日本央行 BOJ | L1 官方 | `stat-search.boj.or.jp` | 无 | `fetch_extra.py` | 日本 M2 同比 |
| 英格兰银行 BoE | L1 官方 | `bankofengland.co.uk/boeapps/database` | 无 | `fetch_extra.py` | 英国 M4 同比 |
| 加拿大央行 BoC | L1 官方 | `bankofcanada.ca/valet` | 无 | `fetch_extra.py` | 加拿大 M2 |
| TSA | L1 官方 | `tsa.gov/travel/passenger-volumes` | 无 | `fetch_macro.py` | 每日旅客安检量 |
| Zillow Research | 第三方研究数据 | `files.zillowstatic.com/research/public_csvs` | 无 | `fetch_extra.py` | 美国市场租金 ZORI |
| 东方财富数据中心 | L3 官方数据汇编 | `datacenter-web.eastmoney.com` | 无 | `fetch_cn_macro.py`、`fetch_extra.py`、`fetch_liquidity.py`、`fetch_calendar.py` | 中国宏观、中国货币、国债、财经日历 |
| 中国货币网 | L1/L2 官方市场基础设施 | `chinamoney.com.cn/ags/ms` | 无，可能有反爬限制 | `fetch_extra.py` | LPR、SHIBOR |
| iFinD | L2 商业终端 | 本地 `agent-gw` 插件 | 需要专有插件环境 | `fetch_ifind.py` | A 股指数、沪金、沪银 |
| CME FedWatch | L2 交易所 | `cmegroup.cn/fed-watch` | 无；需浏览器渲染 | `fetch_fedwatch.py` | 会议降息/不变/加息概率 |
| CME 期货结算价 | L2 交易所 | CME ZQ 结算价 | 视访问方式而定 | `fetch_release.py` | 自建 FedWatch 引擎 |
| 金十财经日历 | L3 汇编 | 金十 WebSocket | `JIN10_TOKEN` | `fetch_calendar.py` | 预期、前值、公布值、星级 |
| Forex Factory | L3 汇编 | `nfs.faireconomy.media` | 无 | `fetch_calendar.py` | 本周经济日历 |
| Polymarket Gamma/CLOB | L2 预测市场 | `gamma-api.polymarket.com`、`clob.polymarket.com` | 无 | `fetch_polymarket.py` | 政策和政治事件概率 |
| OCMacro | L3 第三方模型 | `ocmacro.com/dashboard/trump` | 无 | `fetch_trump.py` | 特朗普压力指数、支持率、TACO 事件 |
| CNN Truth Social Archive | L3 媒体存档 | `ix.cnn.io/data/truth-social` | 无 | `fetch_trump.py` | Truth Social 帖子 |
| 金十快讯 | L3 财经资讯 | `flash-api.jin10.com` | `JIN10_TOKEN` | `fetch_news.py` | 实时新闻 |
| 先讯 AlphaHeartbeat | L3 财经资讯 | `api.alphaheartbeat.com` | `AH_SESSION` | `fetch_news.py` | VIP/延时新闻、情绪和 AI logic 字段 |
| CS 财经 | L3 财经资讯 | `api.cnthesims.com` | `CS_TOKEN` | `fetch_news.py` | 24H 资讯 |
| MacroMicro CSV | L3 人工导入 | 用户从 MacroMicro 下载 | GitHub Token 用于上传 | `panels/mm_upload.html` | `chart_series.js`、`mm_catalog.js` |
| 第三方终端 EDB 快照 | L3 人工导入 | 原始终端未在仓库中说明 | 非自动 | `ht_*`、`ow_*`、`cs_snapshot.json` | 大规模历史和截面数据 |

## 3. 美国利率、债券和信用数据

| 数据 | 原始来源 | 代码/标识 | 处理方式 | 输出 |
|---|---|---|---|---|
| 美债 2Y、10Y、30Y 收益率 | FRED / 美国财政部 H.15 | `DGS2`、`DGS10`、`DGS30` | 清洗空值，按日期升序 | `chart_series.js` |
| 10Y-2Y、10Y-3M 利差 | FRED | `T10Y2Y`、`T10Y3M` | 原始单位为百分点；展示时可转 bp | `chart_series.js` |
| 10Y 实际利率 | FRED / Treasury TIPS | `DFII10` | 原样入库 | `chart_series.js` |
| 5Y、10Y 盈亏平衡通胀 | FRED | `T5YIE`、`T10YIE` | 原样入库 | `chart_series.js` |
| 5y5y 远期通胀预期 | FRED | `T5YIFR` | 原样入库 | `chart_series.js` |
| EFFR、SOFR、SOFR99、TGCR、OBFR、IORB | FRED / 纽约联储 | 多个同名 ID | 原样入库，随后计算利差 | `chart_series.js`、`liquidity.json` |
| 30 年房贷利率 | FRED | `MORTGAGE30US` | 原样入库 | `chart_series.js` |
| 高收益债 OAS、收益率 | FRED / ICE BofA | `BAMLH0A0HYM2`、`BAMLH0A0HYM2EY` | 原样入库，计算分位和压力信号 | `chart_series.js`、`liquidity.json` |
| AAA、BBB 信用利差 | FRED / ICE BofA | `BAMLC0A1CAAA`、`BAMLC0A4CBBB` | 原样入库 | `chart_series.js` |
| NFCI、圣路易斯金融压力指数 | FRED / 地区联储 | `NFCI`、`STLFSI4` | 原样入库 | `chart_series.js` |
| ACM 10Y 期限溢价 | 纽约联储 ACM Excel | `ACMTP10` | 解析 Excel，保留 2019 年以来数据 | `chart_series.js` |
| 美债拍卖 | 美国财政部 FiscalData | 2Y/10Y/30Y | 提取投标倍数、间接投标占比、最高收益率 | `chart_series.js`、`auctions.json` |
| 海外国债收益率 | FRED / OECD | 日本、德国、英国 10Y | 原样入库 | `chart_series.js` |
| 欧央行存款利率 | FRED / ECB | `ECBDFR` | 原样入库 | `chart_series.js` |

## 4. 通胀数据

| 数据 | 原始来源 | 处理方式 | 输出 |
|---|---|---|---|
| CPI、核心 CPI | FRED，底层 BLS | 月度指数计算 12 期同比 | `chart_series.js` |
| PCE、核心 PCE | FRED，底层 BEA | 月度指数计算 12 期同比 | `chart_series.js` |
| PPI | FRED，底层 BLS | 月度指数计算 12 期同比 | `chart_series.js` |
| 粘性 CPI | FRED / Atlanta Fed | 原样入库 | `chart_series.js` |
| 中位 CPI | FRED / Cleveland Fed | 原样入库 | `chart_series.js` |
| 截尾 PCE | FRED / Dallas Fed | 原样入库 | `chart_series.js` |
| 超级核心 CPI | FRED / BLS | 季调与非季调序列分别保存 | `chart_series.js` |
| 进口价格指数 | FRED / BLS | 原样入库 | `chart_series.js` |
| 密歇根 1 年通胀预期 | FRED / University of Michigan | 原样入库 | `chart_series.js` |
| 克利夫兰 5 年通胀预期 | FRED / Cleveland Fed | 原样入库 | `chart_series.js` |
| CPI 14 个分项 | BLS API | 计算季调环比、非季调同比；使用 BLS 相对重要性权重计算贡献 | `release_panels.json` |
| PCE 价格贡献 | BEA NIPA 表 2.8.8 | 按 BEA LineNumber 选择商品、服务、核心、能源、住房等分项 | `release_panels.json` |

CPI 分项包括食品、能源、住所、房租、业主等价租金、新车、二手车、服装、医疗商品、医疗服务、交通服务、娱乐、家居陈设与运营、机动车保险。

## 5. 就业数据

| 数据 | 原始来源 | 处理方式 | 输出 |
|---|---|---|---|
| 非农就业总量 | FRED / BLS CES | 对就业存量做一阶差分得到月度新增 | `chart_series.js` |
| 非农行业分项 | BLS API | 当前月减上月，得到行业新增/减少就业 | `release_panels.json` |
| U3、U6 失业率 | FRED / BLS | 原样入库 | `chart_series.js` |
| 初请、续请失业金 | FRED / DOL | 初请由人转换为千人；续请保留人数 | `chart_series.js` |
| JOLTS 职位空缺 | FRED / BLS | 原样入库 | `chart_series.js` |
| JOLTS 空缺率、雇用率、离职率 | FRED / BLS | 原样入库 | `chart_series.js` |
| 平均时薪同比 | FRED / BLS | 月度序列计算 12 期同比 | `chart_series.js` |
| ECI 雇佣成本指数 | FRED / BLS | 原样入库 | `chart_series.js` |
| 制造业就业、失业人数 | FRED / BLS | 原样入库 | `chart_series.js` |

非农分项包含私人部门、商品生产、采矿伐木、建筑、制造、贸易运输公用、零售、信息、金融、专业商业服务、教育医疗、休闲住宿、其他服务和政府部门。

## 6. 增长、制造业、消费和房地产

| 数据 | 原始来源 | 处理方式 | 输出 |
|---|---|---|---|
| 工业产出、制造业产出 | FRED | 计算 12 期同比 | `chart_series.js` |
| 产能利用率 | FRED | 原样入库 | `chart_series.js` |
| 耐用品订单 | FRED / Census | 计算 12 期同比 | `chart_series.js` |
| 核心资本品订单 | FRED / Census | 原样入库 | `chart_series.js` |
| 新屋开工 | FRED / Census | 原样入库 | `chart_series.js` |
| 美国房价 | FRED | 原样或同比入库 | `chart_series.js` |
| Zillow 租金 ZORI | Zillow Research CSV | 选择 `RegionName=United States` 的全国序列 | `chart_series.js` |
| 零售销售、实际消费、储蓄率、实际可支配收入 | FRED | 原样入库 | `chart_series.js` |
| 商业库存、库存销售比 | FRED | 原样入库 | `chart_series.js` |
| 密歇根消费者信心 | FRED / UMich | 原样入库 | `chart_series.js` |
| TSA 旅客安检量 | TSA 官网 | HTML 解析，转换为百万人 | `chart_series.js` |
| GDP 分项贡献 | BEA NIPA 表 1.1.2 | 提取消费、投资、库存、净出口、政府的百分点贡献 | `release_panels.json` |
| 实际 PCE 贡献 | BEA NIPA 表 2.8.2 | 提取商品、耐用品、非耐用品、服务贡献 | `release_panels.json` |

注意：代码把 FRED `USSTHPI` 命名为“Case-Shiller 全国房价指数”，但该 ID 实际对应 FHFA All-Transactions House Price Index。迁移时应修正名称，避免口径错误。

## 7. 财政、债务、贸易和国际资本流动

| 数据 | 原始来源 | 处理方式 | 输出 |
|---|---|---|---|
| 联邦债务总额 | 美国财政部 FiscalData | 金额除以 `1e12` 转万亿美元 | `chart_series.js` |
| 联邦债务/GDP | FRED | 原样入库 | `chart_series.js` |
| 月度财政赤字/盈余 | FRED / Treasury | 原样入库 | `chart_series.js` |
| 联邦净利息支出 | FRED / BEA | 原样入库 | `chart_series.js` |
| TGA | FRED / Treasury | 同时保留日度和周度口径 | `chart_series.js`、`liquidity.json` |
| 联邦关税收入 | FRED / BEA | 原样入库 | `chart_series.js` |
| 美国进出口和贸易差额 | FRED / BEA | 原样入库 | `chart_series.js` |
| 美国对华进口、出口 | FRED / Census | 原样入库 | `chart_series.js` |
| 美国对华贸易差额 | FRED 底层序列 | `进口 - 出口` 的日期交集计算 | `chart_series.js` |
| TIC 海外持有美债 | 美国财政部 TIC SLT Table 5 | 提取日本、中国大陆、英国和外国官方部门；按月末日期积累 | `tic.json`、`chart_series.js` |
| 一级交易商持仓 | 纽约联储 FR2004 | 提取美债、MBS、公司债、机构债净持仓 | `chart_series.js` |

## 8. 美元与全球流动性

### 8.1 原始数据

- SOFR、SOFR99、TGCR、EFFR、IORB、ON RRP 利率与余额：FRED / 纽约联储。
- 银行准备金、美联储总资产、FIMA 逆回购池、央行互换：FRED / H.4.1。
- TGA：FRED / 美国财政部。
- SRF 常备回购使用量：纽约联储 Markets API。
- 1M AA 金融商业票据利率：FRED。
- 美国 M1/M2：FRED。
- 欧元区 M3：ECB Data Portal。
- 中国 M1/M2：东方财富汇编，底层为中国人民银行数据。
- 日本 M2：日本央行官网。
- 英国 M4：英格兰银行官网。
- 加拿大 M2：加拿大央行 Valet API。
- 美联储、欧央行、日本央行资产负债表：FRED 分发的央行数据。

### 8.2 衍生计算

| 衍生指标 | 公式/方法 | 输出 |
|---|---|---|
| SOFR-IORB | `(SOFR - IORB) × 100`，单位 bp | `SPR_SOFR_IORB` |
| TGCR-IORB | `(TGCR - IORB) × 100` | `SPR_TGCR_IORB` |
| SOFR-EFFR | `(SOFR - EFFR) × 100` | `SPR_SOFR_EFFR` |
| SOFR 尾部 | `(SOFR99 - SOFR) × 100` | `SPR_SOFR99` |
| SOFR-ON RRP | `(SOFR - ON_RRP_RATE) × 100` | `SPR_SOFR_ONRRP` |
| 商票-SOFR | `(DCPF1M - SOFR) × 100` | `SPR_CP_SOFR` |
| 净流动性 | `WALCL/1e6 - TGA/1e6 - RRP/1e3`，单位万亿美元 | `NETLIQ` |
| 美国 M2 同比 | 月度 12 期同比 | `M2SL_YOY` |
| 欧元区 M3 同比 | 月度 12 期同比 | `LIQ_EZM3_YOY` |
| 央行资产同比 | 周度 52 期或月度 12 期同比 | 多个 `*_YOY` |
| 全球 M2 同比 | 美国、欧元区、中国同比的简单平均 | `GM2_YOY` |

### 8.3 压力指标

- 对 SOFR-IORB、SOFR 尾部、商票-SOFR、高收益 OAS、VIX、MOVE 计算历史分位。
- 对净流动性、美元指数、10Y 实际利率计算 63 日变化分位。
- 将可用分量等权平均，得到综合压力分位。
- 对美元、实际利率、高收益 OAS、VIX、MOVE 的 63 日变化计算 Z-score；`z >= 2` 触发广义冲击信号。
- 对 SOFR-IORB、SOFR 尾部、商票-SOFR 的 20 日变化计算 Z-score；`z >= 2` 触发融资压力信号。
- 当 ON RRP 余额低于 1000 亿美元时，将缩表状态判为阶段 2。

## 9. 能源、商品和贵金属

| 数据 | 原始来源 | 处理方式 | 输出 |
|---|---|---|---|
| WTI、Brent 现货 | FRED / EIA | 原样入库 | `chart_series.js` |
| Henry Hub 天然气 | FRED / EIA | 原样入库 | `chart_series.js` |
| 美国汽油零售价 | FRED / EIA | 原样入库 | `chart_series.js` |
| 美国原油产量 | FRED / EIA | 原样入库 | `chart_series.js` |
| COMEX 黄金 | Yahoo Finance `GC=F` | 历史收盘价 | `chart_series.js` |
| 白银 | Yahoo Finance `SI=F` 或 SLV ETF | 历史/快照 | `chart_series.js`、`live_quotes.json` |
| 铜 | Yahoo Finance `HG=F` | 实时快照 | `live_quotes.json` |
| 黄金、白银 ETF | Yahoo Finance `GLD`、`SLV` | 价格和份额快照积累 | `chart_series.js` |
| 铂金 ETF | Yahoo Finance `PPLT` | 历史收盘价 | `chart_series.js` |
| 沪金、沪银 | iFinD | 最近 30 日收盘价增量合并 | `chart_series.js` |
| 金银比 | 黄金价格 / 白银价格 | 按日期交集相除 | `GOLD_SILVER_RATIO` |

## 10. 股票、外汇、ETF 和加密资产

| 数据 | 原始来源 | Yahoo/iFinD 标识 | 输出 |
|---|---|---|---|
| 标普 500、纳斯达克 | Yahoo Finance | `^GSPC`、`^IXIC` | `chart_series.js`、`live_quotes.json` |
| VIX、MOVE | FRED/CBOE + Yahoo/ICE BofA | `VIXCLS`、`^MOVE` | `chart_series.js`、`liquidity.json` |
| DXY | Yahoo Finance | `DX-Y.NYB` | `chart_series.js`、`live_quotes.json` |
| 广义贸易加权美元 | FRED | `DTWEXBGS` | `chart_series.js` |
| USDJPY、EURUSD、USDCNY | Yahoo/FRED | `JPY=X`、`EURUSD=X`、`DEXCHUS` | `chart_series.js`、`live_quotes.json` |
| 比特币 | Yahoo Finance | `BTC-USD` | `chart_series.js`、`live_quotes.json` |
| 债券 ETF | Yahoo Finance | LQD、HYG、EMB、TLT | `chart_series.js` |
| 商品 ETF | Yahoo Finance | GLD、SLV、PPLT | `chart_series.js` |
| 行业 ETF | Yahoo Finance | XLK、XLF、XLE、XLV、XLI | `chart_series.js` |
| 比特币 ETF | Yahoo Finance | IBIT | `chart_series.js` |
| 恒生指数、沪深 300 ETF | Yahoo Finance | `^HSI`、`510300.SS` | `chart_series.js` |

实时快照额外保存最新价、前收、绝对涨跌、涨跌幅、行情时间和币种。本次抓取失败的单个品种沿用旧值；全部失败则不覆写旧快照。

## 11. 中国宏观与境内市场

### 11.1 东方财富宏观库

| 数据 | 东方财富报表 | 处理方式 |
|---|---|---|
| CPI 同比、CPI 指数 | `RPT_ECONOMY_CPI` | 字段映射、按日期升序 |
| PPI 同比 | `RPT_ECONOMY_PPI` | 字段映射 |
| 制造业、非制造业 PMI | `RPT_ECONOMY_PMI` | 字段映射 |
| M0/M1/M2 同比、M2 存量 | `RPT_ECONOMY_CURRENCY_SUPPLY` | 字段映射；部分管线转为万亿元 |
| GDP 累计同比和累计值 | `RPT_ECONOMY_GDP` | 字段映射 |
| 规上工业增加值同比 | `RPT_ECONOMY_INDUS_GROW` | 字段映射 |
| 新增人民币贷款和累计值 | `RPT_ECONOMY_RMB_LOAN` | 字段映射 |
| 出口、进口同比 | `RPT_ECONOMY_CUSTOMS` | 字段映射 |
| 贸易差额 | `RPT_ECONOMY_CUSTOMS` | `(出口金额 - 进口金额) / 10000`，转亿元 |
| 社会消费品零售同比 | `RPT_ECONOMY_TOTAL_RETAIL` | 字段映射 |
| 中债 2Y/5Y/10Y/30Y | `RPTA_WEB_TREASURYYIELD` | 多页抓取，保留较长日频历史 |

东方财富属于第三方汇编；底层通常来自国家统计局、人民银行、海关总署和中债，但项目并没有逐条回溯到原始官方 API。

### 11.2 中国货币网

- LPR 1Y、5Y：`cm-u-bk-currency/LprHis`。
- SHIBOR 隔夜、3M：`cm-u-bk-shibor/ShiborHis`。
- 按年度拆分请求，避免大跨度查询返回空结果。

### 11.3 iFinD 境内行情

- 上证综指、沪深 300、上证 50、中证 500、中证 1000。
- 深证成指、创业板指、科创 50。
- 沪金 AU9999、沪银 AG9999。
- 每批最多 3 个代码，取最近 30 日收盘价，按日期合并旧序列。
- 依赖 `/app/.agents/plugins/ifind`，普通 GitHub Actions 环境无法运行。

### 11.4 中国相关衍生指标

- 中美 10Y 利差：美国 10Y 减中国 10Y，按日期交集计算。
- 美国对华贸易差额：美国自华进口减对华出口。
- M1/M2 剪刀差目前可由已入库序列进一步计算，但仓库未形成独立统一序列。

## 12. CFTC 持仓与拥挤度

| 市场 | 原始来源 | 处理方式 |
|---|---|---|
| 黄金、白银、铂金、钯金 | CFTC 年度 COT 历史 ZIP | 非商业多头减非商业空头 |
| 铜 | CFTC COT | 同上 |
| 美元指数、日元 | CFTC COT | 同上 |
| 标普 E-mini、比特币 | CFTC COT | 同上 |
| 2Y、10Y、30Y 美债期货 | CFTC COT | 同上 |

进一步计算：

- 当前净持仓在 2019 年以来样本中的分位。
- 13 周净持仓变化。
- 13 周变化在历史样本中的分位。
- 黄金、白银、铂金、钯金生成贵金属杠杆/拥挤度卡片。
- GLD、SLV ETF 份额由 Yahoo 每日积累；达到至少 26 个观察点后才标记为可用。

产物：`chart_series.js` 和 `cftc.json`。

## 13. 经济日历与数据发布面板

### 13.1 日历源

| 来源 | 覆盖范围 | 提供字段 | 处理方式 |
|---|---|---|---|
| 金十 WebSocket | 过去约 62 天至未来 13 天 | 前值、预期、公布值、星级、大事 | 解码协议，转北京时间；中国宏观公布值持续积累 |
| Forex Factory XML | 本周 USD/CNY 中高重要性事件 | 预期、前值、公布值 | GMT 转北京时间，英文名称映射中文 |
| 东方财富财经日历 | 未来 14 天多国数据、会议、休市 | 名称、时间、国家/类型 | 国家和关键词过滤，每日最多保留 10 条 |

单源失败时沿用旧文件的相应部分；三源全部失败时保留旧文件并标记 `stale=true`。产物为 `calendar_consensus.json`；金十已经公布的数据还会积累到 `j10_store.json` 并写入 `chart_series.js`。

### 13.2 官方数据发布分项

- 非农：BLS CES API，计算总量与 14 个行业分项的月度变化。
- CPI：BLS API，计算总 CPI、核心 CPI 以及 14 个分项的环比、同比和权重贡献。
- GDP：BEA NIPA 1.1.2，提取消费、投资、库存、净出口和政府贡献。
- PCE 价格：BEA NIPA 2.8.8，提取价格分项贡献。
- 实际 PCE：BEA NIPA 2.8.2，提取商品和服务对实际消费的贡献。
- 单块失败时保留旧值并添加 `stale` 标记。

产物：`release_panels.json`。

## 14. FedWatch

项目存在两套实现：

1. `fetch_fedwatch.py` 使用 Playwright 打开 CME 中文官方 FedWatch 页面，进入 QuikStrike iframe，逐会议提取 `EASE / NO CHANGE / HIKE` 概率和隐含中间利率，输出 `fedwatch.json`。
2. `fetch_release.py` 使用 CME 联邦基金期货结算价、EFFR、当前目标区间和 FOMC 日历，按 CME 方法学计算未来会议概率，输出到 `release_panels.json`。

两套数值可能有少量差异；迁移时应指定一个主源，另一个只做校验。

## 15. Polymarket 政策和政治预期

自动筛选主题：

- 美联储加息、降息、FOMC 决定和美国衰退。
- 政府关门、债务上限、关税和中美贸易。
- 特朗普支持率、2026 年参众两院控制权。
- 伊朗、核协议、霍尔木兹海峡和油价。

处理方式：

- 指定重要市场 slug 优先抓取。
- 按主题调用 Gamma 搜索。
- 从高成交量榜按关键词补充市场。
- 保存问题、结果价格、24 小时成交量和截止日期。
- 优先从 CLOB 获取日度历史价格。
- CLOB 失败时，沿用旧文件并追加当天快照。
- 每个市场最多保留约 400 天。

产物：`polymarket.json`。

## 16. 特朗普、支持率和 Truth Social

| 数据 | 来源 | 处理方式 | 输出 |
|---|---|---|---|
| 特朗普压力指数 | OCMacro | 从页面内嵌 JSON 提取历史和值 | `trump_zone.json` |
| 压力分项 | OCMacro，底层含 Yahoo/FRED/Treasury/Cleveland Fed/Silver Bulletin | 提取 approval、DGS10、MOVE、SP500、VIX 等贡献 | `trump_zone.json` |
| 支持率、反对率、净支持率 | OCMacro | 去重、按日期升序 | `trump_zone.json` |
| TACO 事件 | OCMacro | 提取威胁、回撤、强度、原因、含义和图表标注 | `trump_zone.json` |
| Truth Social 帖子 | CNN 存档 | 清除 HTML，保存时间、文本、链接和转发标记 | `truth_posts.json` |

OCMacro 的压力指数是第三方模型结果，不是官方统计；应与其底层原始序列区分。

## 17. 新闻数据

| 来源 | 接口 | 鉴权 | 保存字段 |
|---|---|---|---|
| 金十 PLUS | `flash-api.jin10.com` | `JIN10_TOKEN` | 时间、正文、重要性、VIP、标签、ID |
| 先讯 | `api.alphaheartbeat.com` | `AH_SESSION` Cookie | 时间、标题、AI logic、情绪、等级、来源、重要度 |
| CS 财经 | `api.cnthesims.com` | `CS_TOKEN` | 时间、正文、标签、ID |

处理方式：清除 HTML 标签，单源错误互不影响，输出 `news_latest.json` 和浏览器回退文件 `news_fallback.js`。页面端再进行合并去重、关键词分类、重要消息置顶和机械摘要。

安全注意：CS 财经接口在当前代码中使用 HTTP 而不是 HTTPS；不建议在新项目中原样复制携带 Token 的请求方式。

## 18. 自动图表产物

`gen_charts.py` 从 `chart_series.js` 读取统一序列库并生成：

1. 美债 2Y/10Y/30Y 收益率。
2. 10Y-2Y 与 10Y-3M 利差。
3. 10Y 实际利率与黄金。
4. DXY 与 USDJPY。
5. WTI 与 Brent。
6. SOFR-IORB。
7. ON RRP 与银行准备金。
8. 中美 10Y 利差。
9. FedWatch 加息/维持概率。
10. 金银比。

输出为 `charts/*.png` 和 `charts/catalog.json`。图表主题分为利率美债、美元外汇、商品贵金属、流动性和中国资产。

注意：当前部分图表副标题包含硬编码事件叙述，例如霍尔木兹事件；迁移为通用项目时应改成配置或从事件库动态读取。

## 19. 人工导入和人工维护内容

以下内容不能当作自动抓取数据：

| 文件/数据 | 来源 | 更新方式 |
|---|---|---|
| `views.json` | 人工或 AI 辅助研究判断 | 新观点手工写入；观点按日期冻结 |
| `forecasts.json` | 金十共识、公开媒体转述的投行预测 | 人工/半人工整理 |
| `research_notes.json` | 用户提供研报或图表 | AI 转述、提取数据点后写入 |
| `geopolitics.json` | 新华社、央视、财新、维基百科等公开报道汇编 | 人工事实整理 |
| `cs_snapshot.json` | 第三方终端 EDB 快照 | 手工导入 |
| `ht_catalog.js`、`ht_series_*.js` | 第三方终端宏观库 | 手工批量导入 |
| `ow_catalog.js`、`ow_series.js` | 外部周报/终端图表数据 | 手工批量导入 |
| `mm_catalog.js` 和 MM 序列 | MacroMicro CSV | 浏览器本地解析后上传 GitHub |
| 日报、周报 HTML 中的深度判断 | 人工/AI 生成 | 生成新报告时写入并冻结 |

这些数据应该放在单独的 `manual` 或 `research` 命名空间，避免自动数据更新后让旧观点看起来像最新结论。

## 20. 通用清洗和存储规则

当前项目已经采用或建议保留以下规则：

- 时间序列统一为 `[[YYYY-MM-DD, value], ...]`。
- 日期升序排列，同一天按键覆盖去重。
- 数值转为浮点数，跳过空字符串、`.`、`NaN` 和无法解析的值。
- 同比使用固定期数：月度通常 12 期，周度通常 52 期。
- 一阶差分用于把就业存量转为新增量。
- 跨序列运算只使用日期交集；流动性序列必要时按并集日期前向填充。
- 原始值、衍生值、信号和研究观点使用不同 ID 或不同表存储。
- 多数补充序列仅保留最近 350 点；中债等回测序列最多保留约 2600 点。
- 抓取成功时才替换现有序列；失败时尽量保留旧值。
- 关键 JSON 应保存 `updated`、`asof`、`source`、`stale` 和 `fails` 字段。
- 多数写盘使用临时文件加 `os.replace`，避免生成半截文件。
- `chart_series.js` 是浏览器可直接加载的 JS 变量，不是标准 JSON；新项目更推荐以标准 JSON 或数据库为主，再生成前端缓存。

推荐统一记录结构：

```json
{
  "id": "DFII10",
  "name": "美国10Y实际利率",
  "unit": "%",
  "provider": "FRED",
  "original_publisher": "U.S. Treasury",
  "source_level": "L1",
  "frequency": "daily",
  "updated_at": "2026-09-19T08:00:00+08:00",
  "last_observation": "2026-09-18",
  "stale": false,
  "derived": false,
  "data": [
    ["2026-09-17", 2.31],
    ["2026-09-18", 2.34]
  ]
}
```

## 21. 自动更新频率

| 任务 | 当前计划 |
|---|---|
| 宏观主数据、补充数据、中国宏观、经济日历、特朗普数据、图集 | 每日 07:40、20:40 北京时间 |
| Polymarket、CFTC、流动性、补充宏观 | 每日约 09:05 北京时间 |
| Yahoo 实时行情 | 工作日 09:12 至 23:12，约每 2 小时；周末额外一次 |
| CME FedWatch | 工作日约每 2 小时 |
| 新闻快照 | 每 30 分钟 |
| Truth Social/特朗普数据 | 美东白天每小时，夜间约每 5 小时 |
| BLS/BEA 数据发布面板 | 工作日发布后和次日补刷 |

## 22. 迁移到新项目时的推荐边界

建议拆成四层：

```text
raw_data       原始抓取：官方或市场源返回的数据
derived_data   同比、差分、利差、比值、分位数、Z-score
signals        人工预设阈值触发的机械信号
research_views 人工或 AI 编写的判断、证伪条件和配置建议
```

不建议把 `views.json`、日报 HTML 中的结论或关键词新闻摘要混进原始数据层。它们不是从数据源直接获取的事实，而是人工规则或研究判断。
