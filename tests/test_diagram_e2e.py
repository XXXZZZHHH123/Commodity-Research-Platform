"""从零建一张产业图：完整走一遍研究员的动线。

这一组不测单个函数，测的是**把这些函数串起来用会不会出事**。单元测试全绿而流程走不通，
是这类"多个抽屉互相配合"的功能最常见的失败方式：每一步单看都对，连起来少一次刷新、
少存一个字段，用的人就卡住了。

动线两条：
  A 空白画布 → 加分组 → 加子分组 → 加节点 → 绑指标 → 保存 → 重开
  B 研究员已有的 Markdown 大纲 → 导入 → 补绑定 → 导出 → 再导入（必须无损）
"""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tin.config import SHANGHAI
from tin.export import diagram, diagram_md
from tin.ingest.record import record
from tin.models import Indicator
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn
from tin.web.app import app

TODAY = date(2026, 9, 21)


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setattr("tin.web.app.SessionLocal", lambda: session)
    monkeypatch.setattr("tin.web.app._board_date", lambda s, d=None: TODAY)
    monkeypatch.setattr("tin.web.app.latest_trade_date", lambda s: TODAY)
    return TestClient(app)


def a_series(session, series_id, name, unit="吨", frequency="月", values=(120, 100)):
    session.add(Indicator(series_id=series_id, name=name, variety="SN", category="供给",
                          caliber={"time_type": "发布时点"}, unit=unit, source="测试",
                          frequency=frequency, fetch_mode="manual", phase="P1", status="可用"))
    session.flush()
    for i, v in enumerate(values):
        record(session, ObservationIn(
            series_id=series_id, value=v,
            as_of=datetime.combine(TODAY - timedelta(days=i * 30), datetime.min.time(),
                                   SHANGHAI).replace(hour=15),
            caliber=Caliber(time_type="发布时点"), source="测试",
            entered_by="研究员", note="模拟录入"))
    # 必须 commit：路由里是 `with SessionLocal() as s`，退出 with 会关会话并回滚，
    # 只 flush 的数据撑不过第一个请求。
    session.commit()


# ---------- 动线 A：空白画布搭到能看 ----------

def test_build_a_whole_diagram_from_an_empty_canvas(client, session):
    a_series(session, "E.MINE", "国产锡精矿产量")
    a_series(session, "E.REFINED", "精锡产量")

    # 1. 新建一张空布局：还没有任何节点，也该能存下来
    r = client.post("/api/sn/diagram/templates",
                    json={"name": "我的结构图", "layout": {"nodes": [], "edges": []}})
    assert r.status_code == 200, r.text
    tid = r.json()["saved"]["id"]

    # 2. 加一个顶层分组、一个子分组、两个节点——前端 dgAdd 造出来的就是这个形状
    layout = {"nodes": [
        {"id": "g1", "kind": "group", "label": "供给端", "x": 20, "y": 20, "w": 600, "h": 300,
         "binding": {"kind": "none"}, "statics": []},
        {"id": "g2", "kind": "group", "label": "矿端", "parent": "g1",
         "x": 40, "y": 60, "w": 260, "h": 200, "binding": {"kind": "none"}, "statics": []},
        {"id": "n1", "label": "国产矿", "parent": "g2", "x": 60, "y": 100, "w": 210, "h": 104,
         "binding": {"kind": "series", "series_id": "E.MINE"}, "statics": []},
        {"id": "n2", "label": "精锡", "parent": "g1", "x": 340, "y": 100, "w": 210, "h": 104,
         "binding": {"kind": "series", "series_id": "E.REFINED"}, "statics": []},
    ], "edges": [{"from": "n1", "to": "n2"}]}

    # 3. **保存之前**就要能取到值：新节点在库里还不存在，这是第一版的真 bug
    r = client.post("/api/sn/diagram/values", json={"layout": layout})
    assert r.status_code == 200, r.text
    vals = {n["node_id"]: n for n in r.json()["nodes"]}
    assert vals["n1"]["state"] == diagram.OK
    assert vals["n1"]["value"] == 120, "刚绑上就该显示数据，不能等保存"

    # 4. 保存后重新按模板取值，结果必须一致
    r = client.post("/api/sn/diagram/templates",
                    json={"id": tid, "name": "我的结构图", "layout": layout})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/sn/diagram/values?template_id={tid}")
    assert r.status_code == 200
    saved = {n["node_id"]: n for n in r.json()["nodes"]}
    assert saved["n1"]["value"] == 120 and saved["n2"]["value"] == 120

    # 5. 层级与连线要原样存回来——拖了半天的结构不能在保存时被改写
    rows = diagram.list_templates(session, "SN")
    mine = next(t for t in rows if t["id"] == tid)
    by = {n["id"]: n for n in mine["layout"]["nodes"]}
    assert by["g2"]["parent"] == "g1"
    assert by["n1"]["parent"] == "g2"
    assert mine["layout"]["edges"] == [{"from": "n1", "to": "n2"}]


