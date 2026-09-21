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
pytest
```

服务器直连不到的数据源，走离线搬运（见「数据源」一节）：

```bash
python -m tin.jobs snap-raw  --out raw --days 3   # 在能上网的机器上下载原件，不碰数据库
python -m tin.jobs import-raw --raw raw --days 7  # 把 raw/ 拷到服务器后回放入库，可重复执行
```

## 定时取数

取数跑在服务器自己身上，由 systemd timer 调度（工作日 08:00 与 16:30，见
`deploy/container/systemd/`）。16:30 那次取当日行情与仓单，08:00 那次主要补隔夜的
FRED 与 VIX——当天的上期所文件那会儿还没挂出来，属正常「未发布」。

不走部署脚本的机器用 crontab 等价：

```cron
30 16 * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
0  8  * * 1-5  cd /path/to/tin_display && /path/to/envs/tin/bin/python -m tin.jobs daily >> logs/daily.log 2>&1
```

取数不依赖 GitHub Actions：服务器连不上 GitHub，这条路走不通，理由与推演见
[`docs/自动取数方案与否决记录.md`](docs/自动取数方案与否决记录.md)。

## GitHub CI/CD 与内网部署

仓库提供 GitHub Actions + 内网 self-hosted runner 的部署方案。推送到 `main` 或 `cicd` 后先在
GitHub 托管 runner 上执行测试，成功后由公司 Linux 主机上的 runner 主动领取部署任务；
不需要向公网开放 SSH、数据库或应用端口。首次安装与运维步骤见
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

如果内网主机无法访问 GitHub，流水线会生成包含 Python 3.12、全部依赖和应用代码的
离线容器包。服务器只需 Docker，无需安装 Python 或运行 GitHub runner；Nginx 在宿主机
一次性安装，后续应用产物不重复携带，见
[`docs/OFFLINE_DEPLOYMENT.md`](docs/OFFLINE_DEPLOYMENT.md)。

## 数据源

| 数据 | 来源 | 方式 |
|---|---|---|
| 沪锡全合约行情（收盘价、结算价、成交、持仓） | 上期所 `kx{日期}.dat` | 自动 |
| 注册仓单（日） / 交易所库存（周） | 上期所仓单日报 / 库存周报 | 自动 |
| 人民币中间价 | 外汇交易中心 | 自动（接口只给当日） |
| VIX | CBOE 官方 CSV | 自动 |
| 美债、实际利率、美元、SPX、SOX、信用、流动性、商品与美国周期 | FRED CSV | 自动 |
| SMM 现货、社库、TC、开工率、LME、ICDX、SOX、SPX、海关 | 订阅 / 授权 / 月度 | **人工录入**（页面「指标 → 人工录入」） |

服务器所在内网若访问不到境外源（FRED、CBOE 占库内观测的绝大多数），用 `snap-raw` /
`import-raw` 离线搬运：在能上网的机器上抓原件，拷进服务器回放入库。回放走与在线采集
完全相同的 `record()` 闸门，已由 `tests/test_raw_replay.py` 断言两者逐行一致。

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
