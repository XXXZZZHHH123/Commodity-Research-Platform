# 05 · Excel 导入导出与通用模板方案

> 版本 v2.2 ｜ 2026-09-20 ｜ 上级文档 `PRD/00_总控.md`、`PRD/01_一期PRD_看板与报告.md`、`PRD/04_附录_数据字典与设计规范.md`
> 适用范围：大宗商品研究工作台（锡 SN 及后续扩面金属品种）

---

## 1. 背景与核心原则

### 1.1 现状与痛点

1. **录入效率瓶颈**：当前系统仅提供 `/sn/entry` 单条人工录入通道。第三方商业报价单（SMM 现货与升贴水、Mysteel 精矿 TC 与开工率）、海外库存与海关月报多为 Excel 表格。单条手动打字录入几十天历史数据或数十个指标耗时冗长，极易发生打字错漏。
2. **导出灵活性与合规缺失**：当前仅在品种页提供硬编码的 `/sn/contracts.csv` 合约表导出，无法按需组合指标列；且导出的文件缺乏商业数据合规标识、缺乏对“阻断不出数”状态的批注保护，甚至可能发生假数据前向填充或过期研判外泄。
3. **重复配置摩擦高**：研究员在晨报速递、周度供需平衡表、跨期套利等不同研究场景下需要反复重新勾选指标，需要一套轻量、确定性、可复用的导出模板机制。

### 1.2 系统底层不变量（严守守则）

1. **事实层闸门不妥协（P2 / P3 原则）**：所有批量导入数据必须经过 `tin.ingest.record.record()` 统一网关，强制绑定 `series_id`、`as_of`、`caliber`、`source` 与录入人信息，口径冲突或未登记指标严禁直接落库。
2. **严禁人工导入篡改自动采集指标**：仅允许人工录入类指标（`fetch_mode == 'manual'`）通过 Excel 导入；自动采集类指标（交易所行情、外汇中间价、VIX）绝对禁止人工写入，违者直接阻断，守住事实底座权威性。
3. **严禁系统替人编造时点（P2 守卫）**：日频数据补齐截点必须在导入预览中显式向研究员展示、允许整列微调，并在入库 `note` 中永久留痕。
4. **不出数保护（FR-5.2 原则）**：派生计算处于 `blocked` 状态时，导出 Excel 必须留空且附带错误批注，严禁输出看似正常的数值或静默空白。
5. **禁止前向填充假数据（04 §5.3 原则）**：周频与月频指标在日频导出宽表中，非发布日必须留空，绝对禁止沿用前值（ffill）。
6. **严守商业数据授权合规（04 §8 原则）**：SMM、Mysteel 等商业订阅数据带出系统必须带合规警示，并写入审计日志（`audit_log`）。
7. **确定性状态机**：废除隐式后台无感记忆，单层确定性模板选择，微调显式标记 `[已修改]`。
8. **服务端权威校验防绕过**：导入确认实行服务端授权缓存（`preview_id` 机制），严禁前端直传未经校验的裸数据绕过人工防线；commit 阶段必须执行二次并发状态比对与覆盖参数完整性校验。

---

## 2. 依赖与数据模型底座

### 2.1 依赖声明

环境内已有 `charset_normalizer`（通过 httpx / requests 引入），无需额外引入 `chardet`。在 `requirements.txt` 与 `pyproject.toml` 中显式补齐 Excel 读写引擎：
```toml
openpyxl >= 3.1.5    # Excel 2007+ (.xlsx) 读写与样式、批注引擎
xlrd >= 2.0.1        # 传统 Excel 97-2003 (.xls) 读取引擎
charset_normalizer >= 3.3.0  # CSV 文件编码自动探测（UTF-8, UTF-8 BOM, GB18030）
```

### 2.2 数据模型定义与迁移

统一采用系统底层约定的 `UTCDateTime` 和 `JSONType`（位于 `src/tin/db.py`），通过 `alembic revision --autogenerate` 生成迁移脚本。

