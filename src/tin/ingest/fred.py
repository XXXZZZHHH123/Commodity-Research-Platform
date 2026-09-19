"""FRED 公开 CSV 数据：宏观、利率、流动性与跨资产序列。"""

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from io import StringIO
from urllib.parse import urlencode

from tin.config import NEW_YORK
from tin.ingest.shfe import Batch, ParseError
from tin.schemas.caliber import Caliber
from tin.schemas.observation import IndicatorSpec, ObservationIn


@dataclass(frozen=True)
class MacroSeries:
    series_id: str
    name: str
    group: str
    unit: str
    frequency: str
    provider: str
    source_level: str
    publisher: str
    fred_id: str | None = None
    market_close: bool = False
    scale: float = 1.0
    display_unit: str | None = None
    decimals: int = 2

    @property
    def display_name(self) -> str:
        return self.name


# 首批选择与锡的金融定价、风险偏好和宏观需求最相关的公开序列。
FRED_SERIES: tuple[MacroSeries, ...] = (
    MacroSeries("FRED.DGS2", "美国国债 2Y", "利率与通胀", "%", "日", "FRED", "L1", "U.S. Treasury", "DGS2"),
    MacroSeries("FRED.DGS10", "美国国债 10Y", "利率与通胀", "%", "日", "FRED", "L1", "U.S. Treasury", "DGS10"),
    MacroSeries("FRED.DGS30", "美国国债 30Y", "利率与通胀", "%", "日", "FRED", "L1", "U.S. Treasury", "DGS30"),
    MacroSeries("FRED.DFII10", "美国 10Y 实际利率", "利率与通胀", "%", "日", "FRED", "L1", "U.S. Treasury", "DFII10"),
    MacroSeries("FRED.T10Y2Y", "美国 10Y-2Y 利差", "利率与通胀", "%", "日", "FRED", "DERIVED", "Federal Reserve", "T10Y2Y"),
    MacroSeries("FRED.T10YIE", "美国 10Y 盈亏平衡通胀", "利率与通胀", "%", "日", "FRED", "DERIVED", "Federal Reserve", "T10YIE"),
    MacroSeries("FRED.SP500", "标普 500", "风险偏好", "点", "日", "FRED", "L2", "S&P Dow Jones Indices", "SP500", True),
    MacroSeries("FRED.NASDAQSOX", "费城半导体指数", "风险偏好", "点", "日", "FRED", "L2", "Nasdaq", "NASDAQSOX", True),
    MacroSeries("MACRO.VIX", "VIX", "风险偏好", "点", "日", "CBOE", "L2", "CBOE", None, True),
    MacroSeries("FRED.BAMLH0A0HYM2", "美国高收益债 OAS", "风险偏好", "%", "日", "FRED", "L2", "ICE BofA", "BAMLH0A0HYM2"),
    MacroSeries("FRED.NFCI", "芝加哥联储金融条件", "风险偏好", "指数", "周", "FRED", "L1", "Chicago Fed", "NFCI", decimals=3),
    MacroSeries("FRED.DTWEXBGS", "广义贸易加权美元", "美元与流动性", "指数", "日", "FRED", "L1", "Federal Reserve", "DTWEXBGS"),
    MacroSeries("FX.USDCNY.mid", "美元兑人民币中间价", "美元与流动性", "元", "日", "CFETS", "L1", "中国外汇交易中心", decimals=4),
    MacroSeries("FRED.WALCL", "美联储总资产", "美元与流动性", "百万美元", "周", "FRED", "L1", "Federal Reserve", "WALCL", scale=1_000_000, display_unit="万亿美元"),
    MacroSeries("FRED.WTREGEN", "美国财政部 TGA", "美元与流动性", "十亿美元", "周", "FRED", "L1", "U.S. Treasury", "WTREGEN", scale=1_000, display_unit="万亿美元"),
    MacroSeries("FRED.RRPONTSYD", "隔夜逆回购余额", "美元与流动性", "十亿美元", "日", "FRED", "L1", "Federal Reserve Bank of New York", "RRPONTSYD", scale=1_000, display_unit="万亿美元"),
    MacroSeries("FRED.M2SL", "美国 M2", "美元与流动性", "十亿美元", "月", "FRED", "L1", "Federal Reserve", "M2SL", scale=1_000, display_unit="万亿美元"),
    MacroSeries("FRED.DCOILWTICO", "WTI 原油现货", "商品与周期", "美元/桶", "日", "FRED", "L1", "U.S. EIA", "DCOILWTICO", True),
    MacroSeries("FRED.PCOPPUSDM", "全球铜价", "商品与周期", "美元/吨", "月", "FRED", "L1", "IMF", "PCOPPUSDM", decimals=0),
    MacroSeries("FRED.INDPRO", "美国工业产出", "增长与就业", "指数", "月", "FRED", "L1", "Federal Reserve", "INDPRO"),
    MacroSeries("FRED.PAYEMS", "美国非农就业", "增长与就业", "千人", "月", "FRED", "L1", "U.S. BLS", "PAYEMS", decimals=0),
    MacroSeries("FRED.UNRATE", "美国失业率", "增长与就业", "%", "月", "FRED", "L1", "U.S. BLS", "UNRATE", decimals=1),
    MacroSeries("FRED.CPIAUCSL", "美国 CPI", "增长与就业", "指数", "月", "FRED", "L1", "U.S. BLS", "CPIAUCSL", decimals=1),
)

