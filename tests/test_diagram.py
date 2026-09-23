"""动态产业图的取值与状态判定（docs/动态产业图_功能实现方案.md）。

这一组测试守的是同一件事：**图上每个方框必须诚实**。
没数就说没数，断更就说断更，口径冲突和缺输入要分开说，代理指标要标出来——
显示一个看起来正常的数字，比显示空白危险得多。
"""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from tin.config import SHANGHAI
from tin.export import diagram
from tin.ingest.record import record
from tin.models import Derived, DiagramTemplate, Indicator
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

TODAY = date(2026, 9, 21)


def node(nid, series=None, formula=None, proxy=False, label="测试节点"):
    binding = {"kind": "none"}
    if series:
        binding = {"kind": "series", "series_id": series, "proxy": proxy}
    if formula:
        binding = {"kind": "derived", "formula_id": formula}
    return {"id": nid, "label": label, "x": 0, "y": 0, "w": 200, "h": 100, "binding": binding}


def add_series(session, series_id, frequency="日", unit="吨", status="可用"):
    session.add(Indicator(series_id=series_id, name=series_id, variety="SN", category="库存与流通",
                          caliber={"time_type": "发布时点"}, unit=unit, source="测试",
                          frequency=frequency, fetch_mode="manual", phase="P1", status=status))
    session.flush()


def add_points(session, series_id, points, step_days=1):
    """points 为从新到旧的数值列表。"""
    for i, value in enumerate(points):
        record(session, ObservationIn(
            series_id=series_id, value=value,
            as_of=datetime.combine(TODAY - timedelta(days=i * step_days), datetime.min.time(),
                                   SHANGHAI).replace(hour=15),
            caliber=Caliber(time_type="发布时点"), source="测试",
            entered_by="测试", note="测试"))
    session.flush()


# ---------- 六种状态 ----------

def test_normal_node_shows_value_change_and_timestamp(session):
    add_series(session, "T.OK")
    add_points(session, "T.OK", [110, 100])
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.OK")]}, TODAY)[0]
    assert out["state"] == diagram.OK
    assert out["value"] == 110
    assert out["mom"] == pytest.approx(10.0)
    assert out["as_of"] == TODAY.isoformat()
    assert out["unit"] == "吨"


def test_stale_node_says_how_long_it_has_been_silent(session):
    """断更必须说出"多久没更新"，只标个颜色等于让人自己去数日子。"""
    add_series(session, "T.STALE", frequency="日")
    record(session, ObservationIn(
        series_id="T.STALE", value=1,
        as_of=datetime(2026, 9, 1, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="发布时点"), source="测试", entered_by="测试", note="测试"))
    session.flush()
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.STALE")]}, TODAY)[0]
    assert out["state"] == diagram.STALE
    assert "20 天未更新" in out["note"]
    assert out["value"] == 1, "断更仍要显示最后一个值，但必须同时说明它有多旧"


def test_registered_but_empty_series_says_no_data(session):
    add_series(session, "T.EMPTY")
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.EMPTY")]}, TODAY)[0]
    assert out["state"] == diagram.NO_DATA
    assert out["value"] is None


def test_retired_indicator_is_not_silently_blank(session):
    """指标被停用后，图上那个框不能变成一片空白——要说清楚是指标没了。"""
    add_series(session, "T.GONE", status="停用")
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.GONE")]}, TODAY)[0]
    assert out["state"] == diagram.RETIRED
    assert "停用" in out["note"]


def test_unbound_node_is_a_state_not_an_error(session):
    """结构分组框、纯静态标注框本来就不绑指标，不该报错。"""
    out = diagram.resolve(session, {"nodes": [node("n1")]}, TODAY)[0]
    assert out["state"] == diagram.UNBOUND
    assert out["value"] is None


def test_blocked_and_missing_input_are_different_states(session):
    """补数据就能算 vs 口径对不上补也没用——混成一种，研究员就不知道该去录数还是查口径。"""
    for fid, status, note in (("F.MISS", "missing_input", "缺少输入：现货"),
                              ("F.BLOCK", "blocked_caliber_mismatch", "价格类型不一致")):
        session.add(Derived(trade_date=TODAY.isoformat(), formula_id=fid, variety="SN",
                            value=None, status=status, note=note,
                            inputs=[], params={}, computed_at=datetime.now(SHANGHAI)))
    session.flush()

    got = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        node("miss", formula="F.MISS"), node("block", formula="F.BLOCK")]}, TODAY)}
    assert got["miss"]["state"] == diagram.MISSING_INPUT
    assert got["block"]["state"] == diagram.BLOCKED
    assert got["miss"]["value"] is None and got["block"]["value"] is None


def test_proxy_flag_survives_to_the_node(session):
    """代理指标必须带着标记到前端——不标就是把推断当实测。"""
    add_series(session, "T.PROXY", unit="点")
    add_points(session, "T.PROXY", [100])
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.PROXY", proxy=True)]}, TODAY)[0]
    assert out["proxy"] is True


# ---------- 取值的几个陷阱 ----------