#### 1. `indicators` 表扩充与历史数据回填
消除 `app.py` 中硬编码的 `DEFAULT_ENTRY_TIME`，在 `indicators` 表新增字段。
由于 `seed_indicators()` 仅在指标缺失时插入、不覆盖已有记录，**迁移脚本必须包含显式 SQL 回填**：
```python
# src/tin/models/facts.py
default_entry_time: Mapped[time | None] = mapped_column(Time, nullable=True)
```
Alembic 迁移脚本执行内容：
```python
op.add_column("indicators", sa.Column("default_entry_time", sa.Time(), nullable=True))
# 显式回填已有 4 个指标的默认截点时刻
op.execute("UPDATE indicators SET default_entry_time = '11:30:00' WHERE series_id = 'SMM.SN.spot.1'")
op.execute("UPDATE indicators SET default_entry_time = '11:00:00' WHERE series_id = 'MYSTEEL.SN.TC.YN40'")
op.execute("UPDATE indicators SET default_entry_time = '15:00:00' WHERE series_id = 'MYSTEEL.SN.stock.social'")
op.execute("UPDATE indicators SET default_entry_time = '19:00:00' WHERE series_id = 'LME.SN.stock'")
```

#### 2. `export_templates` 导出模板表（字段类型明确定义）
遵循工程约定，使用自定义 `UTCDateTime` 和 `JSONType`。
加入**部分唯一索引**，确保同一品种下只允许存在一个 `is_default=True` 的模板。
针对不同字段类型在导出分派时的歧义，**模板的 columns 结构强制显式指定 kind**：
```python
# src/tin/models/system.py
from datetime import datetime
from sqlalchemy import Boolean, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column
from tin.db import Base, JSONType, UTCDateTime

class ExportTemplate(Base):
    __tablename__ = "export_templates"
    __table_args__ = (
        Index(
            "uq_export_template_default",
            "variety",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default = TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variety: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # columns 格式明确包含 kind:
    # [{"field": "trade_date", "label": "交易日", "kind": "meta"},
    #  {"field": "SHFE.SN.main.settle", "label": "主力结算价", "kind": "observation"},
    #  {"field": "BASIS", "label": "结算价基差", "kind": "derived"},
    #  {"field": "version", "label": "判断版本", "kind": "judgment"}]
    columns: Mapped[list] = mapped_column(JSONType)
    date_range: Mapped[str] = mapped_column(String(32), default="recent_30_trade_days")
    start_date: Mapped[str | None] = mapped_column(String(10))  # 自定义区间 YYYY-MM-DD
    end_date: Mapped[str | None] = mapped_column(String(10))
    sort_order: Mapped[str] = mapped_column(String(16), default="desc")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
```

#### 3. `import_previews` 导入暂存表（服务端权威缓存与双重清理）
避免多进程部署下内存缓存不同步，暂存结果落库 SQLite 临时表，TTL 为 30 分钟：
```python
class ImportPreview(Base):
    __tablename__ = "import_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID4
    variety: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(256))
    # payload 只存入库所需的最小集合，不保留原始单元格全文或整表副本：
    # {"columns": [{col_key, header, series_id, match_kind, variants, as_of_time, note}],
    #  "rows": [{row_key, col_key, series_id, as_of, value, category, reason, old_value}]}
    payload: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
```
清理策略（双重清理）：
1. 每次调用 `/api/sn/import/preview` 时触发清理 `DELETE FROM import_previews WHERE expires_at < :now`；
2. 每日定时调度 `python -m tin.jobs daily` 中增加清理任务，确保无人导入时不会残留包含商业数据的过期记录。

---

## 3. Excel 智能导入引擎设计

### 3.1 宽表过闸门与服务端防绕过架构

#### 1. 服务端权威缓存（`preview_id` 机制）
- **实施方案**：
  1. `/api/sn/import/preview` 完成文件解析与校验后，将权威解析结果写入 `import_previews` 表，生成唯一 `preview_id` 返回前端；
  2. `/api/sn/import/commit` **严禁接收裸数据行**，仅接收：
     - `preview_id: str`
     - `selected_row_keys: list[str]`（用户勾选入库的行标识集合）
     - `column_overrides: dict[str, dict]`（用户确认的多品位 series_id 选定、整列时点调整、整列来源说明）
     - `actor: str`（操作人姓名）
  3. 服务端从 `import_previews` 读取权威数据，应用覆盖配置后统一调用 `record()` 入库。这使得 API 层的人工审核防线绝对不可绕过。

#### 2. 宽表来源说明与录入人防造假
- **录入人身份**：`entered_by` 强制绑定当前上传操作者（`actor`，必填项）。表内即便存在“录入人”列，也仅作为补充信息并入 `note`，严防假借他人签名。
- **两级来源说明**：
  - **全局默认说明**：在抽屉顶部填写（如 `批量导入自 2026-09-20 现货周报.xlsx`）；
  - **单列单独覆盖**：在识别表头中允许逐列修改（如第 B 列设为 `SMM 官网定盘`，第 C 列设为 `Mysteel 电话调研`）。

