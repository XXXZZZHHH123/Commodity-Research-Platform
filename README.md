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
python -m tin.jobs backfill --days 14       # 回补近 14 天上期所数据与 VIX
python -m tin.jobs enter SMM.SN.spot.1 406000 --as-of "2026-09-18 11:30" --by 张三 --note "SMM 1#锡均价"
uvicorn tin.web.app:app --host 0.0.0.0 --port 8765
pytest
```

## 定时任务（内网服务器 crontab）

```cron
30 16 * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
0  8  * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
```

## 数据源

| 数据 | 来源 | 方式 |
|---|---|---|
| 沪锡全合约行情（收盘价、结算价、成交、持仓） | 上期所 `kx{日期}.dat` | 自动 |
| 注册仓单（日） / 交易所库存（周） | 上期所仓单日报 / 库存周报 | 自动 |
| 人民币中间价 | 外汇交易中心 | 自动（接口只给当日） |
| VIX | CBOE 官方 CSV | 自动 |
| SMM 现货、社库、TC、开工率、LME、ICDX、SOX、SPX、海关 | 订阅 / 授权 / 月度 | **人工录入**（页面「指标 → 人工录入」） |

订阅与授权数据一期不写任何抓取代码（00 §7.1）。

## 目录

```
PRD/            需求文档（00–04）与参考资料
docs/           开发计划与进度
seeds/          指标登记表、锡判断 v1 种子
src/tin/        caliber 口径字典 · ingest 取数 · compute 派生与守卫 · judgments 判断
                export 看板与快照 · web 页面 · jobs 命令行
tests/          fixtures 为 2026-09-18 的真实接口响应
```