def test_points_after_the_board_date_are_not_used(session):
    """年频按期末标注会产生晚于今天的点，渲染当日图时不能取用。"""
    add_series(session, "T.FUTURE", frequency="年")
    record(session, ObservationIn(
        series_id="T.FUTURE", value=999,
        as_of=datetime(2026, 12, 31, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="发布时点"), source="测试", entered_by="测试", note="测试"))
    record(session, ObservationIn(
        series_id="T.FUTURE", value=100,
        as_of=datetime(2026, 6, 30, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="发布时点"), source="测试", entered_by="测试", note="测试"))
    session.flush()
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.FUTURE")]}, TODAY)[0]
    assert out["value"] == 100, "不能把 12-31 那个未来点当成最新值"


def test_only_the_latest_revision_feeds_the_change_rate(session):
    """同一时点被修订过时，历史修订不该参与环比——否则环比会算成「自己和自己的旧版本比」。"""
    add_series(session, "T.REV")
    at = datetime(2026, 9, 21, 15, tzinfo=SHANGHAI)
    prev = datetime(2026, 9, 20, 15, tzinfo=SHANGHAI)
    for value, when in ((100, prev), (110, at), (120, at)):  # 同一时点写两次 → 产生修订
        record(session, ObservationIn(series_id="T.REV", value=value, as_of=when,
                                      caliber=Caliber(time_type="发布时点"), source="测试",
                                      entered_by="测试", note="测试"))
    session.flush()
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.REV")]}, TODAY)[0]
    assert out["value"] == 120
    assert out["mom"] == pytest.approx(20.0), "应与 9-20 的 100 比，而不是与同一天的旧修订 110 比"


def test_yoy_only_when_the_frequency_makes_it_meaningful(session):
    """月频回退 12 期算同比；日频没有固定期数，不硬算。"""
    add_series(session, "T.MON", frequency="月")
    add_points(session, "T.MON", [130] + [100] * 12, step_days=30)
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.MON")]}, TODAY)[0]
    assert out["yoy"] == pytest.approx(30.0)

    add_series(session, "T.DAY", frequency="日")
    add_points(session, "T.DAY", [130] + [100] * 12)
    out = diagram.resolve(session, {"nodes": [node("n2", series="T.DAY")]}, TODAY)[0]
    assert out["yoy"] is None, "日频没有固定的「去年同期」期数，不应硬凑一个"


# ---------- 布局模板 ----------

def test_layout_must_have_unique_node_ids(session):
    with pytest.raises(diagram.DiagramError, match="唯一"):
        diagram.save_template(session, "SN", {"name": "x", "layout": {
            "nodes": [node("same"), node("same")]}})
    with pytest.raises(diagram.DiagramError, match="至少"):
        diagram.save_template(session, "SN", {"name": "x", "layout": {"nodes": []}})


def test_first_template_becomes_default_and_heir_is_promoted(session):
    a = diagram.save_template(session, "SN", {"name": "甲", "layout": {"nodes": [node("n1")]}})
    b = diagram.save_template(session, "SN", {"name": "乙", "layout": {"nodes": [node("n1")]}})
    assert a["is_default"] and not b["is_default"]

    left = diagram.delete_template(session, "SN", a["id"])["templates"]
    assert len(left) == 1 and left[0]["is_default"], "默认被删后要顺位，否则默认永远删不掉"


def test_default_layout_is_seeded_on_first_visit(session):
    """第一次进页面给一张摆好的结构图，而不是一张白纸。"""
    tpl = diagram.ensure_default(session, "SN")
    assert tpl["name"]
    nodes = tpl["layout"]["nodes"]
    assert len(nodes) >= 15
    bound = [n for n in nodes if n["binding"]["kind"] != "none"]
    assert len(bound) >= 15, "种子布局应当大部分已绑好指标"
    assert diagram.ensure_default(session, "SN")["id"] == tpl["id"], "重复调用不应再建一份"
    assert len(session.scalars(select(DiagramTemplate)).all()) == 1


def test_layout_stores_bindings_not_values(session):
    """布局里只能存「绑定谁」，不能缓存取数结果——缓存了图就变成又一份会过期的快照。

    `statics` 里的 value 是调研系数（"32%"这种字符串标注），不是实测值，允许存。
    """
    tpl = diagram.ensure_default(session, "SN")
    for n in tpl["layout"]["nodes"]:
        assert set(n) <= {"id", "label", "layer", "x", "y", "w", "h", "binding", "statics"}
        for s in n.get("statics", []):
            assert isinstance(s["value"], str), "静态标注只能是文字，数值必须来自事实层"


def test_derived_node_with_a_value_carries_the_formula_unit(session):
    """derived 表不存单位——单位是公式的属性。取错地方会在真算出值时才崩。"""
    session.add(Derived(trade_date=TODAY.isoformat(), formula_id="BASIS", variety="SN",
                        value=850.0, status="ok", inputs=[], params={},
                        computed_at=datetime.now(SHANGHAI)))
    session.flush()
    out = diagram.resolve(session, {"nodes": [node("n1", formula="BASIS")]}, TODAY)[0]
    assert out["state"] == diagram.OK
    assert out["value"] == 850.0
    assert out["unit"] == "元/吨"