#### 3. CSV 中文编码自动探测
解析 `.csv` 时执行探测链：
1. 检查是否存在 UTF-8 BOM（`codecs.BOM_UTF8`）；
2. 使用 `charset_normalizer` 采样前 10KB 探测编码；
3. 降级尝试：`utf-8-sig` $\to$ `gb18030` $\to$ `utf-8`。

### 3.2 严禁人工导入自动采集指标

- `/sn/entry` 明确约束“只能为人工录入指标录入数值”。导入引擎必须严格对齐这一闸门。
- **规则**：若待导入列匹配到的指标其 `fetch_mode != 'manual'`（如 `SHFE.SN.main.settle`、`FX.USDCNY.mid`、`MACRO.VIX`）：
  - 系统禁止该列入库；
  - 该列下所有行直接归入「04 异常未导入」清单；
  - 明确提示原因：`自动采集指标不接受人工导入，防止篡改交易所或官方权威行情`。

### 3.3 严防编造时点（P2 守卫）

- 宽表若只填日期（如 `2026-09-18`），**严禁系统静默在后台补齐时刻**。
- **规则**：
  1. 导入预览界面逐列显示系统建议的时点：`该列将按 11:30 记为 as_of`（取自 `indicators.default_entry_time`）；
  2. 允许研究员在预览界面就地修改整列时刻；
  3. 入库时，系统在写入观测记录的 `note` 中强制追加标记：`[截点补齐: 11:30] {用户原说明}`，实现永久留痕。

### 3.4 口径与品位防呆

#### 1. 匹配优先级与往返无损保障
为了让系统自导出的文件能顺利再导入（往返无损），确立严格的表头匹配优先级：
- **优先级 1（显式代码精确对齐）**：若表头包含 `[{series_id}]`（如 `云南40%精矿TC [MYSTEEL.SN.TC.YN40]`），正则提取中括号内的代码直接对齐。此匹配属于精确绑定，**不走模糊匹配，绝对不触发多变体警告**，保证往返自导入 100% 顺畅。
- **优先级 2（名称模糊匹配）**：仅在表头不含 `[...]` 代码标签时启用。此时严格受下方品位防呆规则约束。

#### 2. 相近品位指标防呆（40% vs 60% TC）
- **安全规则**：
  - 在优先级 2（纯名称匹配）下，只要指标在系统中存在多口径变体（不同品位、实物吨/金属吨），**系统绝对禁止自动通过匹配**；
  - 标记该列为 `WARN_MULTIPLE_CALIBER_VARIANTS`，预览界面强制要求研究员从下拉菜单中手动选定口径；若未手动选定，提交按钮保持置灰阻断。

#### 3. 数量级异常判定算法 (`WARN_MAGNITUDE_OUTLIER`)
- **判定算法**：
  - 查询库内该 `series_id` 最近 20 条历史观测值；
  - 若有效历史记录数 $< 5$（冷启动或新指标）：**跳过检查，不告警**，杜绝首次初始化时伪警报刷屏；
  - 计算历史绝对值中位数 $M = \text{median}(|value|)$；
  - 若 $M == 0$（变动量序列或净额序列）：**跳过检查**，避免除以零或绝对倍数失效；
  - 当 $M > 0$ 且 $|v| > 0$ 时，计算对数偏离：$|\log_{10}(|v|) - \log_{10}(M)|$。若差值 $\ge 2.0$（相差达 2 个数量级，即超过 100 倍或低于 0.01 倍），触发 `WARN_MAGNITUDE_OUTLIER`；
  - 提示文本：`数值 {v} 与历史中位数 {M} 相差达 2 个数量级，请核对是否发生单位混淆（如元与万元、吨与万吨）`。

### 3.5 四类识别清单分类体系

解析引擎对表格数据进行分类，在前端以独立标签页展示：

