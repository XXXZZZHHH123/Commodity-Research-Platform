"""SMM 终端导出工作簿的整体导入。

SMM 终端导出的 xlsx 每张表是「日期 × 指标」的宽表，前四行固定为：

    R1 指标名称   SHFE: 锡: 主力合约: 收盘价: 日度
    R2 指标Id     a10024116          ← 供应商稳定编码，作为 vendor_code 精确对齐
    R3 单位       元/吨
    R4 频率       日
    R5+ 数据      首列日期，其余为数值

因此这条通道不需要任何表头名称猜测：`指标Id` 是主键，改了显示名也不会错位。

与网页批量导入的分工：网页那条通道面向研究员手工整理的几十行表格，有 5 万单元格的硬上限；
终端导出动辄几十万条，只能走命令行。两者最终都汇入同一个 `record()` 闸门。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from tin.caliber.dictionary import ContractKind, PriceType, SpotSource, StockScope, TimeType, WeightBasis
from tin.config import SHANGHAI
from tin.schemas.caliber import Caliber
from tin.schemas.observation import IndicatorSpec, ObservationIn

HEADER_LABELS = ("指标名称", "指标Id", "单位", "频率")
SOURCE = "SMM 终端"
SOURCE_URL = "https://www.smm.cn/（终端导出，订阅授权数据，禁止外发）"

# 已登记指标沿用原 series_id，不另建一份。键是 SMM 指标Id。
OVERRIDES: dict[str, str] = {
    "s20002960": "SMM.SN.spot.1",  # SMM 1#锡-平均价，BASIS 的现货端
}

# 名称里带这些词的列不导入：SMM 已标注停用，再入库只会让缺口台账长期挂着假缺口。
DISCONTINUED = "（停）"


@dataclass
class SmmSeries:
    vendor_code: str
    name: str
    unit: str
    frequency: str
    sheet: str
    points: dict[date, float] = field(default_factory=dict)

    @property
    def discontinued(self) -> bool:
        return self.name.startswith(DISCONTINUED)

    @property
    def series_id(self) -> str:
        return OVERRIDES.get(self.vendor_code, f"SMM.{self.vendor_code}")


class SmmFormatError(ValueError):
    pass


def read_workbook(path: str | Path) -> list[SmmSeries]:
    """读出全部序列。表头不符的工作表整张跳过，不猜。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out: list[SmmSeries] = []
    seen: set[str] = set()
    try:
        for sheet in wb.sheetnames:
            rows = wb[sheet].iter_rows(values_only=True)
            head = []
            for _ in range(4):
                try:
                    head.append(list(next(rows)))
                except StopIteration:
                    break
            if len(head) < 4 or tuple(str(h[0]).strip() if h and h[0] else "" for h in head) != HEADER_LABELS:
                continue
            names, codes, units, freqs = head
            cols: dict[int, SmmSeries] = {}
            for i in range(1, len(names)):
                code = str(codes[i]).strip() if i < len(codes) and codes[i] else ""
                if not names[i] or not code or code in seen:
                    continue  # 同一 Id 在表里出现两次（短名 + 全名），只取先出现的那份
                seen.add(code)
                cols[i] = SmmSeries(
                    vendor_code=code, name=str(names[i]).strip(), sheet=sheet,
                    unit=str(units[i]).strip() if i < len(units) and units[i] else "",
                    frequency=str(freqs[i]).strip() if i < len(freqs) and freqs[i] else "",
                )
            for row in rows:
                day = row[0]
                if not isinstance(day, datetime):
                    continue
                day = day.date()
                for i, series in cols.items():
                    if i >= len(row):
                        continue
                    value = row[i]
                    if value is None or (isinstance(value, str) and value.strip() in ("", "-")):
                        continue
                    try:
                        series.points[day] = float(value)
                    except (TypeError, ValueError):
                        continue  # 文本备注列，不是数值序列
            out.extend(cols.values())
    finally:
        wb.close()
    if not out:
        raise SmmFormatError(f"{path} 里没有符合 SMM 终端格式的工作表（前四行须为 {'/'.join(HEADER_LABELS)}）")
    return out