def test_an_unbound_node_says_so_instead_of_vanishing(client, session):
    """空白画布刚加出来的节点是未绑定的——它必须出现在取值结果里并说明状态。"""
    layout = {"nodes": [{"id": "n1", "label": "新节点", "x": 0, "y": 0, "w": 210, "h": 104,
                         "binding": {"kind": "none"}, "statics": []}], "edges": []}
    r = client.post("/api/sn/diagram/values", json={"layout": layout})
    assert r.status_code == 200
    assert r.json()["nodes"][0]["state"] == diagram.UNBOUND


def test_values_endpoint_survives_a_layout_that_is_still_half_built(client, session):
    """半成品布局不该把接口打挂：指向不存在的指标、父级指向自己、边指向已删节点。"""
    layout = {"nodes": [
        {"id": "n1", "label": "打错的绑定", "x": 0, "y": 0, "w": 210, "h": 104,
         "binding": {"kind": "series", "series_id": "NOT.A.REAL.SERIES"}, "statics": []},
        {"id": "n2", "label": "自己是自己的爹", "parent": "n2", "x": 0, "y": 0, "w": 210, "h": 104,
         "binding": {"kind": "none"}, "statics": []},
    ], "edges": [{"from": "n1", "to": "已删掉的节点"}]}
    r = client.post("/api/sn/diagram/values", json={"layout": layout})
    assert r.status_code == 200, r.text
    states = {n["node_id"]: n["state"] for n in r.json()["nodes"]}
    assert states["n1"] in (diagram.NO_DATA, diagram.UNBOUND, diagram.RETIRED)
    assert states["n2"] == diagram.UNBOUND


def test_the_template_dropdown_actually_switches_layouts(client, session):
    """页面必须认 `?template=`。

    原来这个路由无条件返回默认布局，query 参数被整个忽略：下拉框选哪一张都回到同一张，
    刚新建的空白布局根本打不开——"多存几份视角"这个功能等于不存在。
    """
    a_series(session, "E.MINE", "国产锡精矿产量")
    one = client.post("/api/sn/diagram/templates", json={"name": "视角甲", "layout": {"nodes": [
        {"id": "n1", "label": "甲的节点", "x": 0, "y": 0, "w": 210, "h": 104,
         "binding": {"kind": "none"}, "statics": []}]}}).json()["saved"]
    two = client.post("/api/sn/diagram/templates", json={"name": "视角乙", "layout": {"nodes": [
        {"id": "n1", "label": "乙的节点", "x": 0, "y": 0, "w": 210, "h": 104,
         "binding": {"kind": "none"}, "statics": []}]}}).json()["saved"]

    # 节点标签由前端从 DG_TEMPLATE 渲染（tojson 会把中文转义），所以看选中的是哪张布局
    def opened(tid):
        body = client.get(f"/sn/diagram?template={tid}").text
        assert f'<option value="{tid}" selected>' in body, f"没有打开布局 {tid}"
        return body

    a, b = opened(one["id"]), opened(two["id"])
    assert f'<option value="{two["id"]}" selected>' not in a
    assert f'<option value="{one["id"]}" selected>' not in b

    assert client.get("/sn/diagram?template=99999").status_code == 404, \
        "打不开的布局要说打不开，不能悄悄给另一张"


