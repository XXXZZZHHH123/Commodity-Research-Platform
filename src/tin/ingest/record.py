"""观测入库的唯一闸门（FR-3.1 / FR-4.1）。"""

import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.models import Indicator, Observation
from tin.schemas.observation import IndicatorSpec, ObservationIn


class RecordError(ValueError):
    pass


def ensure_indicator(session: Session, spec: IndicatorSpec) -> None:
    if session.get(Indicator, spec.series_id) is None:
        session.add(Indicator(
            series_id=spec.series_id, name=spec.name, variety=spec.variety, category=spec.category,
            caliber=spec.caliber.dump(), unit=spec.unit, source=spec.source, source_url=spec.source_url,
            frequency=spec.frequency, fetch_mode=spec.fetch_mode, phase="P1",
        ))
        session.flush()


def _check_caliber_consistent(indicator: Indicator, obs: ObservationIn) -> None:
    got = obs.caliber.dump()
    for key, expected in indicator.caliber.items():
        if key == "note":
            continue
        if got.get(key) != expected:
            raise RecordError(
                f"{obs.series_id} 口径不符：登记为 {key}={expected}，本次入库为 {key}={got.get(key)}")


def latest_index(session: Session, series_ids: list[str]) -> dict[tuple[str, datetime], Observation]:
    """预取这些序列的最新修订，供批量入库时当缓存用。

    批量导入一次几十万条，逐条 SELECT 会让入库时间从分钟级变成小时级；但闸门只能有一个，
    所以是把查询结果喂给 `record()`，而不是另开一条绕过校验的写入路径。
    """
    rows = session.scalars(select(Observation).where(Observation.series_id.in_(series_ids))
                           .order_by(Observation.revision)).all()
    return {(r.series_id, r.as_of): r for r in rows}  # 按 revision 升序，最后写入的即最新


def record(session: Session, obs: ObservationIn, *, fetched_at: datetime | None = None,
           cache: dict[tuple[str, datetime], Observation] | None = None) -> Observation | None:
    """写入一条观测。同 series_id+as_of 值不变则跳过；值变了则追加新修订号，保留历史。

    传入 `cache`（见 `latest_index`）时用它代替逐条查询，校验与判重逻辑完全一致。
    """
    indicator = session.get(Indicator, obs.series_id)
    if indicator is None:
        raise RecordError(f"指标未登记：{obs.series_id}")
    if indicator.status != "可用":
        raise RecordError(f"指标已停用：{obs.series_id}")
    if indicator.fetch_mode == "manual" and not (obs.entered_by and obs.note):
        raise RecordError(f"{obs.series_id} 为人工录入指标，必须填写录入人与来源说明")
    _check_caliber_consistent(indicator, obs)

    if cache is None:
        latest = session.scalars(
            select(Observation)
            .where(Observation.series_id == obs.series_id, Observation.as_of == obs.as_of)
            .order_by(Observation.revision.desc())
            .limit(1)
        ).first()
    else:
        latest = cache.get((obs.series_id, obs.as_of))
    if latest is not None and math.isclose(latest.value, obs.value, rel_tol=1e-12, abs_tol=0.0):
        return None

    row = Observation(
        series_id=obs.series_id, value=obs.value, as_of=obs.as_of,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        caliber_snapshot=obs.caliber.dump(), source=obs.source, source_url=obs.source_url,
        revision=0 if latest is None else latest.revision + 1,
        entered_by=obs.entered_by, note=obs.note, status="正常",
    )
    session.add(row)
    if cache is None:
        session.flush()
    else:
        cache[(obs.series_id, obs.as_of)] = row
    return row
