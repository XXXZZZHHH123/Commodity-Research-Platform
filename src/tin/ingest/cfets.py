"""中国外汇交易中心：人民币汇率中间价（每个工作日 9:15 发布）。"""

from datetime import datetime

from tin.caliber.dictionary import TimeType
from tin.config import SHANGHAI
from tin.ingest.shfe import Batch, ParseError
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

CCPR_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"


def parse_ccpr(payload: dict) -> Batch:
    if str(payload.get("head", {}).get("rep_code")) != "200":
        raise ParseError(f"外汇交易中心返回异常：{payload.get('head')}")
    as_of = datetime.strptime(payload["data"]["lastDate"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
    rec = next((r for r in payload["records"] if r.get("vrtEName") == "USD/CNY"), None)
    if rec is None or not str(rec.get("price", "")).strip():
        raise ParseError("中间价数据中没有 USD/CNY")
    return Batch(observations=[ObservationIn(
        series_id="FX.USDCNY.mid", value=float(rec["price"]), as_of=as_of,
        caliber=Caliber(time_type=TimeType.发布时点, note="人民币兑美元中间价"),
        source="CFETS", source_url=CCPR_URL)])