# ---------- 口径推导 ----------

_CATEGORY_RULES = (
    ("仓单", "库存与流通"), ("库存", "库存与流通"),
    ("矿端平衡", "矿端"), ("锡矿", "矿端"),
    ("锡市平衡", "供需平衡"),
    ("强度指数", "情绪"), ("强弱指数", "情绪"),
    ("消费结构", "需求"),
    ("溢价", "价格"), ("升贴水", "价格"), ("价差", "价格"), ("价", "价格"),
    ("成交量", "价格"), ("持仓量", "价格"),
)

# 顺序即优先级："夜盘收盘价"先命中收盘价。同一价格类型下的不同场次（日盘/夜盘/10点15）
# 口径字典里没有对应维度，只能落在 note 里——跨场次比价的守卫暂时拦不住，见方案文档。
_PRICE_TYPES = (("结算价", PriceType.结算价), ("收盘价", PriceType.收盘价),
                ("平均价", PriceType.均价), ("均价", PriceType.均价),
                ("开盘价", PriceType.开盘价), ("最高价", PriceType.最高价),
                ("最低价", PriceType.最低价))

_STOCK_SCOPES = (("仓单", StockScope.交易所注册仓单), ("社会库存", StockScope.社会库存),
                 ("库存", StockScope.交易所库存))

_CONTRACTS = (("主力合约", ContractKind.主力合约), ("连二合约", ContractKind.连二连续))

# 发布时刻：SHFE 收盘 15:00，LME 现货结算与库存 19:00（北京时间），SMM 自采多在 11:30 出价。
_ENTRY_TIME = {"SHFE": time(15, 0), "LME": time(19, 0), "SMM": time(11, 30)}


def spot_source(name: str) -> SpotSource | None:
    """现货报价来源。只认现货价序列，升贴水、价差这类不套用。

    个旧市场不在口径字典里，宁可留空并在 note 标注，也不塞一个字典外的值——
    口径字典是同时点守卫的唯一真源，塞脏值等于让守卫失效。
    """
    if "1#锡" not in name:
        return None
    if "长江" in name:
        return SpotSource.长江有色
    if "个旧" in name:
        return None
    return SpotSource.SMM


def publisher(name: str) -> str:
    """原始发布方。SMM 是我们取得数据的渠道，不一定是数据的产生者。"""
    head = name.removeprefix(DISCONTINUED).split(":")[0].strip()
    return head if head in _ENTRY_TIME else "SMM"


def derive_category(name: str) -> str:
    for token, category in _CATEGORY_RULES:
        if token in name:
            return category
    return "其他"


def derive_caliber(series: SmmSeries) -> Caliber:
    """从 SMM 的层级命名里能确定的维度就填，确定不了的留空。

    留空不是偷懒：口径字典是同时点守卫的依据，填一个猜的值比留空危险得多——
    守卫会拿它去判"两端是否同口径"，猜错就放行了本该阻断的计算。
    """
    name, who = series.name, publisher(series.name)
    kwargs: dict = {}

    # 交易所行情与仓单代表交易时点；SMM 自采的报价、调研、平衡表是发布时点
    kwargs["time_type"] = TimeType.交易时点 if who in ("SHFE", "LME") else TimeType.发布时点

    for token, value in _PRICE_TYPES:
        if token in name:
            kwargs["price_type"] = value
            break
    for token, value in _CONTRACTS:
        if token in name:
            kwargs["contract_kind"] = value
            break
    for token, value in _STOCK_SCOPES:
        if token in name:
            kwargs["stock_scope"] = value
            break
    source = spot_source(name)
    if source is not None:
        kwargs["spot_source"] = source
    if series.unit == WeightBasis.金属吨:
        kwargs["weight_basis"] = WeightBasis.金属吨
    elif series.unit == WeightBasis.实物吨:
        kwargs["weight_basis"] = WeightBasis.实物吨

    note = f"SMM 终端 {series.vendor_code}；原始发布方 {who}"
    if "分仓库" in name or "分地区" in name:
        note += "；分项口径，不可与总计相加混用"
    if "个旧" in name:
        note += "；现货来源为个旧市场，口径字典暂无该枚举，spot_source 留空"
    return Caliber(note=note, **kwargs)


