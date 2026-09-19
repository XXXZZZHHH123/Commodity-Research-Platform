import math

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from tin.schemas.caliber import Caliber


class IndicatorSpec(BaseModel):
    """采集器遇到未登记的合约序列时，据此自动登记（只用于具体合约行情，不用于白名单指标）。"""

    series_id: str
    name: str
    variety: str
    category: str
    caliber: Caliber
    unit: str
    source: str
    source_url: str | None = None
    frequency: str
    fetch_mode: str = "auto"


class ObservationIn(BaseModel):
    """入库前的观测。时点必须带时区，口径必须合法——两者缺一不可入库。"""

    model_config = ConfigDict(frozen=True)

    series_id: str
    value: float
    as_of: AwareDatetime
    caliber: Caliber
    source: str
    source_url: str | None = None
    entered_by: str | None = None
    note: str | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"数值非有限值：{v}")
        return v