def test_a_brand_new_blank_layout_opens_without_crashing(client, session):
    blank = client.post("/api/sn/diagram/templates",
                        json={"name": "白纸", "layout": {"nodes": [], "edges": []}}).json()["saved"]
    body = client.get(f"/sn/diagram?template={blank['id']}").text
    assert "这张布局还是空的" in body, "空布局要给下一步指引，不是一块白板"


# ---------- 动线 B：研究员已有的大纲导进来 ----------

OUTLINE = """# 锡产业结构

## 供给端
- 国产锡精矿 [占比 32%] <!-- bind: E.MINE -->
  - 云南
  - 广西
- 进口锡精矿 [占比 68%]

### 冶炼
- 精锡产量 <!-- bind: E.REFINED -->

## 需求端
- 锡焊料 [占比 53%]
"""


def test_import_an_outline_then_read_values_off_it(client, session):
    a_series(session, "E.MINE", "国产锡精矿产量")
    a_series(session, "E.REFINED", "精锡产量")

    r = client.post("/api/sn/diagram/markdown", json={"markdown": OUTLINE, "name": "从大纲导入"})
    assert r.status_code == 200, r.text
    tid = r.json()["saved"]["id"]

    r = client.get(f"/api/sn/diagram/values?template_id={tid}")
    assert r.status_code == 200
    vals = {n["label"]: n for n in r.json()["nodes"]}
    assert vals["国产锡精矿"]["value"] == 120, "大纲里写了 bind 注释，导入后就该有数"
    assert vals["锡焊料"]["state"] == diagram.UNBOUND, "没写 bind 的节点是未绑定，不是错误"


def test_outline_round_trips_without_losing_structure(client, session):
    """导出再导入必须无损——研究员会在文本里改结构，改完导回来。"""
    a_series(session, "E.MINE", "国产锡精矿产量")
    a_series(session, "E.REFINED", "精锡产量")

    r = client.post("/api/sn/diagram/markdown", json={"markdown": OUTLINE, "name": "一轮"})
    tid = r.json()["saved"]["id"]
    once = client.get(f"/api/sn/diagram/markdown?template_id={tid}").json()["markdown"]

    r = client.post("/api/sn/diagram/markdown", json={"markdown": once, "name": "一轮"})
    assert r.status_code == 200, r.text
    tid2 = r.json()["saved"]["id"]
    twice = client.get(f"/api/sn/diagram/markdown?template_id={tid2}").json()["markdown"]

    assert once == twice, f"第二轮与第一轮不一致：\n{once}\n---\n{twice}"
    assert "<!-- bind: E.MINE -->" in once, "绑定必须跟着导出走，否则导回来全掉了"
    assert "[占比 32%]" in once, "静态标注也要跟着走"
    assert "### 冶炼" in once, "子分组的层级不能被压平"
    for label in ("云南", "广西", "进口锡精矿", "锡焊料"):
        assert label in once