| 清单类别 | 判定标准 | 系统行为 | 交互设计 |
|---|---|---|---|
| **01 可导入 (Ready)** | 库内无此 `series_id` + `as_of`，口径一致且数值合法 | 待入库 | 默认全选，支持单条取消 |
| **02 将产生修订 (Revision)** | 库内已存在该时点记录，但**数值不一致** | 若提交将递增 `revision`，保留历史 | **默认不勾选**！明确展示 `原值 -> 新值`，强制研究员核对后显式确认 |
| **03 相同忽略 (Skip)** | 库内已存在且数值完全一致 | 不入库，不递增 revision | 灰色次要文本汇总展示总条数，不打扰用户 |
| **04 异常未导入 (Errors)** | 自动采集指标、指标未登记、日期无法解析、非数字占位符 | 跳过该单元格/该行 | 标红展示行号、列名、原始值、失败原因 |

### 3.6 提交阶段二次校验与防并发陈旧处理

在研究员查看预览抽屉的几分钟窗口期内，可能存在定时采集任务写入或他人录入。
`/api/sn/import/commit` 执行时，服务端做两道终审守卫：
1. **并发陈旧检测 (Stale Rows Guard)**：
   - 逐行重新比对库内最新状态；
   - 若原本在预览中判定为“01 可导入”的行，此时库内已存在不同数值（已悄悄转为“02 将产生修订”），系统立即跳过该行写入；
   - 响应中返回 `stale_rows` 列表并提示：`部分数据在预览后已被后台更新，已自动拦截改写，请刷新重试`。
2. **`column_overrides` 改绑重校验**：
   - 若用户通过 `column_overrides` 将某列改绑到了新的 `series_id`；
   - 服务端必须重新验证：新指标存在、状态为可用、`fetch_mode == 'manual'`，并用新指标的历史数据重跑 `WARN_MAGNITUDE_OUTLIER`。

### 3.7 批量导入多日派生重算机制

- **事实澄清与计算规则**：
  派生公式严格仅读取 `observations` 事实表（例如 `STOCK_CHG_D` 取前一交易日的观测记录），不依赖 `derived` 表内已有结果。因此重算顺序与数学结果完全无关。
- **重算调度**：
  - 提取本次实际入库记录所涉及的所有交易日，去重并**按时间升序排序**（正序执行仅为保持日历推进的日志可读性与审计追溯便利）；
  - 逐日调用 `compute_day(s, trade_date)`；
  - 返回响应中包含 `recalculated_dates` 清单。

### 3.8 下载标准模板

- 提供接口 `GET /api/sn/import/template`；
- 动态生成 `.xlsx` 标准导入模板，表头格式为 `{指标名} [{series_id}]`（如 `SMM 1# 锡现货 [SMM.SN.spot.1]`），第 2–3 行附带口径提示与示例时点。

---

## 4. 规范 Excel 导出与合规审计

### 4.1 导出抽屉的交互布局原型

遵循一期低摩擦（P5）与全站设计语言一致性原则，采用**右侧滑出抽屉（Slide-over Drawer）**形态，宽度统一为 `max-w-xl`：

```
┌─────────────────────────────────────────────────────────────────┐
│ 自定义数据导出抽屉                                          [×] │
├─────────────────────────────────────────────────────────────────┤
│ 模板预设：                                                      │
│ [ 晨报核心 (默认) · 已修改 ▾ ]  [撤销修改]  [覆盖保存]  [另存为] │
├─────────────────────────────────────────────────────────────────┤
│ 已选导出字段与列顺序（按键调序为主，支持鼠标拖拽）：            │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ [ 1. 交易日 ]                                               │ │
│ │ [ 2. 主力结算价 ◀ ▶ × ]  [ 3. SMM现货 ◀ ▶ × ]              │ │
│ │ [ 4. 结算价基差 ◀ ▶ × ]  [ 5. 上期所仓单 ◀ ▶ × ]            │ │
│ └─────────────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────────┤
│ 指标字段选取矩阵（默认展开常用组，次用组折叠降噪）：            │
│                                                                 │
│ ▼ 01 基础行情 [全选/清空] (2/5)                                 │
│   [x] 交易日 (trade_date)   [x] 主力结算价 (SHFE.SN.main.settle)│
│   [ ] 主力收盘价 (close)    [ ] 成交量 (volume)  [ ] 持仓量 (oi)│
│                                                                 │
│ ▼ 02 现货与基差 [全选/清空] (2/2)                               │
│   [x] SMM 1# 锡现货 (SMM.SN.spot.1)                             │
│   [x] 结算价基差 (BASIS)                                        │
│                                                                 │
│ ▶ 03 供需与库存 (1/4) [点击展开]                                │
│                                                                 │
│ ▶ 04 宏观外盘 (0/4) [点击展开]                                  │
│                                                                 │
│ ▶ 05 阈值与研判 (0/2) [点击展开]                                │
├─────────────────────────────────────────────────────────────────┤
│ 【内部参考】本表含 SMM / Mysteel 商业授权数据，严禁对外复制流转。│
│ （琥珀色背景，严禁使用暗红色破坏证伪/过期视觉语义）              │
├─────────────────────────────────────────────────────────────────┤
│ 数据区间：[ 最近 30 个交易日 ▾ ]  数据排序：[ 日期倒序 (最新) ▾ ]│
│ 导出人姓名：[ 张研究员 ]（必填，用于合规审计）                  │
├─────────────────────────────────────────────────────────────────┤
│                                            [导出 Excel (.xlsx)] │
└─────────────────────────────────────────────────────────────────┘
```

