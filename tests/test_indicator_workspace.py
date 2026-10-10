from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from lxml.html import fromstring

from tin.config import SHANGHAI
from tin.ingest.fred import SERIES_BY_ID, parse_series
from tin.ingest.record import record
from tin.ingest.runner import store
from tin.models import Indicator
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn
from tin.web.app import app


@pytest.fixture
def client(loaded, monkeypatch):
    monkeypatch.setattr("tin.web.app.SessionLocal", lambda: loaded)
    return TestClient(app)


def series(session, sid="TEST.OUTPUT", *, values=(100, 120), days=(16, 18), unit="吨"):
    session.add(Indicator(series_id=sid, name="精锡产量 <测试>", variety="SN", category="供给",
                          caliber={"time_type": "发布时点"}, unit=unit, source="测试源",
                          frequency="日", fetch_mode="manual", status="可用", vendor_code="ID123"))
    session.flush()
    for day, value in zip(days, values):
        record(session, ObservationIn(series_id=sid, value=value,
            as_of=datetime(2026, 9, day, 15, tzinfo=SHANGHAI),
            caliber=Caliber(time_type="发布时点"), source="测试源", entered_by="测试", note="测试"))
    session.commit()


def test_macro_link_keeps_date_and_opens_macro_filter(client):
    response = client.get("/sn/macro?date=2026-09-18", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/sn/indicators?scope=macro&date=2026-09-18"


def test_catalog_is_complete_grouped_open_and_secondary_panels_collapsed(client, loaded):
    series(loaded)
    html = fromstring(client.get("/sn/indicators").text)
    groups = html.xpath("//details[@data-indicator-group]")
    assert groups and all("open" in g.attrib for g in groups)
    ids = html.xpath("//tr[@data-indicator-row]/@data-series-id")
    assert "TEST.OUTPUT" in ids and "FRED.DGS10" in ids
    assert len(ids) == len(set(ids)), "宏观指标与登记指标只能展示一次"
    assert not any(s.startswith("SHFE.SN.26") for s in ids)
    assert html.xpath('//*[@id="indicator-search"]')
    assert html.xpath('//*[@id="indicator-favorites"]')
    for ident in ("indicator-checks", "indicator-sources", "indicator-dictionary"):
        assert "open" not in html.get_element_by_id(ident).attrib
    assert not html.xpath('//nav//a[starts-with(@href,"/sn/macro")]')
    assert "精锡产量 <测试>" in html.text_content()
    assert "精锡产量 &lt;测试&gt;" in client.get("/sn/indicators").text


def test_preview_uses_latest_revision_and_never_reads_future_values(client, loaded):
    series(loaded)
    record(loaded, ObservationIn(series_id="TEST.OUTPUT", value=125,
        as_of=datetime(2026, 9, 18, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="发布时点"), source="测试源", entered_by="测试", note="修订"))
    record(loaded, ObservationIn(series_id="TEST.OUTPUT", value=900,
        as_of=datetime(2026, 9, 19, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="发布时点"), source="测试源", entered_by="测试", note="未来"))
    loaded.commit()
    response = client.get("/api/sn/indicators/previews", params={"series_id": "TEST.OUTPUT", "date": "2026-09-18"})
    assert response.status_code == 200
    card = response.json()["cards"][0]
    assert card["value"] == "125.00" and card["count"] == 2
    assert card["as_of"] == "2026-09-18"
    history = client.get("/api/sn/series/TEST.OUTPUT/history?date=2026-09-18").json()
    assert [p["value"] for p in history["points"]] == [100, 125]


def test_macro_preview_keeps_display_scale_and_original_units(client, loaded):
    store(loaded, parse_series("observation_date,WALCL\n2026-09-16,7500000\n2026-09-18,7600000\n",
                              SERIES_BY_ID["FRED.WALCL"]))
    loaded.commit()
    response = client.get("/api/sn/indicators/previews?series_id=FRED.WALCL&date=2026-09-18")
    card = response.json()["cards"][0]
    assert card["value"] == "7.60" and card["unit"] == "万亿美元"
    history = client.get("/api/sn/series/FRED.WALCL/history?date=2026-09-18").json()
    assert history["raw_unit"] == "百万美元"
    assert history["points"][-1]["value"] == 7.6


def test_preview_missing_stale_and_retired_are_explicit(client, loaded):
    series(loaded, days=(10, 11))
    cards = client.get("/api/sn/indicators/previews", params=[("series_id", "TEST.OUTPUT"),
        ("series_id", "FRED.DGS10"), ("date", "2026-09-18")]).json()["cards"]
    assert cards[0]["stale"] and cards[0]["as_of"] == "2026-09-11"
    assert cards[1]["status"] == "missing" and "尚无数据" in cards[1]["note"]
    loaded.get(Indicator, "TEST.OUTPUT").status = "停用"
    loaded.commit()
    card = client.get("/api/sn/indicators/previews?series_id=TEST.OUTPUT").json()["cards"][0]
    assert card["retired"]


def test_previews_work_without_a_trading_calendar(session, monkeypatch):
    monkeypatch.setattr("tin.web.app.SessionLocal", lambda: session)
    client = TestClient(app)
    assert client.get("/sn/indicators").status_code == 200
    response = client.get("/api/sn/indicators/previews?series_id=FRED.DGS10")
    assert response.status_code == 200
    assert response.json()["cards"][0]["status"] == "missing"
    series(session)
    preview = client.get("/api/sn/indicators/previews?series_id=TEST.OUTPUT").json()
    assert 'data-chart-date=""' in preview["html"]
    assert len(client.get("/api/sn/series/TEST.OUTPUT/history").json()["points"]) == 2


def test_comparison_aligns_real_dates_without_forward_filling(client, loaded):
    series(loaded)
    series(loaded, "TEST.OTHER", days=(16, 17, 18), values=(200, 800, 220), unit="元/吨")
    result = client.get("/api/sn/indicators/compare", params=[("series_id", "TEST.OUTPUT"),
        ("series_id", "TEST.OTHER"), ("date", "2026-09-18")]).json()
    assert result["status"] == "ok"
    assert result["dates"] == ["2026-09-16", "2026-09-18"]
    assert result["series"][0]["values"] == [100, 120]
    assert result["series"][1]["values"] == pytest.approx([100, 110])
    assert result["series"][1]["raw_values"] == [200, 220]


@pytest.mark.parametrize("values,days", [((0, 10), (16, 18)), ((-1, 2), (16, 18)), ((1, 2), (15, 17))])
def test_comparison_explains_unusable_baseline_or_no_common_period(client, loaded, values, days):
    series(loaded)
    series(loaded, "TEST.OTHER", values=values, days=days)
    result = client.get("/api/sn/indicators/compare", params=[("series_id", "TEST.OUTPUT"),
        ("series_id", "TEST.OTHER")]).json()
    assert result["status"] == "unavailable" and result["reason"]


def test_endpoint_validation(client):
    assert client.get("/api/sn/indicators/previews?series_id=NOPE").status_code == 404
    assert client.get("/api/sn/indicators/previews").status_code == 422
    assert client.get("/api/sn/indicators/previews", params=[("series_id", "MACRO.VIX")] * 41).status_code == 422
    assert client.get("/api/sn/indicators/compare?series_id=MACRO.VIX").status_code == 422
    assert client.get("/api/sn/indicators/compare?series_id=MACRO.VIX&series_id=MACRO.VIX").status_code == 422
    assert client.get("/api/sn/indicators/compare?series_id=MACRO.VIX&series_id=NOPE").status_code == 404
    assert client.get("/sn/indicators?date=bad").status_code == 400


def test_no_common_source_data_is_not_called_a_mismatch(client):
    html = fromstring(client.get("/sn/indicators").text)
    checks = html.get_element_by_id("indicator-checks")
    assert "无法比较" in checks.text_content()
    assert "超出容差" not in checks.text_content()


def test_generic_percentage_uses_percentage_points_not_interest_rate_basis_points(client, loaded):
    series(loaded, unit="%", values=(70, 72))
    preview = client.get("/api/sn/indicators/previews?series_id=TEST.OUTPUT").json()
    assert preview["cards"][0]["change"] == "+2.00 个百分点"
    assert "精锡产量 &lt;测试&gt;" in preview["html"]
    data = client.get("/api/sn/series/TEST.OUTPUT/history").json()
    assert data["change_unit"] == "百分点"


def test_browsing_missing_macro_series_does_not_register_it(client, loaded):
    assert loaded.get(Indicator, "FRED.DGS10") is None
    client.get("/sn/indicators")
    client.get("/api/sn/indicators/previews?series_id=FRED.DGS10")
    assert loaded.get(Indicator, "FRED.DGS10") is None