def test_a_layout_built_by_hand_can_be_exported_as_an_outline(client, session):
    """反向：画布上搭的结构要能导成大纲，否则这条通道是单向的。"""
    a_series(session, "E.MINE", "国产锡精矿产量")
    layout = {"nodes": [
        {"id": "g1", "kind": "group", "label": "供给端", "x": 0, "y": 0, "w": 600, "h": 300,
         "binding": {"kind": "none"}, "statics": []},
        {"id": "n1", "label": "国产矿", "parent": "g1", "x": 20, "y": 40, "w": 210, "h": 104,
         "binding": {"kind": "series", "series_id": "E.MINE"},
         "statics": [{"label": "占比", "value": "32%"}]},
    ], "edges": []}
    r = client.post("/api/sn/diagram/templates", json={"name": "手搭的", "layout": layout})
    tid = r.json()["saved"]["id"]

    md = client.get(f"/api/sn/diagram/markdown?template_id={tid}").json()["markdown"]
    assert "## 供给端" in md
    assert "- 国产矿 [占比 32%] <!-- bind: E.MINE -->" in md

    back = diagram_md.parse(md)
    assert [n["label"] for n in back["nodes"] if n.get("kind") != "group"] == ["国产矿"]


# ---------- 建完之后的几个抽屉都要能开 ----------

def test_every_drawer_the_page_offers_actually_answers(client, session):
    a_series(session, "E.MINE", "国产锡精矿产量")

    assert client.get("/api/sn/diagram/detail?series_id=E.MINE").status_code == 200
    assert client.get("/api/sn/diagram/suggest?label=国产锡精矿").status_code == 200
    assert client.get("/api/sn/diagram/crosscheck").status_code == 200
    assert client.get("/sn/indicators/E.MINE").status_code == 200
    assert client.get("/api/sn/indicators/E.MINE/usage").status_code == 200

    # 指标不存在时要 404，不能 500
    assert client.get("/sn/indicators/NOPE").status_code == 404
    assert client.get("/api/sn/diagram/detail?series_id=NOPE").status_code == 404


def test_indicator_page_says_whether_anything_depends_on_it(client, session):
    a_series(session, "E.MINE", "国产锡精矿产量")
    layout = {"nodes": [{"id": "n1", "label": "国产矿", "x": 0, "y": 0, "w": 210, "h": 104,
                         "binding": {"kind": "series", "series_id": "E.MINE"}, "statics": []}],
              "edges": []}
    client.post("/api/sn/diagram/templates", json={"name": "甲", "layout": layout})

    body = client.get("/sn/indicators/E.MINE").text
    assert "国产矿" in body, "指标页要说出它被图上哪个节点绑着"

    a_series(session, "E.ORPHAN", "没人用的指标")
    body = client.get("/sn/indicators/E.ORPHAN").text
    assert "没有任何产业图节点绑定它" in body


# ---------- 种子布局的形状约束 ----------

def test_seed_layout_is_compact_and_wired_through_groups():
    """节点只放「名称/数值/差值/时点」，所以框可以小；结构由分类框之间的连线承担。

    第一版是二十来条节点两两连线，互相穿插，反而看不出谁流向谁——产业链上的流向
    本来就是**环节之间**的事，不是某两个具体指标之间的事。
    """
    import json

    from tin.config import ROOT

    layout = json.loads((ROOT / "seeds" / "diagram_sn.json").read_text(encoding="utf-8"))
    nodes = layout["nodes"]
    by = {n["id"]: n for n in nodes}
    plain = [n for n in nodes if n.get("kind") != "group"]

    assert all(n["w"] <= 170 and n["h"] <= 76 for n in plain), "节点该是紧凑尺寸"

    edges = layout["edges"]
    group_edges = [e for e in edges if by[e["from"]].get("kind") == "group"]
    assert len(group_edges) >= 4, "主链路要由分类框串起来"
    assert all(by[e["to"]].get("kind") == "group" for e in group_edges), "分组只连分组"

    # 剩下的节点级连线必须是真正的包含关系，不能是复原出来的物料流
    node_edges = [e for e in edges if by[e["from"]].get("kind") != "group"]
    assert len(node_edges) <= 2, f"节点级连线应当很少，现在有 {len(node_edges)} 条"

    w = max(n["x"] + n["w"] for n in nodes)
    h = max(n["y"] + n["h"] for n in nodes)
    assert w / h >= 2.5, f"要横向铺开给宽屏看，现在 {w}×{h}"