> **字段代码准确性注记**：
> - 库存序列：`SHFE.SN.stock.weekly`（周频）；
> - 汇率中间价：`FX.USDCNY.mid`；
> - 纳斯达克半导体：`FRED.NASDAQSOX`；
> - 库存日变动：`STOCK_CHG_D`；
> - 基差率（`BASIS_RATE`）目前未实现，一期仅支持 `BASIS`, `SPREAD_M1M2`, `STOCK_CHG_D`。

### 4.2 导出排版、冻结与往返无损闭环

1. **机器可读性与排版规范**：
   - **第 1 行强制为纯表头**：列头命名规范为 `{中文名} [{series_id}]`（如 `主力结算价 [SHFE.SN.main.settle]`）；
   - **冻结首行首列**：设置 `ws.freeze_panes = "B2"`；
   - **数据行自第 2 行开始**：纯粹的数据表格，确保任何第三方程序以及系统自身解析器可直接读取。
2. **往返无损闭环 (Round-trip Integrity)**：
   - 当研究员将导出的 Excel 修改后重新导入时，导入引擎优先通过正则表达式 `\[([A-Za-z0-9_.]+)\]` 提取代码，直连绑定库内 `series_id`，杜绝一切歧义。
3. **指标停用或删除时的优雅降级**：
   - 模板引用的指标在数据库被标记为停用或删除时，**系统不报错、不中断**；
   - 界面提示 `[已停用]`；
   - 导出的 Excel 自动剔除该列，并在 Sheet 2 `口径与合规说明` 中注明：`模板中的指标 {series_id} 已停用，本次导出已略过`。

### 4.3 出数保护与三类细分批注

派生与观测指标单元格严禁静默留空或填 0，必须通过 Excel 批注（Cell Comment）明确保留现场状态：

| 状态类型 | 判定条件 | 单元格数值 | Excel 批注内容规范 |
|---|---|---|---|
| **阻断不出数** | `derived.status` 为 `blocked_*` | 强制留空 (None) | `[阻断不出数: 口径/时点不一致] {derived.note}` |
| **尚未计算** | 该交易日尚未跑 `compute_day` | 强制留空 (None) | `[未计算] 该交易日尚未执行派生计算` |
| **数据缺失** | 基础观测值未取到 | 强制留空 (None) | `[数据缺失] 预期更新时点: {expected_by}` |

### 4.4 低频指标在日频表中的对齐与不填假数

1. **绝对禁止前向填充（04 §5.3 守卫）**：
   - 周频指标（`SHFE.SN.stock.weekly`）与月频指标在非发布日的日频行中**必须为空单元格**，严禁执行 `ffill`。
2. **非交易日顺延对齐机制**：
   - 海关进出口等月度指标 `as_of` 为月末自然日（如周六周日或法定假日）；
   - 规则：若 `as_of` 为非交易日，该数据行**顺延对齐至下一个交易日 (Next Trade Date)**；
   - 单元格附加批注：`[顺延对齐] 原始数据时点 as_of 为 2026-09-30 (非交易日)，顺延对齐至本交易日`。

### 4.5 导出研判强制绑定过期标记

- 研判列输出必须为复合文本，杜绝孤立基调外泄：
  - 若已过期：`偏多 (基于 v1，已过期 12 天)`；
  - 若生效中：`偏多 (v1，生效中)`。

### 4.6 商业数据合规与审计防空（04 §8 守卫）

