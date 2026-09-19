"""采集调度（FR-3.1）：每个数据源失败重试 1 次；仍失败记入 fetch_runs 并在页面显示缺口，不中断其余采数。"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from tin.config import settings
from tin.ingest import cboe, cfets, shfe
from tin.ingest.record import ensure_indicator, record
from tin.ingest.shfe import Batch
from tin.models import FetchRun, TradingDay

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


class NotPublished(Exception):
    """目标日数据尚未发布或当日非交易日。不是故障，不重试。"""


def _get(client: httpx.Client, url: str, method: str = "GET") -> httpx.Response:
    r = client.request(method, url)
    if r.status_code == 404:
        raise NotPublished(f"{url} 返回 404")
    r.raise_for_status()
    r.encoding = "utf-8"
    return r


@dataclass(frozen=True)
class Fetcher:
    name: str
    fetch: Callable[[httpx.Client, date], Batch]


def _quotes(c, d):
    return shfe.parse_quotes(_get(c, shfe.QUOTE_URL.format(d=d.strftime("%Y%m%d"))).json())


def _warrant(c, d):
    return shfe.parse_daily_warrant(_get(c, shfe.WARRANT_URL.format(d=d.strftime("%Y%m%d"))).text, d)


def _weekly(c, d):
    return shfe.parse_weekly_stock(_get(c, shfe.WEEKLY_URL.format(d=d.strftime("%Y%m%d"))).text, d)


def _fx(c, d):
    batch = cfets.parse_ccpr(_get(c, cfets.CCPR_URL, "POST").json())
    got = batch.observations[0].as_of.date()
    if got != d:
        raise NotPublished(f"中间价接口只提供当日数据，当前为 {got}，无法取 {d}")
    return batch


def _vix(c, d):
    batch = cboe.parse_vix(_get(c, cboe.VIX_URL).text, since=d - timedelta(days=10))
    if not batch.observations:
        raise NotPublished(f"VIX 截至 {d} 前 10 日无数据")
    return batch


FETCHERS = (
    Fetcher("shfe_quotes", _quotes),
    Fetcher("shfe_warrant", _warrant),
    Fetcher("shfe_weekly_stock", _weekly),
    Fetcher("cfets_fx", _fx),
    Fetcher("cboe_vix", _vix),
)


def store(session: Session, batch: Batch) -> int:
    for spec in batch.indicators:
        ensure_indicator(session, spec)
    if batch.trading_day:
        d, seq = batch.trading_day
        if session.get(TradingDay, d.isoformat()) is None:
            session.add(TradingDay(trade_date=d.isoformat(), year=d.year, year_seq=seq))
    fetched_at = datetime.now(timezone.utc)
    return sum(record(session, o, fetched_at=fetched_at) is not None for o in batch.observations)


def run_one(session: Session, client: httpx.Client, f: Fetcher, d: date) -> FetchRun:
    run = FetchRun(fetcher=f.name, target_date=d.isoformat(), started_at=datetime.now(timezone.utc),
                   status="running", attempts=0)
    for attempt in (1, 2):
        run.attempts = attempt
        try:
            batch = f.fetch(client, d)
            with session.begin_nested():
                run.written = store(session, batch)
            run.status, run.error = "ok", None
            break
        except NotPublished as e:
            run.status, run.error = "未发布", str(e)
            break
        except Exception as e:  # noqa: BLE001 —— 任一数据源故障都不能中断其余采数
            run.status, run.error = "失败", f"{type(e).__name__}: {e}"
            log.warning("%s %s 第 %d 次失败：%s", f.name, d, attempt, run.error)
    run.finished_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    return run


def fetch_all(session: Session, d: date, only: set[str] | None = None) -> list[FetchRun]:
    with httpx.Client(headers=UA, timeout=settings.http_timeout, follow_redirects=True) as client:
        return [run_one(session, client, f, d) for f in FETCHERS if only is None or f.name in only]
