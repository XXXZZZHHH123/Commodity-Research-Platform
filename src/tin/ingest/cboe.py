"""CBOE 官方 VIX 日线（免费公开 CSV）。"""

import csv
from datetime import date, datetime, time
from io import StringIO

from tin.caliber.dictionary import PriceType, TimeType
from tin.config import NEW_YORK
from tin.ingest.shfe import Batch, ParseError
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_CALIBER = Caliber(time_type=TimeType.交易时点, price_type=PriceType.收盘价, note="美东 16:15 收盘")


def parse_vix(text: str, since: date | None = None) -> Batch:
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None or "CLOSE" not in reader.fieldnames:
        raise ParseError(f"VIX CSV 表头异常：{reader.fieldnames}")
    batch = Batch()
    for row in reader:
        d = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
        if since is not None and d < since:
            continue
        batch.observations.append(ObservationIn(
            series_id="MACRO.VIX", value=float(row["CLOSE"]),
            as_of=datetime.combine(d, time(16, 15), NEW_YORK),
            caliber=VIX_CALIBER, source="CBOE", source_url=VIX_URL))
    return batch