1. **分层合规防伪体系**：
   - **Sheet 2 统一命名与显式警示**：工作簿 Sheet 2 统一命名为 **`口径与合规说明`**，首部以琥珀色/警示边框标注商业授权许可条款与口径字典；
   - **打印页眉注入**：使用 openpyxl 注入打印页眉 `ws.oddHeader.center.text = "【内部投研参考 严禁外发】本表含 SMM / Mysteel 商业授权数据"`；
   - **导出抽屉常驻警示**：在导出按钮上方常驻合规警示行，**严格采用琥珀色令牌 (`bg-[var(--amber-soft)] text-[var(--amber)] border-[var(--amber)]/30`)**，严禁使用暗红，确保红色语义仅留给证伪触发与计算阻断。
2. **审计日志必填防空**：
   - 一期尚未引入登录体系（M4），导出抽屉中强制提供 `导出人姓名` 输入框（默认从 localStorage 预填上次操作人）；
   - `/api/sn/export/excel` 接口强制校验 `actor` 非空，落库写入 `audit_log`（记录操作人、导出商业指标集、时间、数据行数）。

---

## 5. 统一 API 与数据流规格

| 路径 | 方法 | 功能描述 | 请求参数 / Body | 响应结构 |
|---|---|---|---|---|
| `/api/sn/import/template` | GET | 下载带代码的标准空白导入模板 | 无 | 流式 `.xlsx` 文件 |
| `/api/sn/import/preview` | POST | 上传文件试算并暂存权威数据 | `file: UploadFile`, `actor: str` | `{preview_id, ready_count, revision_count, skipped_count, error_count, summary}` |
| `/api/sn/import/commit` | POST | 校验并提交入库触发重算 | `{preview_id: str, selected_row_keys: list[str], column_overrides: dict, actor: str}` | `{committed: int, revisions: int, stale_rows: list, recalculated_dates: [...]}` |
| `/api/sn/export/excel` | POST | 规范导出 Excel | `{columns: [...], date_range: str, start_date: str, end_date: str, sort_order: str, actor: str}` | 流式 `.xlsx` 文件（带批注与字典 Sheet） |
| `/api/sn/export/templates` | GET/POST/DELETE | 模板增删查改 | 模板 JSON Payload | 模板对象清单 / 操作结果 |

---

## 6. 里程碑与量化验收指标

### 6.1 研发落地顺序

1. **M-EXCEL-1：依赖与数据底座迁移**：声明 `openpyxl` 与 `xlrd`；完成 `indicators.default_entry_time` 增加与 SQL 数据回填；完成 `export_templates` 表与部分唯一索引建立；建立 `import_previews` 暂存表。
2. **M-EXCEL-2：Excel 智能导入引擎（优先攻坚）**：实现 `excel_importer.py`、`preview_id` 服务端暂存与 TTL 清理、多品位防呆与代码精确对齐、四类清单生成、并发陈旧检测、模板下载与 `record()` 事务入库、多日升序重算。
3. **M-EXCEL-3：规范 Excel 导出引擎与合规审计**：实现 `excel.py`、`freeze_panes`、出数保护三类批注、非交易日顺延对齐、合规打印页眉与 Sheet 2 `口径与合规说明`、`audit_log` 审计记录。
4. **M-EXCEL-4：极简导出面板与模板管理**：单层模板切换、已选胶囊调序、停用指标优雅降级、联调验收。

### 6.2 严格量化验收标准

- **导出性能**：导出 500 个交易日 $\times$ 20 个指标列的完整 Excel 文件（含批注与 Sheet 2 字典），生成响应时间 $\le$ 2.0 秒。
- **导入性能（常规规模）**：上传包含 5,000 个单元格的宽表文件，服务端解析、校验、写入暂存表并返回 `preview_id` 的时间 $\le$ 3.0 秒。
- **导入性能（上限规模）**：上传达到硬上限 120,000 单元格的文件，返回 `preview_id` 的时间 $\le$ 15.0 秒；解析期间前端进度动画必须持续运行，不得出现无反馈的静默等待。
- **多日重算性能**：单次导入 60 个交易日历史数据，触发 60 天 `compute_day` 重算的总耗时 $\le$ 3.0 秒。
- **单元格规模硬上限**：上传文件单元格总数超过 120,000 单元格直接拦截阻断，提示用户按时间分批；若识别为 SMM 终端导出格式则改走摘要确认通道（见方案 07 §8），不受此限。
- **幂等性验证**：同一份已成功入库的 Excel 再次上传并预览，100% 的有效数据行必须归入「相同忽略」清单，实际入库写入数与修订数严格为 0。
- **往返无损闭环验证**：系统导出的 Excel 文件无需任何修改，直接上传至导入引擎，指标识别匹配率必须为 100%（通过 `[series_id]` 标签无损对齐），且多品位警告数严格为 0。
- **并发防陈旧验证**：在 preview 与 commit 间隙人为插入新版本数据，commit 接口能准确识别出冲突行并归入 `stale_rows`，不发生悄悄覆盖。
- **闸门安全性验证**：直接调用 `/api/sn/import/commit` 传入非法或过期 `preview_id`，系统严格返回 400/404 阻断，无法绕过预览校验。

