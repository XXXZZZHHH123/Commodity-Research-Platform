"""原件留证与离线回放（docs/数据进出方案.md §4）。

这一组测试是安全带：只要它们绿着，「从原件回放」就与「当天在线抓取」等价——
在别的机器上抓回来、拷进服务器入库的数据，不会与服务器直连采集长出不同的结果。
"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tin.models  # noqa: F401
from tin.db import Base, make_engine
from tin.ingest import raw, runner
from tin.jobs.seed import seed_indicators
from tin.models import Observation

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 9, 18)


def _fixture_for(url: str) -> bytes | None:
    """按 URL 派发到 fixtures；未覆盖的 URL 返回 None，调用方按 404 处理。

    交易所的当日件只在 9/18 这一天存在，其余日期一律 404——真实世界里周末与节假日就是这样。
    中间价接口不同：它任何时候都返回「今天」的值，所以无条件返回同一份 9/18 的 payload。
    """
    dated = "20260918" in url
    if "/dailydata/kx" in url:
        return (FIX / "shfe_kx_20260918.json").read_bytes() if dated else None
    if "dailystock_" in url:
        return (FIX / "shfe_dailystock_20260918.html").read_bytes() if dated else None
    if "weeklystock_" in url:
        return (FIX / "shfe_weeklystock_20260918.html").read_bytes() if dated else None
    if "ccpr.json" in url:
        return (FIX / "cfets_ccpr_20260918.json").read_bytes()
    if "VIX_History.csv" in url:
        return (FIX / "cboe_vix_tail.csv").read_bytes()
    if "fredgraph.csv" in url:
        fred_id = url.split("id=")[1].split("&")[0]
        return f"observation_date,{fred_id}\n2026-09-17,1.11\n2026-09-18,2.22\n".encode()
    return None


@pytest.fixture
def offline(monkeypatch):
    """把 httpx 的三个出口都换成 fixture 服务，在线路径与留证路径共用同一批响应。"""
    def serve(method, url, **_):
        body = _fixture_for(str(url))
        request = httpx.Request(method, url)
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, request=request)

    monkeypatch.setattr(httpx.Client, "request", lambda self, method, url, **kw: serve(method, url))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: serve("GET", url))
    monkeypatch.setattr(httpx, "request", lambda method, url, **kw: serve(method, url))


def make_session():
    engine = make_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(engine, expire_on_commit=False)()
    seed_indicators(s)
    return s


def rows(session) -> list[tuple]:
    obs = session.scalars(select(Observation).order_by(Observation.series_id, Observation.as_of)).all()
    return [(o.series_id, o.value, o.as_of, o.source, json.dumps(o.caliber_snapshot, sort_keys=True))
            for o in obs]


def test_plan_covers_every_online_fetcher():
    """留证计划必须与在线采集的数据源一一对应。

    漏掉一个，那个源就永远不会被 Actions 抓到，而且不会有任何报错。
    """
    assert set(raw.plan_day(D)) == {f.name for f in runner.FETCHERS}


def test_fred_requests_never_carry_a_browser_ua():
    """FRED 对带浏览器 UA 的并发请求会挂住不响应，直到读超时——实测 6 条全军覆没。"""
    plan = raw.plan_day(D)
    assert all(not r.browser_ua for r in plan["fred_macro"])
    assert all(r.browser_ua for r in plan["shfe_quotes"] + plan["cfets_fx"])


def test_replay_reproduces_online_fetch_exactly(offline, tmp_path):
    """同一批响应，一条走在线入库，一条先落盘再回放，观测行必须逐行一致。"""
    online = make_session()
    runner.fetch_all(online, D)

    raw.snapshot_day(D, tmp_path)
    replayed = make_session()
    raw.replay_day(replayed, D, tmp_path)

    assert rows(replayed) == rows(online)
    assert len(rows(online)) > 0


def test_replay_is_idempotent(offline, tmp_path):
    """重复回放同一天不产生任何多余行——这是「定时不准就多跑几次」的前提。"""
    raw.snapshot_day(D, tmp_path)
    session = make_session()
    first = sum(r.written for r in raw.replay_day(session, D, tmp_path))
    second = sum(r.written for r in raw.replay_day(session, D, tmp_path))
    assert first > 0
    assert second == 0


def test_tampered_raw_file_is_rejected(offline, tmp_path):
    """原件与台账 sha256 不符时拒绝入库：留证的意义就在于事后可验。"""
    raw.snapshot_day(D, tmp_path)
    target = tmp_path / "shfe" / "quotes" / f"{D.isoformat()}.dat"
    target.write_bytes(target.read_bytes().replace(b"407490", b"999999"))

    session = make_session()
    run = next(r for r in raw.replay_day(session, D, tmp_path) if r.fetcher == "shfe_quotes")
    assert run.status == raw.FAILED
    assert "与台账不符" in run.error


def test_not_published_is_not_a_failure(offline, tmp_path):
    """周末与节假日各源返回 404，这是正常结果，不能当故障——否则长假天天误报。"""
    weekend = date(2026, 9, 19)  # 周六：SHFE 无当日件
    manifest = raw.snapshot_day(weekend, tmp_path)
    assert manifest["sources"]["shfe_quotes"]["status"] == raw.NOT_PUBLISHED
    assert not any(e["status"] == raw.FAILED for e in manifest["sources"].values())


def test_mislabelled_raw_file_is_discarded(offline, tmp_path):
    """中间价接口只给当日值：抓历史日期时拿到的是今天的数，不能存进那天的路径。

    贴错标签的证据比没有证据更糟。
    """
    past = date(2026, 9, 15)
    manifest = raw.snapshot_day(past, tmp_path)
    entry = manifest["sources"]["cfets_fx"]
    assert entry["status"] == raw.NOT_PUBLISHED
    assert entry["files"] == []
    assert not (tmp_path / "cfets" / "ccpr" / f"{past.isoformat()}.json").exists()


def test_manifest_records_a_verifiable_source_url(offline, tmp_path):
    """每份原件都要带 source_url 与 sha256，才接得上 P2 的「每个数字都有来源」。"""
    manifest = raw.snapshot_day(D, tmp_path)
    for name, entry in manifest["sources"].items():
        for f in entry["files"]:
            assert f["url"].startswith("https://"), name
            assert len(f["sha256"]) == 64, name
            assert f["bytes"] > 0, name


def test_snapshot_path_never_touches_the_database():
    """Actions 上跑的是留证路径，它不该把数据库模块拉起来，更不该建出库文件。"""
    code = ("import sys; import tin.ingest.raw; "
            "print([m for m in sys.modules if m in ('tin.db', 'tin.models')])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).parents[1], check=True)
    assert out.stdout.strip() == "[]", f"留证路径牵连了数据库模块：{out.stdout}"


def test_latest_snapshot_reports_freshness(offline, tmp_path):
    """靠这个值能看出「已经好几天没有新原件了」。"""
    assert raw.latest_snapshot(tmp_path) is None
    raw.snapshot_day(D, tmp_path)
    assert raw.latest_snapshot(tmp_path) == D


def test_rolling_files_may_change_between_days(offline, tmp_path):
    """VIX 与 FRED 的全量历史文件会被后一天的抓取覆盖，回放旧日期不能因此失败。

    解析时按目标日截断，所以用新文件回放旧日期仍然得到当天该有的结果。
    """
    raw.snapshot_day(D, tmp_path)
    later = tmp_path / "fred" / "DGS2.csv"
    later.write_text(later.read_text() + "2026-09-21,3.33\n", encoding="utf-8")

    session = make_session()
    run = next(r for r in raw.replay_day(session, D, tmp_path) if r.fetcher == "fred_macro")
    assert run.status == raw.OK, run.error
    later_rows = [o for o in session.scalars(select(Observation)).all()
                  if o.series_id == "FRED.DGS2" and o.as_of.date() > D]
    assert later_rows == [], "回放旧日期时不应带入目标日之后的数据"
