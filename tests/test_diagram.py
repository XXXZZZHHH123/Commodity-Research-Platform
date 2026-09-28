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


def add_points(session, series_id, points, step_days=1, caliber=None):
    """points 为从新到旧的数值列表。caliber 须与指标登记的一致，否则会被闸门拦下。"""
    for i, value in enumerate(points):
        record(session, ObservationIn(
            series_id=series_id, value=value,
            as_of=datetime.combine(TODAY - timedelta(days=i * step_days), datetime.min.time(),
                                   SHANGHAI).replace(hour=15),
            caliber=caliber or Caliber(time_type="发布时点"), source="测试",
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
    with pytest.raises(diagram.DiagramError, match="列表"):
        diagram.save_template(session, "SN", {"name": "x", "layout": {"nodes": "不是列表"}})


def test_a_new_layout_may_start_empty_but_an_existing_one_cannot_be_emptied(session):
    """从零建图当然是从零个节点开始的——拦掉它就等于没有「新建布局」这条路。

    真正要拦的是把一张**已经有内容**的图存成空的：那通常是误删或前端状态丢了，
    存下去就把研究员摆了半天的结构抹掉，而且不可撤销。
    """
    blank = diagram.save_template(session, "SN", {"name": "白纸", "layout": {"nodes": []}})
    assert blank["layout"]["nodes"] == []

    filled = diagram.save_template(session, "SN", {"id": blank["id"], "name": "白纸",
                                                   "layout": {"nodes": [node("n1")]}})
    assert len(filled["layout"]["nodes"]) == 1

    with pytest.raises(diagram.DiagramError, match="不能存成空的"):
        diagram.save_template(session, "SN", {"id": blank["id"], "name": "白纸",
                                              "layout": {"nodes": []}})


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
    groups = [n for n in nodes if n.get("kind") == "group"]
    tops = [g for g in groups if not g.get("parent")]
    assert len(tops) == 3, "供给端 / 精锡 / 需求端三个顶层分组"
    assert any(g.get("parent") for g in groups), "应当有二级分组"
    assert all(g["binding"]["kind"] == "none" for g in groups), "分组框是背景分区，不绑指标"
    # 每个二级分组的父组必须真实存在，否则渲染时层级算不出来
    ids = {n["id"] for n in nodes}
    assert all(g["parent"] in ids for g in groups if g.get("parent"))
    assert diagram.ensure_default(session, "SN")["id"] == tpl["id"], "重复调用不应再建一份"
    assert len(session.scalars(select(DiagramTemplate)).all()) == 1


def test_layout_stores_bindings_not_values(session):
    """布局里只能存「绑定谁」，不能缓存取数结果——缓存了图就变成又一份会过期的快照。

    `statics` 里的 value 是调研系数（"32%"这种字符串标注），不是实测值，允许存。
    """
    tpl = diagram.ensure_default(session, "SN")
    for n in tpl["layout"]["nodes"]:
        assert set(n) <= {"id", "kind", "label", "layer", "parent",
                          "x", "y", "w", "h", "binding", "statics"}
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


# ---------- Markdown 双向 ----------

SAMPLE_MD = """# 锡产业结构
## 供给端
- 国产锡精矿 [占比 32%] [年体量 5.8-6.5 万金属吨] <!-- bind: T.MINE -->
  - 云南
  - 广西
- 进口锡精矿 [占比 68%]
  - 缅甸矿进口 <!-- bind: T.MM -->
## 精锡
- 国内产量 <!-- bind: T.REFINED -->
- 基差 <!-- bind: BASIS -->
"""


def test_markdown_parses_groups_hierarchy_and_bindings():
    from tin.export import diagram_md as md

    layout = md.parse(SAMPLE_MD)
    by = {n["label"]: n for n in layout["nodes"]}
    assert layout["name"] == "锡产业结构"
    assert by["供给端"]["kind"] == "group" and by["精锡"]["kind"] == "group"
    assert by["国产锡精矿"]["binding"] == {"kind": "series", "series_id": "T.MINE"}
    assert by["基差"]["binding"] == {"kind": "derived", "formula_id": "BASIS"}
    assert by["国产锡精矿"]["statics"] == [{"label": "占比", "value": "32%"},
                                            {"label": "年体量", "value": "5.8-6.5 万金属吨"}]
    # 缩进即父子，父子之间自动连线
    assert {"from": by["国产锡精矿"]["id"], "to": by["云南"]["id"]} in layout["edges"]
    # 没有 bind 注释的节点一律未绑定——导入给不了绑定，那是「建议参考」的活
    assert by["云南"]["binding"]["kind"] == "none"


def test_markdown_round_trip_is_lossless():
    """导出再导入必须一字不差。

    曾经按「层级, y」排序输出，结果同层节点被排在一起、父子不再相邻，
    导回来「云南」挂到了「进口锡精矿」名下——静默认错爹，比报错难发现得多。
    """
    from tin.export import diagram_md as md

    first = md.parse(SAMPLE_MD)
    again = md.parse(md.dump(first, first["name"]))
    key = [(n["label"], n["binding"], n.get("statics", []), n.get("kind")) for n in first["nodes"]]
    assert key == [(n["label"], n["binding"], n.get("statics", []), n.get("kind"))
                   for n in again["nodes"]]

    def pairs(layout):
        ids = {n["id"]: n["label"] for n in layout["nodes"]}
        return sorted((ids[e["from"]], ids[e["to"]]) for e in layout["edges"])

    assert pairs(first) == pairs(again)


def test_child_is_never_placed_above_its_parent():
    """子节点排到父节点上方的话，连线倒着走，看图的人会以为物料倒流。"""
    from tin.export import diagram_md as md

    layout = md.parse(SAMPLE_MD)
    by = {n["label"]: n for n in layout["nodes"]}
    assert by["缅甸矿进口"]["y"] >= by["进口锡精矿"]["y"]
    assert by["云南"]["y"] >= by["国产锡精矿"]["y"]


def test_markdown_without_any_list_item_fails_loudly():
    from tin.export import diagram_md as md

    with pytest.raises(md.MarkdownError, match="没有解析出任何节点"):
        md.parse("# 标题\n一段正文，没有列表项。")


# ---------- 指标推荐 ----------

def test_suggestion_requires_a_name_hit(session):
    """没有名称命中就不推荐。

    早期版本按「判断引用过 + 有数据」兜底，结果十几个节点的 Top3 是同样三条，
    纯噪音。给不出推荐时明说，比给三条不相干的有用。
    """
    add_series(session, "T.缅甸矿进口量")
    add_points(session, "T.缅甸矿进口量", [100])
    add_series(session, "T.完全无关的指标")
    add_points(session, "T.完全无关的指标", [100])

    hit = diagram.suggest(session, "SN", "缅甸矿进口")
    assert [r["series_id"] for r in hit] == ["T.缅甸矿进口量"]
    assert diagram.suggest(session, "SN", "镀锡板 · 马口铁") == []


def test_generic_words_do_not_drive_suggestions(session):
    """「中国」「日度」这类词满天飞。

    实测教训：「冶炼产量·中国」曾因为「中国」二字被推荐了「锡锭升贴水：中国」。
    """
    add_series(session, "T.锡锭升贴水中国")
    add_points(session, "T.锡锭升贴水中国", [1])
    assert diagram.suggest(session, "SN", "冶炼产量 · 中国") == []


def test_empty_series_ranks_below_populated_ones(session):
    """0 条数据的指标可以出现，但必须排在后面并注明——否则等于推荐一个空框。"""
    add_series(session, "T.焊料开工率")            # 有名字没数据
    add_series(session, "T.焊料开工率历史")
    add_points(session, "T.焊料开工率历史", [1, 2, 3])

    got = diagram.suggest(session, "SN", "焊料开工率")
    assert got[0]["series_id"] == "T.焊料开工率历史"
    empty = next(r for r in got if r["series_id"] == "T.焊料开工率")
    assert "尚无数据" in empty["why"]


def test_suggestion_marks_series_already_used_in_this_diagram(session):
    add_series(session, "T.社会库存")
    add_points(session, "T.社会库存", [1])
    got = diagram.suggest(session, "SN", "社会库存", bound={"T.社会库存"})
    assert got[0]["already_bound"] is True


def test_change_carries_both_delta_and_percent(session):
    """百分比看不出量级。库存从 100 涨到 110 与从 10000 涨到 11000 都是 +10%，
    但研究员关心的是差了多少吨。"""
    add_series(session, "T.DELTA", unit="吨")
    add_points(session, "T.DELTA", [11000, 10000])
    out = diagram.resolve(session, {"nodes": [node("n1", series="T.DELTA")]}, TODAY)[0]
    assert out["delta"] == 1000
    assert out["mom"] == pytest.approx(10.0)
    assert "spark" not in out, "迷你走势已去掉，节点上用差值与方向表达变化"


# ---------- 多级分组 ----------

def test_markdown_hash_depth_becomes_group_nesting():
    """`##` 是一级分组，`###` 是它的子分组——井号数即层级，研究员不用学新语法。"""
    from tin.export import diagram_md as md

    layout = md.parse("""# 结构
## 供给端
### 矿端
- 国产锡精矿 <!-- bind: T.A -->
### 冶炼
- 冶炼产量
## 需求端
- 锡焊料
""")
    by = {n["label"]: n for n in layout["nodes"]}
    assert by["矿端"]["parent"] == by["供给端"]["id"]
    assert by["冶炼"]["parent"] == by["供给端"]["id"]
    assert by["供给端"]["parent"] is None
    assert by["国产锡精矿"]["parent"] == by["矿端"]["id"]
    # 纯容器组要包住它的子组，否则画布上子组会跑到框外
    assert by["供给端"]["x"] <= by["矿端"]["x"]
    assert by["供给端"]["y"] <= by["矿端"]["y"]


def test_group_membership_is_explicit_not_geometric():
    """归属靠 parent 字段，不靠坐标包含。

    几何判定下把节点拖出框就悄悄脱组了，而且没有任何提示——归属变化必须是显式的。
    """
    from tin.export import diagram_md as md

    layout = md.parse("# 结构\n## 甲\n- 节点A <!-- bind: T.A -->\n")
    node = next(n for n in layout["nodes"] if n["label"] == "节点A")
    group = next(n for n in layout["nodes"] if n["label"] == "甲")
    node["x"], node["y"] = 9999, 9999          # 拖到天边去
    out = md.dump(layout, "结构")
    assert "## 甲" in out and "- 节点A" in out, "坐标变了不影响归属"
    assert node["parent"] == group["id"]


def test_nested_markdown_round_trip():
    from tin.export import diagram_md as md

    src = "# 结构\n## 供给端\n### 矿端\n- 甲 <!-- bind: T.A -->\n### 冶炼\n- 乙\n## 需求端\n- 丙\n"
    first = md.parse(src)
    again = md.parse(md.dump(first, first["name"]))
    key = lambda L: sorted((n["label"], n.get("kind"), n["binding"]["kind"]) for n in L["nodes"])
    assert key(first) == key(again)


# ---------- 节点详情 ----------

def test_detail_carries_caliber_source_and_history(session):
    """详情的重点不是再报一遍数值，是这个数字凭什么可信。"""
    session.add(Indicator(
        series_id="T.DETAIL", name="测试指标", variety="SN", category="矿端",
        caliber={"time_type": "发布时点", "tc_grade": "40度", "note": "测试口径"},
        unit="元/吨", source="Mysteel", source_url="https://example.com/x",
        frequency="周", fetch_mode="manual", phase="P1", vendor_code="ID999"))
    session.flush()
    add_points(session, "T.DETAIL", [110, 100], step_days=7,
               caliber=Caliber(time_type="发布时点", tc_grade="40度"))

    d = diagram.detail(session, "T.DETAIL", TODAY)
    labels = {c["label"]: c["value"] for c in d["caliber"]}
    assert labels["时点类型"] == "发布时点"
    assert labels["TC 品位"] == "40度"      # 口径字典里的中文标签，不是裸 key
    assert labels["备注"] == "测试口径"
    assert d["vendor_code"] == "ID999" and d["source_url"].startswith("https://")
    assert [h["value"] for h in d["history"]] == [100, 110], "走势按时间正序"
    assert d["fetch_mode"] == "manual"


def test_detail_lists_revisions_so_silent_restatements_are_visible(session):
    """数据商回溯改数是"数字变了但没人告诉你"，修订记录是唯一线索。"""
    add_series(session, "T.REV2")
    at = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI)
    for v in (100, 120):
        record(session, ObservationIn(series_id="T.REV2", value=v, as_of=at,
                                      caliber=Caliber(time_type="发布时点"), source="测试",
                                      entered_by="测试", note="测试"))
    session.flush()
    d = diagram.detail(session, "T.REV2", TODAY)
    assert len(d["revisions"]) == 1
    assert d["revisions"][0]["value"] == 120 and d["revisions"][0]["revision"] == 1
    assert [h["value"] for h in d["history"]] == [120], "走势只用最新修订"


def test_detail_of_a_derived_node_explains_the_formula(session):
    d = diagram.detail(session, "BASIS", TODAY)
    assert d["kind"] == "derived"
    assert "现货" in d["expression"] and "结算价" in d["expression"]
    assert "4 小时" in d["tolerance"], "容差要写出来——它正是阻断与否的依据"


def test_detail_of_unknown_series_fails_clearly(session):
    with pytest.raises(diagram.DiagramError, match="指标不存在"):
        diagram.detail(session, "T.NOPE", TODAY)


# ---------- 跨来源交叉校验 ----------

def test_crosscheck_compares_two_sources_of_the_same_fact(session):
    """有价值的校验是跨来源的。

    实测：SMM 平衡表内部「产量+进口−出口−表观消费 = 库存变化」**恒等于 0**——
    因为表观消费本就是倒算的，校验它是同义反复，永远抓不到任何问题。
    而同一事实的两家来源实测差 −5.3% ~ +8.8%，那才是信号。
    """
    for sid in ("SMM.a10167090", "MYSTEEL.ID01517452"):
        add_series(session, sid, frequency="月", unit="吨")
    add_points(session, "SMM.a10167090", [15290, 15430], step_days=30)
    add_points(session, "MYSTEEL.ID01517452", [16152, 15703], step_days=30)

    out = {c["id"]: c for c in diagram.crosscheck(session, "SN", TODAY)}
    prod = out["prod_cn"]
    assert prod["state"] == diagram.STALE, "两家差 5% 以上，超出 ±3% 容差"
    assert "超出容差" in prod["summary"]
    assert prod["points"][0]["rel"] == pytest.approx(-5.34, abs=0.1)


def test_crosscheck_flags_identical_sources_as_redundant(session):
    """两家完全一致说明转载同一来源，值得指出——不必双边维护。"""
    for sid in ("SMM.a10005167", "MYSTEEL.CM0000138665"):
        add_series(session, sid, frequency="月", unit="吨")
    add_points(session, "SMM.a10005167", [17431, 16831], step_days=30)
    add_points(session, "MYSTEEL.CM0000138665", [17431, 16831], step_days=30)

    out = {c["id"]: c for c in diagram.crosscheck(session, "SN", TODAY)}
    assert out["mine_import"]["state"] == diagram.OK
    assert "同一来源" in out["mine_import"]["summary"]


def test_crosscheck_without_overlapping_dates_says_so(session):
    out = {c["id"]: c for c in diagram.crosscheck(session, "SN", TODAY)}
    assert out["social_stock"]["state"] == diagram.NO_DATA
    assert "无法比较" in out["social_stock"]["summary"]


# ---------- 判断依赖 ----------

def test_nodes_carry_their_role_in_the_current_judgment(session):
    """回答"我这个判断靠产业链上哪几个环节"——角色要跟着节点到前端。"""
    from tin.jobs.seed import seed_judgment
    from tin.models import JudgmentSeriesRef

    add_series(session, "T.STOCK")
    add_series(session, "T.VIX")
    j = seed_judgment(session)
    session.add_all([
        JudgmentSeriesRef(judgment_id=j.id, ref_type="falsifier", ref_id="f1", series_id="T.STOCK"),
        JudgmentSeriesRef(judgment_id=j.id, ref_type="threshold", ref_id="t1", series_id="T.VIX"),
    ])
    session.flush()

    roles, meta = diagram.judgment_roles(session)
    assert "证伪条件" in roles["T.STOCK"]
    assert "阈值" in roles["T.VIX"]
    assert meta["version"] == j.version, "前端要能说出高亮依据的是哪一版判断"

    out = diagram.resolve(session, {"nodes": [
        node("n1", series="T.VIX"), node("n2", series="T.STOCK")]}, TODAY)
    assert "阈值" in out[0]["roles"]

    out2 = diagram.resolve(session, {"nodes": [node("n3", series="T.OTHER")]}, TODAY)
    assert out2[0]["roles"] == [], "没被判断引用的节点角色为空，而不是缺字段"


def test_roles_follow_the_current_judgment_version_not_every_version_ever(session):
    """判断改版，重点关注必须跟着换——这是 P1「判断是一份带时间的快照」的直接要求。

    旧版本归档时引用行仍留在 judgment_series_refs 里。取全表等于把历年所有版本的
    引用并起来：重点关注只增不减，研究员换了判断，图上还亮着上一版关心的环节。
    """
    from tin.models import Judgment, JudgmentSeriesRef

    add_series(session, "T.OLD")
    add_series(session, "T.NEW")

    def a_judgment(version, status):
        row = Judgment(variety="SN", version=version, author="测试", written_at=TODAY,
                       review_period_days=30, review_due=TODAY, contradiction={},
                       marginal_focus=[], pricing_power={}, whitelist=[], thresholds=[],
                       falsifiers=[], status=status, unstructured=[], review_warnings=[],
                       created_at=datetime.now(SHANGHAI))
        session.add(row)
        session.flush()
        return row

    old = a_judgment(1, "已归档")
    session.add(JudgmentSeriesRef(judgment_id=old.id, ref_type="falsifier",
                                  ref_id="f1", series_id="T.OLD"))
    new = a_judgment(2, "生效")
    session.add(JudgmentSeriesRef(judgment_id=new.id, ref_type="threshold",
                                  ref_id="t1", series_id="T.NEW"))
    session.flush()

    roles, meta = diagram.judgment_roles(session)
    assert meta["version"] == 2
    assert "阈值" in roles["T.NEW"]
    assert "T.OLD" not in roles, "上一版判断关心的环节不该还亮着"


def test_roles_are_empty_when_no_judgment_exists(session):
    roles, meta = diagram.judgment_roles(session)
    assert roles == {} and meta == {}


# ---------- 指标被谁在用 ----------

def test_usage_tells_you_whether_anyone_depends_on_this_indicator(session):
    """「这条指标要不要维护」——444 行列表回答不了，靠的是有没有人在用。"""
    from tin.jobs.seed import seed_judgment
    from tin.models import JudgmentSeriesRef

    add_series(session, "T.USED")
    j = seed_judgment(session)
    session.add(JudgmentSeriesRef(judgment_id=j.id, ref_type="falsifier",
                                  ref_id="f1", series_id="T.USED"))
    diagram.save_template(session, "SN", {"name": "布局甲", "layout": {
        "nodes": [node("n1", series="T.USED", label="社会库存")], "edges": []}})
    session.flush()

    u = diagram.usage(session, "T.USED")
    assert u["judgment"] == [{"role": "证伪条件", "ref_id": "f1"}]
    assert u["nodes"][0]["node"] == "社会库存"
    assert u["nodes"][0]["template"] == "布局甲"

    add_series(session, "T.ORPHAN")
    orphan = diagram.usage(session, "T.ORPHAN")
    assert orphan["judgment"] == [] and orphan["nodes"] == [], "没人用就是没人用，不编造引用"


# ---------- 分组合计 ----------

def group(gid, op=None, parent=None):
    b = {"kind": "agg", "op": op} if op else {"kind": "none"}
    return {"id": gid, "kind": "group", "label": gid, "parent": parent,
            "x": 0, "y": 0, "w": 400, "h": 300, "binding": b, "statics": []}


def test_group_sums_its_children(session):
    for sid in ("G.A", "G.B"):
        add_series(session, sid, unit="吨", frequency="月")
    add_points(session, "G.A", [100, 90], step_days=30)
    add_points(session, "G.B", [25, 20], step_days=30)

    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1", "sum"),
        {**node("n1", series="G.A"), "parent": "g1"},
        {**node("n2", series="G.B"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["value"] == 125
    assert out["g1"]["unit"] == "吨"
    assert "2 项合计" in out["g1"]["note"]


def test_group_refuses_to_add_across_units(session):
    """实物吨和金属吨差 2–3 倍，加出来的数看起来完全正常——这正是 FR-5.2 要防的。"""
    add_series(session, "G.T", unit="吨", frequency="月")
    add_series(session, "G.M", unit="金属吨", frequency="月")
    add_points(session, "G.T", [100], step_days=30)
    add_points(session, "G.M", [40], step_days=30)

    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1", "sum"),
        {**node("n1", series="G.T"), "parent": "g1"},
        {**node("n2", series="G.M"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["state"] == diagram.BLOCKED
    assert out["g1"]["value"] is None, "阻断时必须留空，不能给 140"
    assert "单位不一致" in out["g1"]["note"]


def test_group_refuses_to_add_across_frequencies(session):
    add_series(session, "G.D", unit="吨", frequency="日")
    add_series(session, "G.MO", unit="吨", frequency="月")
    add_points(session, "G.D", [10])
    add_points(session, "G.MO", [300], step_days=30)

    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1", "sum"),
        {**node("n1", series="G.D"), "parent": "g1"},
        {**node("n2", series="G.MO"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["state"] == diagram.BLOCKED
    assert "频率不一致" in out["g1"]["note"]


def test_partial_sum_says_it_is_partial(session):
    """少算了一项的合计，看起来和完整的合计一模一样——必须说出来。"""
    add_series(session, "G.A", unit="吨", frequency="月")
    add_series(session, "G.EMPTY", unit="吨", frequency="月")
    add_points(session, "G.A", [100], step_days=30)

    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1", "sum"),
        {**node("n1", series="G.A"), "parent": "g1"},
        {**node("n2", series="G.EMPTY"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["value"] == 100
    assert out["g1"]["state"] == diagram.STALE, "不完整的合计不能标成正常"
    assert "未计入" in out["g1"]["note"]


def test_group_share_ranks_children(session):
    for sid in ("G.A", "G.B"):
        add_series(session, sid, unit="吨", frequency="月")
    add_points(session, "G.A", [30], step_days=30)
    add_points(session, "G.B", [70], step_days=30)

    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1", "share"),
        {**node("n1", series="G.A", label="小的"), "parent": "g1"},
        {**node("n2", series="G.B", label="大的"), "parent": "g1"}]}, TODAY)}
    parts = out["g1"]["parts"]
    assert [p["label"] for p in parts] == ["大的", "小的"], "占比要从大到小排"
    assert parts[0]["pct"] == pytest.approx(70.0)


def test_group_without_children_says_so(session):
    out = {n["node_id"]: n for n in diagram.resolve(
        session, {"nodes": [group("g1", "sum")]}, TODAY)}
    assert out["g1"]["state"] == diagram.UNBOUND
    assert "没有直接子节点" in out["g1"]["note"]


def test_a_plain_group_still_has_no_value(session):
    """没写 agg 的分组框就是背景分区，不该凭空长出一个数。"""
    add_series(session, "G.A", unit="吨", frequency="月")
    add_points(session, "G.A", [100], step_days=30)
    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        group("g1"), {**node("n1", series="G.A"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["state"] == diagram.UNBOUND
    assert out["g1"]["value"] is None


def test_sum_excludes_parts_of_other_items(session):
    """缅甸矿进口 ⊂ 进口锡精矿：单位频率完全一样，守卫拦不住，求和会重复计一次。

    包含关系在数据里是显式的——同组内一条 A→B 的连线就是"B 是 A 的一部分"。
    """
    for sid in ("G.TOTAL", "G.PART", "G.OTHER"):
        add_series(session, sid, unit="吨", frequency="月")
    add_points(session, "G.TOTAL", [100], step_days=30)
    add_points(session, "G.PART", [30], step_days=30)     # 是 TOTAL 的一部分
    add_points(session, "G.OTHER", [50], step_days=30)

    layout = {"nodes": [
        group("g1", "sum"),
        {**node("whole", series="G.TOTAL"), "parent": "g1"},
        {**node("part", series="G.PART"), "parent": "g1"},
        {**node("other", series="G.OTHER"), "parent": "g1"}],
        "edges": [{"from": "whole", "to": "part"}]}
    out = {n["node_id"]: n for n in diagram.resolve(session, layout, TODAY)}
    assert out["g1"]["value"] == 150, "不能算成 180 —— 那 30 吨被数了两遍"
    assert "重复计" in out["g1"]["note"]


def test_group_sum_can_pick_which_children_count(session):
    """一个分组里常混着量和价（矿端既有产量也有加工费 TC）。

    「全体子节点」这个默认只在同质分组里成立；混着就永远算不出来，
    而那个分组恰恰是最想看合计的。
    """
    add_series(session, "P.A", unit="吨", frequency="月")
    add_series(session, "P.B", unit="吨", frequency="月")
    add_series(session, "P.PRICE", unit="元/吨", frequency="周")
    add_points(session, "P.A", [100], step_days=30)
    add_points(session, "P.B", [40], step_days=30)
    add_points(session, "P.PRICE", [17500], step_days=7)

    nodes = [group("g1", "sum"),
             {**node("a", series="P.A"), "parent": "g1"},
             {**node("b", series="P.B"), "parent": "g1"},
             {**node("price", series="P.PRICE"), "parent": "g1"}]

    # 默认全体 → 单位不一致，阻断
    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": nodes}, TODAY)}
    assert out["g1"]["state"] == diagram.BLOCKED

    # 手选两项 → 算得出来，并标明是手选的
    nodes[0]["binding"]["members"] = ["a", "b"]
    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": nodes}, TODAY)}
    assert out["g1"]["state"] == diagram.OK
    assert out["g1"]["value"] == 140
    assert "手选" in out["g1"]["note"], "手选的合计不是全量，必须说出来"


def test_picked_members_that_left_the_group_are_reported(session):
    """节点被拖走或删掉后，手选名单会指向不存在的成员——要说清楚，不能静默算成 0。"""
    add_series(session, "P.A", unit="吨", frequency="月")
    add_points(session, "P.A", [100], step_days=30)
    g = group("g1", "sum")
    g["binding"]["members"] = ["已经不在了"]
    out = {n["node_id"]: n for n in diagram.resolve(session, {"nodes": [
        g, {**node("a", series="P.A"), "parent": "g1"}]}, TODAY)}
    assert out["g1"]["state"] == diagram.UNBOUND
    assert "手选的参与项都不在" in out["g1"]["note"]