SERIES_BY_ID = {s.series_id: s for s in FRED_SERIES}
FETCHABLE_SERIES = tuple(s for s in FRED_SERIES if s.fred_id)
FEATURED_SERIES = (
    "FRED.DGS10", "FRED.DFII10", "FRED.DTWEXBGS", "FRED.SP500",
    "FRED.NASDAQSOX", "MACRO.VIX", "FRED.BAMLH0A0HYM2", "FRED.DCOILWTICO",
)


@dataclass(frozen=True)
class SourceInfo:
    name: str
    level: str
    scope: str
    access: str
    status: str


SOURCE_CATALOG: tuple[SourceInfo, ...] = (
    SourceInfo("FRED", "L1/L2", "美国宏观、利率、流动性、跨资产", "无需密钥", "已接入"),
    SourceInfo("上期所", "L2", "沪锡行情、仓单、库存", "无需密钥", "已接入"),
    SourceInfo("中国外汇交易中心", "L1/L2", "人民币汇率中间价", "无需密钥", "已接入"),
    SourceInfo("CBOE", "L2", "VIX", "无需密钥", "已接入"),
    SourceInfo("美国财政部 FiscalData / TIC", "L1", "债务、拍卖、海外持债", "无需密钥", "待接入"),
    SourceInfo("BLS", "L1", "就业、CPI 分项", "可无密钥", "待接入"),
    SourceInfo("BEA", "L1", "GDP、PCE 分项", "需要 BEA_KEY", "需凭证"),
    SourceInfo("CFTC", "L1", "COT 持仓与拥挤度", "无需密钥", "待接入"),
    SourceInfo("纽约联储 / ACM", "L1", "融资、一级交易商、期限溢价", "无需密钥", "待接入"),
    SourceInfo("ECB / BOJ / BoE / BoC", "L1", "全球货币与央行数据", "无需密钥", "待接入"),
    SourceInfo("TSA / Zillow", "L1/L3", "消费活动、租金", "无需密钥", "待接入"),
    SourceInfo("东方财富数据中心", "L3", "中国宏观、国债、日历", "无需密钥", "待接入"),
    SourceInfo("中国货币网", "L1/L2", "LPR、SHIBOR", "可能限流", "待接入"),
    SourceInfo("Yahoo Finance", "L2", "行情、ETF、外汇、加密资产", "可能限流", "待接入"),
    SourceInfo("iFinD", "L2", "A 股指数、沪金沪银", "专有插件", "需凭证"),
    SourceInfo("CME FedWatch / 期货", "L2", "利率决议概率", "需要浏览器或结算价", "待接入"),
    SourceInfo("金十 / Forex Factory", "L3", "经济日历", "金十需要 Token", "需凭证"),
    SourceInfo("Polymarket", "L2", "政策与政治事件概率", "无需密钥", "待接入"),
    SourceInfo("OCMacro / CNN Archive", "L3", "特朗普压力与社交媒体", "无需密钥", "候选"),
    SourceInfo("金十 / 先讯 / CS 财经", "L3", "实时新闻", "需要 Token/Session", "需凭证"),
    SourceInfo("MacroMicro CSV", "L3", "宏观图表序列", "用户导出", "人工导入"),
    SourceInfo("第三方终端 EDB", "L3", "历史与截面数据库", "终端导出", "人工导入"),
)


