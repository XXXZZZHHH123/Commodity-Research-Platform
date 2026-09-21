# 大宗商品研究工作台 · 一期（锡）

研究员的判断 + 自动取得的数据 → 每日比对。需求见 `PRD/`，开发计划与进度见 `docs/开发计划_一期.md`。

## 环境

```bash
conda activate tin                    # Python 3.12
pip install -r requirements.txt && pip install -e .
```

数据库默认 `data/tin.db`（SQLite）。切 PostgreSQL：加装 `psycopg[binary]`，设 `TIN_DATABASE_URL=postgresql+psycopg://...`。

## 常用命令

```bash
python -m tin.jobs init                     # 建库 + 导入指标登记表与锡判断 v1
python -m tin.jobs daily                    # 取数 → 计算派生值 → 导出当日 JSON 快照
python -m tin.jobs fetch --date 2026-09-18 --only fred_macro  # 单独更新宏观序列
python -m tin.jobs backfill --days 14       # 回补近 14 天上期所数据与 VIX
python -m tin.jobs enter SMM.SN.spot.1 406000 --as-of "2026-09-18 11:30" --by 张三 --note "SMM 1#锡均价"
uvicorn tin.web.app:app --host 0.0.0.0 --port 8765
# 批量导入：/sn/entry 页「下载导入模板」→ 填好后「批量导入 Excel」→ 预览确认 → 入库
python -m tin.jobs snap-raw --out .rawstore/raw --days 3   # 下载官方源原件留证（不入库，GitHub Actions 跑的就是它）
python -m tin.jobs import-raw --raw .rawstore/raw --days 7 # 回放原件入库并计算派生值，可重复执行
pytest
```

## 定时任务（内网服务器 crontab）

```cron
30 16 * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
0  8  * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
```

## 自动取数（GitHub Actions）

`.github/workflows/` 下三个 workflow：`ci` 跑测试与合规断言，`probe-sources` 手动探测各官方源在
GitHub runner 上的可达性，`fetch-raw` 每个交易日定时下载原件并提交到 `data-raw` 分支。

Actions 只负责「抓到并留证」，**不碰数据库**——事实库里有人工录入的授权数据，不能出网。
本机再用 `import-raw` 回放原件入库，结果与当天在线跑 `daily` 等价，且可重复执行。
方案与落地步骤见 `docs/GitHub_Actions自动取数方案.md`。

## 数据源

| 数据 | 来源 | 方式 |
|---|---|---|
| 沪锡全合约行情（收盘价、结算价、成交、持仓） | 上期所 `kx{日期}.dat` | 自动 |
| 注册仓单（日） / 交易所库存（周） | 上期所仓单日报 / 库存周报 | 自动 |
| 人民币中间价 | 外汇交易中心 | 自动（接口只给当日） |
| VIX | CBOE 官方 CSV | 自动 |
| 美债、实际利率、美元、SPX、SOX、信用、流动性、商品与美国周期 | FRED CSV | 自动 |
| SMM 现货、社库、TC、开工率、LME、ICDX、SOX、SPX、海关 | 订阅 / 授权 / 月度 | **人工录入**（页面「指标 → 人工录入」） |

订阅与授权数据一期不写任何抓取代码（00 §7.1）。

宏观页 `/sn/macro` 展示趋势、最新值、变化与来源台账。`DATA_SOURCES_AND_PROCESSING.md`
中需要密钥或专有插件的数据源会明确标记为「需凭证」，不会以占位数据冒充已接入。

## 目录

```
PRD/            需求文档（00–04）与参考资料
docs/           开发计划与进度
seeds/          指标登记表、锡判断 v1 种子
src/tin/        caliber 口径字典 · ingest 取数 · compute 派生与守卫 · judgments 判断
                export 看板与快照 · web 页面 · jobs 命令行
tests/          fixtures 为 2026-09-18 的真实接口响应
```