---

## 7. 界面契约与交互规格 (UI Contract)

### 7.1 入口精简与收敛（严格遵循 P4 / P5）

一期收敛入口数量，不替研究员预设过多路径，**全站仅设 2 个权威物理入口**：

| 功能 | 宿主页面 | 确切物理位置 | 交互形式 | 边界界定 |
|---|---|---|---|---|
| **批量导入** | `/sn/entry`（录入通道） | 页面顶部标题栏右侧主操作区 | 次要按钮：`批量导入 Excel` | 点击滑出 `#import-drawer`；原单条录入表单保持不变。 |
| **自定义导出** | `/sn`（品种总览看板） | 首屏核心统计条（数据就绪/缺口）右侧 | 次要按钮：`自定义导出 Excel` | 点击滑出 `#export-drawer`；不挤占顶部常驻导航条。 |

> **合约表导出独立界定**：
> `/sn` 区块 02 合约行情表右下角的 `/sn/contracts.csv` **完全保留原有形态**（作为秒级下载单表快照）。一期严禁在该处混合两种截然不同的数据形态（按合约排行的截面快照 vs 按日期排行的长时序宽表），不设自定义导出入口。

### 7.2 抽屉统一与前端通用化重构

1. **抽屉形态规范**：
   - 导入抽屉：`id="import-drawer"`，最大宽度 `max-w-2xl`；
   - 导出抽屉：`id="export-drawer"`，最大宽度 `max-w-xl`。
2. **代码重构契约 (`src/tin/web/static/app.js`)**：
   - **重构通用开关函数**：将原硬编码的 `toggleDrawer(open)` 重构为支持指定抽屉的通用签名 `toggleDrawer(drawerId, open)`；
   - **统一通用遮罩**：将 `#quick-drawer-backdrop` 升级为通用遮罩 `#drawer-backdrop`，点击时关闭当前活跃的抽屉；
   - **页面滚动锁定一致性**：抽屉打开时为 `document.body` 增加 `overflow-hidden`，收起时移除，修复现有快速录入抽屉滚动穿透问题。

### 7.3 破坏性操作的防误触与反馈契约

| 操作按钮 | 呈现位置与激活条件 | 确认与反馈机制 |
|---|---|---|
| **删除此模板** | 模板行右侧；选中任一已存模板时均可用（含默认模板） | 弹出二次确认对话框：`确认删除导出模板 "{name}" 吗？此操作不可逆。`（按钮：确认删除 / 取消）<br>**默认模板的删除后继规则**：若被删除的是默认模板，服务端自动将剩余模板中 `updated_at` 最新的一个置为默认；若删除后已无任何模板，回到 §7.5 的冷启动空态 `[未命名配置 (未保存)]`，避免出现「默认模板永远删不掉」的死角 |
| **覆盖保存** | 模板行右侧；**仅在模板处于 `[已修改]` 状态下高亮激活为墨绿色 (`--primary`)** | 弹出轻量确认：`确认将当前列配置覆盖保存至模板 "{name}" 吗？原模板配置将被替换。`（按钮：确认覆盖 / 取消） |
| **另存为新模板** | 模板行右侧；**未勾选任何指标时禁用 (disabled)** | 勾选指标后激活；点击弹出轻量命名输入弹窗（输入名称后点击确定保存） |
| **撤销修改** | **彻底移出底栏**，置于模板下拉框紧邻右侧；仅在 `[已修改]` 状态下出现 | 点击后即刻恢复勾选与胶囊顺序至初始状态，**并立即弹出 toast 提示：`已恢复到模板初始配置`** |