def url_for(spec: MacroSeries, through: date, lookback_days: int = 900) -> str:
    if not spec.fred_id:
        raise ValueError(f"{spec.series_id} 不是 FRED 序列")
    start = date.fromordinal(max(1, through.toordinal() - lookback_days))
    query = urlencode({"id": spec.fred_id, "cosd": start.isoformat(), "coed": through.isoformat()})
    return f"https://fred.stlouisfed.org/graph/fredgraph.csv?{query}"


def _as_of(d: date, market_close: bool) -> datetime:
    if market_close:
        return datetime.combine(d, time(16, 0), NEW_YORK)
    return datetime.combine(d, time(0, 0), timezone.utc)


def parse_series(text: str, spec: MacroSeries, through: date | None = None, source_url: str | None = None) -> Batch:
    """解析 fredgraph.csv；空值和 `.` 跳过，日期保持为观测期而不是抓取时刻。"""
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ParseError(f"FRED {spec.fred_id} CSV 没有表头")
    date_field = next((x for x in ("observation_date", "DATE", "date") if x in reader.fieldnames), None)
    value_field = spec.fred_id if spec.fred_id in reader.fieldnames else next(
        (x for x in reader.fieldnames if x != date_field), None)
    if date_field is None or value_field is None:
        raise ParseError(f"FRED {spec.fred_id} CSV 表头异常：{reader.fieldnames}")

    time_type = "交易时点" if spec.market_close else "发布时点"
    note = f"FRED ID {spec.fred_id}；原始发布方 {spec.publisher}；来源层级 {spec.source_level}"
    caliber = Caliber(time_type=time_type, price_type="收盘价" if spec.market_close else None,
                      note="FRED 仅提供观测日期，不含精确发布时间" if not spec.market_close else None)
    url = source_url or (url_for(spec, through) if through else "https://fred.stlouisfed.org/")
    batch = Batch(indicators=[IndicatorSpec(
        series_id=spec.series_id, name=spec.name, variety="COMMON", category=spec.group,
        caliber=caliber, unit=spec.unit, source=spec.provider, source_url=url,
        frequency=spec.frequency, fetch_mode="auto",
    )])
    for row in reader:
        raw = (row.get(value_field) or "").strip()
        if raw in {"", ".", "NaN", "nan"}:
            continue
        try:
            d = date.fromisoformat((row.get(date_field) or "").strip())
            value = float(raw)
        except ValueError as exc:
            raise ParseError(f"FRED {spec.fred_id} 无法解析：{row}") from exc
        if through and d > through:
            continue
        batch.observations.append(ObservationIn(
            series_id=spec.series_id, value=value, as_of=_as_of(d, spec.market_close),
            caliber=caliber, source=spec.provider, source_url=url, note=note,
        ))
    if not batch.observations:
        raise ParseError(f"FRED {spec.fred_id} 没有可用观测")
    return batch