def derive_spec(series: SmmSeries, variety: str = "SN") -> IndicatorSpec:
    return IndicatorSpec(
        series_id=series.series_id,
        name=series.name[:120],
        variety=variety,
        category=derive_category(series.name),
        caliber=derive_caliber(series),
        unit=series.unit or "-",
        source=SOURCE,
        source_url=SOURCE_URL,
        frequency=series.frequency or "日",
        fetch_mode="manual",
    )


def observations(series: SmmSeries, entered_by: str, variety: str = "SN") -> list[ObservationIn]:
    """把一条序列的所有点转成待入库观测。

    日期单元格只有日期没有时刻，按发布方的常规发布时刻补齐，并在 note 里写明这是补的，
    符合「系统不替人编造时点，但要显式说明补齐规则」。
    """
    caliber = derive_caliber(series)
    at = _ENTRY_TIME.get(publisher(series.name), time(15, 0))
    note = f"SMM 终端导出 {series.vendor_code}；时刻按 {publisher(series.name)} 常规发布时刻 {at:%H:%M} 补齐"
    return [
        ObservationIn(series_id=series.series_id, value=value,
                      as_of=datetime.combine(day, at, SHANGHAI), caliber=caliber,
                      source=SOURCE, source_url=SOURCE_URL, entered_by=entered_by, note=note)
        for day, value in sorted(series.points.items())
    ]


# ---------- 入库 ----------

@dataclass
class LoadReport:
    registered: int = 0        # 新登记的指标
    reused: int = 0            # 命中已登记指标
    skipped_series: int = 0    # 停用列
    written: int = 0           # 新增观测
    unchanged: int = 0         # 值未变，未重复写
    rejected: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"指标 新登记 {self.registered} / 复用 {self.reused} / 跳过停用 {self.skipped_series}；"
                f"观测 写入 {self.written:,} / 值未变跳过 {self.unchanged:,} / 拒绝 {len(self.rejected)}")


def load(session, path: str | Path, *, entered_by: str, variety: str = "SN",
         dry_run: bool = False, progress=None) -> LoadReport:
    """把整本工作簿入库。所有观测仍逐条走 `record()` 闸门，只是共用一份预取的最新值索引。"""
    from tin.ingest.record import RecordError, latest_index, record
    from tin.models import Indicator

    report = LoadReport()
    everything = read_workbook(path)
    wanted = [s for s in everything if not s.discontinued]
    report.skipped_series = len(everything) - len(wanted)

    for series in wanted:
        spec = derive_spec(series, variety)
        existing = session.get(Indicator, spec.series_id)
        if existing is None:
            report.registered += 1
            if not dry_run:
                session.add(Indicator(
                    series_id=spec.series_id, name=spec.name, variety=spec.variety,
                    category=spec.category, caliber=spec.caliber.dump(), unit=spec.unit,
                    source=spec.source, source_url=spec.source_url, frequency=spec.frequency,
                    fetch_mode=spec.fetch_mode, phase="P1", vendor_code=series.vendor_code,
                    default_entry_time=_ENTRY_TIME.get(publisher(series.name), time(15, 0)),
                ))
        else:
            report.reused += 1
            if not dry_run and not existing.vendor_code:
                existing.vendor_code = series.vendor_code
    if not dry_run:
        session.flush()
    if dry_run:
        report.written = sum(len(s.points) for s in wanted)
        return report

    cache = latest_index(session, [s.series_id for s in wanted])
    for n, series in enumerate(wanted, 1):
        for obs in observations(series, entered_by, variety):
            try:
                row = record(session, obs, cache=cache)
            except RecordError as exc:
                report.rejected.append(f"{series.series_id}: {exc}")
                break  # 同一序列的后续点必然同样被拒，不必刷屏
            report.written += row is not None
            report.unchanged += row is None
        session.flush()
        if progress and n % 20 == 0:
            progress(n, len(wanted), report)
    session.commit()
    return report