### 7.4 确定性状态机与显式反馈

- **模板未修改态 (Pristine)**：
  - 下拉框文案：`[ 晨报核心 (默认) ▾ ]`；
  - 按钮状态：`[另存为]` 激活（若有勾选），`[覆盖保存]` 置灰禁用，`[撤销修改]` 隐藏。
- **模板已修改态 (Dirty)**：
  - 只要研究员增减 Checkbox 或调整列顺序：
  - 下拉框文案更新为：`[ 晨报核心 (默认) · 已修改 ▾ ]`，左侧附带小圆点指示；
  - 按钮状态：`[撤销修改]` 显现，`[覆盖保存]` 激活为墨绿色高亮 (`--primary`)。
- **防丢失关闭拦截**：若在已修改状态下点击遮罩或关闭按钮，弹出微提示：`当前微调尚未保存，是否直接关闭？（未保存修改将被舍弃）`。

### 7.5 空态、错误态与规模上限契约

1. **冷启动空态 (0 模板)**：
   - 首次使用无任何已存模板时，下拉框显示：`[未命名配置 (未保存) ▾]`，列表为空提示 `暂无已存模板`；
   - 未勾选字段时 `[另存为]` 按钮置灰禁用，悬浮提示 `请先勾选指标字段后再保存为模板`。
2. **规模硬上限拦截**：
   - 导入文件总单元格数硬性限制为 **120,000 单元格**；
   - 超出限制直接拦截并给出明确阻断提示：`文件超出规模限制（最大支持 120,000 单元格），请分批导入或按时间切分`。

   **上限为什么是 12 万。** 实测预览耗时与规模严格线性，约 **0.105 秒 / 千单元格**、payload 约 **165 KB / 千单元格**：

   | 单元格 | 预览耗时 | payload | 预览行数 | 浏览器插入 DOM |
   |---|---|---|---|---|
   | 5 万（原上限） | 5.4 s | 8 MB | 4 万 | 250 ms |
   | **12 万（现上限）** | **12.6 s** | **20 MB** | 9.6 万 | ~480 ms |
   | 15 万 | 15.8 s | 25 MB | 12 万 | ~600 ms |
   | 41.5 万 | 44 s | 69 MB | 33 万 | 1.1 s |

   约束按硬度排序是：**① 15 秒验收线**（对应约 14 万单元格）→ **② payload 存进 `import_previews` 单行** → **③ 人还确认得过来吗**。浏览器不是瓶颈——实测 33 万行插入 DOM 只要 1.1 秒、JS 堆 381 MB，扛得住。

   真正不该再往上抬的理由是 ③：**逐行预览的价值在于抓出"这一列绑错了指标"，而几十万行没人会逐行看，实际操作只会是点全选**——预览就退化成闭眼信任，只剩成本、没有安全收益。所以更大的文件不是"放宽上限"，而是换一条按批次确认的通道。
3. **上传解析进度微反馈**：
   - 文件放入拖拽区后，区域内切换为环形微进度动画与文案：`正在解析文件结构与核验口径...`，期间锁定抽屉，解析完成后平滑过渡到四类清单。

### 7.6 字段调序按键契约（按键为主，拖拽为辅）

- **主路径按键设计**：
  - 胶囊标签格式统一为：`[ 序号. 指标名 ◀ ▶ × ]`；
  - 点击 `◀`：与前一位字段互换位置（首位置灰）；
  - 点击 `▶`：与后一位字段互换位置（末位置灰）；
  - 点击 `×`：取消该字段勾选，自动从已选序列移除；
- **渐进增强**：桌面端保留 HTML5 原生拖拽能力作为鼠标快捷补充。

### 7.7 视觉密度控制与色彩语义守卫

1. **折叠降噪**：
   - 分组 01（基础行情）与 02（现货与基差）默认展开；
   - 分组 03（供需库存）、04（宏观外盘）、05（阈值研判）默认采用 `<details>` 折叠，组标题显示计数角标（如 `供需与库存 (1/4) [展开]`），首屏视觉高度压缩 40%。
2. **色彩语义严格守卫**：
   - 严格沿用 `PRD/04_附录_数据字典与设计规范.md §5` 设计令牌；
   - 导出抽屉内的合规警示行**严禁使用暗红色**，统一使用琥珀色语义令牌：
     `bg-[var(--amber-soft)] text-[var(--amber)] border-[var(--amber)]/30`。