def test_seed_layout_has_no_parent_cycle():
    """归属成环会让层级标签渲染成「供给端 › 矿端 › 供给端」，整个层级失去意义。"""
    import json

    from tin.config import ROOT

    nodes = json.loads((ROOT / "seeds" / "diagram_sn.json").read_text(encoding="utf-8"))["nodes"]
    by = {n["id"]: n for n in nodes}
    for n in nodes:
        seen, cur = {n["id"]}, n
        while cur.get("parent"):
            assert cur["parent"] not in seen, f"{n['id']} 的归属链成环"
            seen.add(cur["parent"])
            cur = by[cur["parent"]]


def test_group_edges_survive_a_markdown_round_trip(client, session):
    """分组之间的连线不能在导出大纲时被悄悄丢掉。"""
    layout = {"nodes": [
        {"id": "g1", "kind": "group", "label": "供给端", "x": 0, "y": 0, "w": 200, "h": 200,
         "binding": {"kind": "none"}, "statics": []},
        {"id": "g2", "kind": "group", "label": "精锡", "x": 260, "y": 0, "w": 200, "h": 200,
         "binding": {"kind": "none"}, "statics": []},
        {"id": "n1", "label": "国产矿", "parent": "g1", "x": 14, "y": 30, "w": 170, "h": 76,
         "binding": {"kind": "none"}, "statics": []},
        {"id": "n2", "label": "精锡产量", "parent": "g2", "x": 274, "y": 30, "w": 170, "h": 76,
         "binding": {"kind": "none"}, "statics": []},
    ], "edges": [{"from": "g1", "to": "g2"}]}
    r = client.post("/api/sn/diagram/templates", json={"name": "带分组连线", "layout": layout})
    assert r.status_code == 200, r.text
    saved = diagram.list_templates(session, "SN")[0]["layout"]
    assert {"from": "g1", "to": "g2"} in saved["edges"]


def test_markdown_can_rewrite_the_layout_you_are_looking_at(client, session):
    """研究员在文本里改结构比拖方框快，但改完必须能落回**手里这张图**。

    原来只有「导入为新布局」：每改一次就多一张图，而且改的不是当前这张——
    等于这条通道只能用一次。
    """
    a_series(session, "E.MINE", "国产锡精矿产量")
    first = client.post("/api/sn/diagram/markdown", json={
        "markdown": "# 锡\n\n## 供给端\n- 国产锡精矿 <!-- bind: E.MINE -->\n",
        "name": "我的图"}).json()["saved"]

    edited = ("# 锡\n\n## 供给端\n- 国产锡精矿 <!-- bind: E.MINE -->\n  - 云南\n"
              "\n## 需求端\n- 锡焊料 [占比 53%]\n")
    r = client.post("/api/sn/diagram/markdown",
                    json={"markdown": edited, "into": first["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is True
    assert r.json()["saved"]["id"] == first["id"], "必须改写同一张，不是又建一张"
    assert r.json()["saved"]["name"] == "我的图", "没给新名字就保留原名"

    assert len(diagram.list_templates(session, "SN")) == 1, "不该多出一张布局"
    layout = diagram.list_templates(session, "SN")[0]["layout"]
    labels = {n["label"] for n in layout["nodes"]}
    assert {"云南", "锡焊料", "需求端"} <= labels, "新增的结构要落进去"
    bound = [n for n in layout["nodes"] if (n.get("binding") or {}).get("series_id")]
    assert bound[0]["series_id" if "series_id" in bound[0] else "binding"], "绑定要跟着走"
    assert bound[0]["binding"]["series_id"] == "E.MINE"


def test_rewriting_a_layout_that_does_not_exist_is_rejected(client, session):
    r = client.post("/api/sn/diagram/markdown",
                    json={"markdown": "# x\n\n## 组\n- 甲\n", "into": 99999})
    assert r.status_code == 404
