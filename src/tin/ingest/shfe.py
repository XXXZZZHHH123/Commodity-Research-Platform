"""上海期货交易所公开数据：全合约日行情、仓单日报、库存周报。直连交易所，不经第三方转载。"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from io import StringIO

import pandas as pd

from tin.caliber.dictionary import ContractKind, PriceType, StockScope, TimeType
from tin.config import SHANGHAI
from tin.schemas.caliber import Caliber
from tin.schemas.observation import IndicatorSpec, ObservationIn

BASE = "https://www.shfe.com.cn/data/tradedata/future"
QUOTE_URL = BASE + "/dailydata/kx{d}.dat"
WARRANT_URL = BASE + "/stockdata/dailystock_{d}/ZH/all.html"
WEEKLY_URL = BASE + "/stockdata/weeklystock_{d}/ZH/all.html"

PRODUCT = {"SN": ("sn_f", "锡")}


class ParseError(ValueError):
    pass


@dataclass
class Batch:
    indicators: list[IndicatorSpec] = field(default_factory=list)
    observations: list[ObservationIn] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    trading_day: tuple[date, int] | None = None  # (交易日, 年内期号)


def trade_close(d: date) -> datetime:
    return datetime.combine(d, time(15, 0), SHANGHAI)


def _num(v) -> float | None:
    # 交易所用空字符串表示缺值；绝不能把它当 0
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return float(v)


_QUOTE_FIELDS = (
    # (字段, 后缀, 名称, 单位, 价格类型)
    ("CLOSEPRICE", "close", "收盘价", "元/吨", PriceType.收盘价),
    ("SETTLEMENTPRICE", "settle", "结算价", "元/吨", PriceType.结算价),
    ("VOLUME", "volume", "成交量", "手", None),
    ("OPENINTEREST", "oi", "持仓量", "手", None),
)


def parse_quotes(payload: dict, variety: str = "SN") -> Batch:
    if payload.get("o_code") != "0000":
        raise ParseError(f"上期所行情返回异常：o_code={payload.get('o_code')} {payload.get('o_msg')}")
    d = datetime.strptime(payload["report_date"], "%Y%m%d").date()
    as_of = trade_close(d)
    url = QUOTE_URL.format(d=d.strftime("%Y%m%d"))
    product_id, cname = PRODUCT[variety]
    rows = [r for r in payload["o_curinstrument"]
            if r.get("PRODUCTID") == product_id and str(r.get("DELIVERYMONTH", "")).isdigit()]
    if not rows:
        raise ParseError(f"{d} 行情中没有 {variety} 合约")

    batch = Batch(trading_day=(d, int(payload["o_year_num"])))
    for r in rows:
        contract = f"{variety}{r['DELIVERYMONTH']}"
        for key, suffix, label, unit, price_type in _QUOTE_FIELDS:
            sid = f"SHFE.{variety}.{r['DELIVERYMONTH']}.{suffix}"
            cal = Caliber(time_type=TimeType.交易时点, price_type=price_type,
                          contract_kind=ContractKind.具体合约, contract=contract)
            batch.indicators.append(IndicatorSpec(
                series_id=sid, name=f"沪{cname}{contract}{label}", variety=variety, category="价格",
                caliber=cal, unit=unit, source="SHFE", source_url=url, frequency="日"))
            v = _num(r.get(key))
            if v is None:
                batch.skipped.append(f"{sid} 当日无值")
                continue
            batch.observations.append(ObservationIn(
                series_id=sid, value=v, as_of=as_of, caliber=cal, source="SHFE", source_url=url))

    with_oi = [r for r in rows if _num(r.get("OPENINTEREST")) is not None]
    main = max(with_oi, key=lambda r: _num(r["OPENINTEREST"]))
    contract = f"{variety}{main['DELIVERYMONTH']}"
    for key, suffix, _label, _unit, price_type in _QUOTE_FIELDS[:2]:
        v = _num(main.get(key))
        if v is None:
            batch.skipped.append(f"SHFE.{variety}.main.{suffix} 主力合约 {contract} 当日无值")
            continue
        batch.observations.append(ObservationIn(
            series_id=f"SHFE.{variety}.main.{suffix}", value=v, as_of=as_of,
            caliber=Caliber(time_type=TimeType.交易时点, price_type=price_type,
                            contract_kind=ContractKind.主力合约, contract=contract,
                            note="按当日持仓量最大选取"),
            source="SHFE", source_url=url))
    return batch


def _find_variety_table(html: str, cname: str, d: date) -> pd.DataFrame:
    tables = pd.read_html(StringIO(html))
    header = str(tables[0].iloc[0, 0])
    if not header.startswith(d.isoformat()):
        raise ParseError(f"报表日期不符：请求 {d}，页面为「{header[:24]}」")
    for t in tables[1:]:
        if str(t.iloc[0, 0]).strip() == cname:
            return t
    raise ParseError(f"{d} 报表中没有「{cname}」表")


def _total_row(t: pd.DataFrame) -> pd.Series:
    hit = t[t.iloc[:, 0].astype(str).str.strip() == "总计"]
    if hit.empty:
        raise ParseError("表中没有“总计”行")
    return hit.iloc[0]


def parse_daily_warrant(html: str, d: date, variety: str = "SN") -> Batch:
    _pid, cname = PRODUCT[variety]
    t = _find_variety_table(html, cname, d)
    col = [c for c in t.columns if str(c).strip() == "期货"]
    if not col:
        raise ParseError(f"仓单日报「{cname}」表缺少“期货”列：{list(t.columns)}")
    value = float(_total_row(t)[col[0]])
    return Batch(observations=[ObservationIn(
        series_id=f"SHFE.{variety}.warrant", value=value, as_of=trade_close(d),
        caliber=Caliber(time_type=TimeType.交易时点, stock_scope=StockScope.交易所注册仓单),
        source="SHFE", source_url=WARRANT_URL.format(d=d.strftime("%Y%m%d")))])


def parse_weekly_stock(html: str, d: date, variety: str = "SN") -> Batch:
    _pid, cname = PRODUCT[variety]
    t = _find_variety_table(html, cname, d)
    col = [c for c in t.columns
           if isinstance(c, tuple) and "本周库存" in str(c[0]) and str(c[1]).strip() == "小计"]
    if not col:
        raise ParseError(f"库存周报「{cname}」表缺少“本周库存/小计”列：{list(t.columns)}")
    value = float(_total_row(t)[col[0]])
    return Batch(observations=[ObservationIn(
        series_id=f"SHFE.{variety}.stock.weekly", value=value, as_of=trade_close(d),
        caliber=Caliber(time_type=TimeType.交易时点, stock_scope=StockScope.交易所库存),
        source="SHFE", source_url=WEEKLY_URL.format(d=d.strftime("%Y%m%d")))])
