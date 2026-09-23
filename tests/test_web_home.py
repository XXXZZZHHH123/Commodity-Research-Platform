"""首屏工作台（F9）与简报评审（F4）。

这一屏守的是唯一的北极星——「我每天真的会打开它」（SPEC §1.3）。所以测试的重点不是
「字段渲染对不对」，而是三件会让人第二天不再打开的事：
- 简报缺席就整页崩（LLM 没接通是常态，不是异常）；
- 没通过证据校验的论断混进正经论断里（质量警报被藏起来 = 幻觉被当成结论）；
- 评审规则在前端被放宽（「改」不写原因照样过，纠错日志就枯竭了）。

另外守住 `elapsed_seconds` 的端到端透传：§6.3 要靠它识别「秒级采纳 = 没看」，
而这个数只有页面算得出来，服务端从 generated_at 反推必然是错的。
"""

from datetime import date, datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import D
from tests.test_signals import activate, enter, week
from tin.compute.signals import evaluate_signals
from tin.jobs.seed import seed_researcher
from tin.models import BriefRevision, DailyBrief
from tin.web.app import app

CLAIMS = [{
    "text": "主力结算价仍在补库区间内",
    "side": "bull",
    "evidence": [{"series_id": "SHFE.SN.main.settle", "value": 405360.0,
                  "as_of": "2026-09-18T15:00:00+08:00", "playbook_item_id": None,
                  "signal_id": None}],
}]
UNVERIFIED = [{
    "field": "claim",
    "claim": "缅甸复产传闻已经兑现，进口量回到万吨以上",
    "side": "bear",
    "evidence": [{"series_id": "CUSTOMS.2609.import.MM", "value": 12000.0, "as_of": None}],
    "reasons": ["证据 1：给出了数值 12000.0 却与库内不符"],
}]


def client(session, monkeypatch) -> TestClient:
    monkeypatch.setattr("tin.web.app.SessionLocal", lambda: session)
    return TestClient(app)


def make_brief(session, *, status="草稿", claims=None, unverified=None, **over) -> DailyBrief:
    """直接造数据行：简报的生成是 B1 的活，首屏只负责读。"""
    me = seed_researcher(session)
    row = DailyBrief(
        researcher_id=me.id, variety="SN", trade_date=D.isoformat(), status=status,
        tone="区间震荡", stance="逢低做多", summary="沪锡主力在补库区间内窄幅运行。",
        range_low=400000.0, range_high=410000.0, range_series_id="SHFE.SN.main.settle",
        claims=CLAIMS if claims is None else claims,
        unverified=list(unverified or []), playbook_item_ids=[], signal_ids=[],
        model="fake-model", generated_at=datetime.now(timezone.utc), **over)
    session.add(row)
    session.commit()
    return row


def review(api: TestClient, brief_id: int, **body):
    return api.post(f"/api/sn/brief/{brief_id}/review", json=body)


# ---------- 首屏四个区 ----------

def test_home_page_shows_all_four_zones(loaded, monkeypatch):
    """首屏从上到下就是早上的动作顺序：待办 → 简报 → 昨夜变化 → 数据墙（折叠）。"""
    make_brief(loaded)
    page = client(loaded, monkeypatch).get("/")

    assert page.status_code == 200
    html = page.text
    for zone in ("待办", "今日简报", "昨夜到今早什么变了", "全景数据墙"):
        assert zone in html, f"首屏缺了「{zone}」区"
    assert 'id="brief-card"' in html
    # 数据墙折起来放最后：首屏是「今天我该干什么」，不是数据墙。<details> 不带 open = 默认收起
    wall = html[html.index('<details id="home-wall"'):]
    assert wall[:wall.index(">")].split("open")[0] == wall[:wall.index(">")], "数据墙默认必须是收起的"
    assert "核心市场" not in html.split('id="home-wall"')[0], "数据墙不能跑到简报前面去"


def test_home_stays_up_when_there_is_no_brief_today(loaded, monkeypatch):
    """LLM 没接通 / 组装失败是常态。简报缺席只该让简报区缺席，不该让整页崩。"""
    page = client(loaded, monkeypatch).get("/")

    assert page.status_code == 200
    html = page.text
    assert 'id="brief-missing"' in html and "今天还没有简报" in html
    assert 'id="brief-card"' not in html
    # 待办与异动照常出——这才是「今天我该干什么」的主体
    assert "昨夜到今早什么变了" in html and "待办" in html
    assert "主力合约结算价" in html, "行情照常，简报缺席不影响事实层"


def test_home_survives_an_empty_database(session, monkeypatch):
    """全新库里一个交易日都没有。/sn 该 503，首屏不该——那等于把人关在门外。"""
    page = client(session, monkeypatch).get("/")

    assert page.status_code == 200
    assert "库里还没有任何交易日数据" in page.text


def test_todos_cover_falsifier_expiry_and_data_outage(loaded, monkeypatch):
    """三类待办同时出现：证伪触发（review_tasks）、判断到期（review_tasks）、指标断供（signals）。"""
    activate(loaded)
    enter(loaded, "CUSTOMS.2609.import.MM", 12300, week(1))  # 证伪条件成立
    # today 推到复盘期之后，判断到期那条待办才会建出来
    evaluate_signals(loaded, "SN", D, today=date(2027, 1, 1))

    html = client(loaded, monkeypatch).get("/").text

    assert "证伪触发" in html and "判断到期" in html
    assert "指标断供" in html, "引用的指标断供必须摆上台面——阈值和证伪条件因此白写"
    assert "MACRO.VIX" in html, "断供待办要说清是哪条序列没来"
    assert "去补录" in html or "看指标登记" in html


def test_overnight_changes_carry_their_own_provenance(loaded, monkeypatch):
    """异动区的每个数字也要带 series_id 与取数时点，否则「变了」没法追。"""
    html = client(loaded, monkeypatch).get("/").text

    assert "SHFE.SN.warrant" in html or "SHFE.SN.main.settle" in html
    assert "核心数字有变化" in html


# ---------- 证据与质量警报 ----------

def test_every_number_in_the_brief_can_be_traced(loaded, monkeypatch):
    """§6.1：简报里 100% 的数字可点开看到 series_id、取数时间、来源。"""
    make_brief(loaded)
    html = client(loaded, monkeypatch).get("/").text

    assert "SHFE.SN.main.settle" in html
    assert "as_of 2026-09-18T15:00:00+08:00" in html
    assert "来源 SHFE" in html, "来源要能看见，不能只有数字"
    assert "togglePopover('ev-0-0')" in html, "每个证据都要能点开"


def test_unverified_claims_are_greyed_out_and_labelled(loaded, monkeypatch):
    """降级标灰的论断是质量警报，不能藏，更不能长得像正经论断。"""
    make_brief(loaded, unverified=UNVERIFIED)
    html = client(loaded, monkeypatch).get("/").text

    assert "未通过证据校验" in html
    assert "data-unverified" in html and 'class="unverified' in html
    assert UNVERIFIED[0]["reasons"][0] in html, "为什么没过要写出来，不然研究员无从判断"

    from tin.web import app as web
    css = (web.HERE / "static" / "app.css").read_text(encoding="utf-8")
    assert ".unverified" in css and "grayscale" in css, "标灰要真的标灰，不能只靠一行小字"


def test_draft_banner_disappears_once_the_researcher_has_acted(loaded, monkeypatch):
    """「AI 草稿，未经研究员确认」是信任的前提（§7），但摘不掉的标注等于没标注。"""
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)
    assert "AI 草稿，未经研究员确认" in api.get("/").text

    assert review(api, brief.id, action="采纳", elapsed_seconds=42).status_code == 200
    html = api.get("/").text
    assert "AI 草稿，未经研究员确认" not in html
    assert "已采纳" in html


# ---------- 采纳 / 改 / 否 ----------

def test_adopt_marks_the_brief_and_logs_a_revision(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    res = review(api, brief.id, action="采纳", elapsed_seconds=120)

    assert res.status_code == 200 and res.json()["status"] == "已采纳"
    rev = loaded.scalars(select(BriefRevision).where(BriefRevision.brief_id == brief.id)).one()
    assert (rev.action, rev.field) == ("采纳", None)
    assert loaded.get(DailyBrief, brief.id).status == "已采纳"


def test_edit_writes_one_revision_per_field_and_applies_the_change(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    res = review(api, brief.id, action="改", reason="社库口径变了，基调要收敛",
                 changes={"tone": "观望", "stance": "降敞口"}, elapsed_seconds=200)

    assert res.status_code == 200 and res.json()["status"] == "已修改"
    revs = loaded.scalars(select(BriefRevision).where(BriefRevision.brief_id == brief.id)).all()
    # 一个字段一条记录：合成一条大 diff 就看不出「这个人反复在改哪一项」
    assert {r.field for r in revs} == {"tone", "stance"}
    assert all(r.reason == "社库口径变了，基调要收敛" for r in revs)
    row = loaded.get(DailyBrief, brief.id)
    assert (row.tone, row.stance, row.status) == ("观望", "降敞口", "已修改")


def test_reject_needs_a_reason_and_settles_the_brief(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    res = review(api, brief.id, action="否", reason="今天的核心矛盾根本不是库存", elapsed_seconds=61)

    assert res.status_code == 200 and res.json()["status"] == "已否决"
    rev = loaded.scalars(select(BriefRevision).where(BriefRevision.brief_id == brief.id)).one()
    assert rev.action == "否" and rev.reason == "今天的核心矛盾根本不是库存"


def test_edit_without_a_reason_is_rejected_with_the_rule_text(loaded, monkeypatch):
    """「改 / 否」必须写原因——蒸馏（F6）的原料就是这个字段。

    前端不得放宽这条，后端的原文案也要原样透出：包成「操作失败」就把规则说明丢了。
    """
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    res = review(api, brief.id, action="改", changes={"tone": "观望"}, elapsed_seconds=3)

    assert res.status_code == 400, "不能返回 200，更不能是裸 500"
    assert "必须写明原因" in res.json()["detail"]
    assert "agent 学习的唯一养料" in res.json()["detail"]
    assert loaded.get(DailyBrief, brief.id).status == "草稿", "被挡下就不该落任何改动"
    assert loaded.scalars(select(BriefRevision)).all() == []


def test_edit_cannot_rewrite_system_provenance_fields(loaded, monkeypatch):
    """unverified / playbook_item_ids / signal_ids / model 是系统留痕，不是观点。

    让人改等于让人篡改审计记录——而那正是「为什么今天这么说」唯一答得上来的依据。
    """
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded, unverified=UNVERIFIED)

    res = review(api, brief.id, action="改", reason="这条其实说得对",
                 changes={"unverified": [], "model": "手动改的"}, elapsed_seconds=9)

    assert res.status_code == 400
    assert "不可修改的字段" in res.json()["detail"]
    assert "系统留痕不接受人工改写" in res.json()["detail"]
    assert loaded.get(DailyBrief, brief.id).unverified == UNVERIFIED


def test_empty_edit_is_rejected_instead_of_silently_passing(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    res = review(api, brief.id, action="改", reason="想了想没什么要改的", changes={},
                 elapsed_seconds=30)

    assert res.status_code == 400 and "至少要改动一个字段" in res.json()["detail"]


def test_unknown_action_is_rejected(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)
    assert review(api, brief.id, action="点赞").status_code == 400


def test_review_on_a_missing_brief_is_400_not_500(loaded, monkeypatch):
    res = review(client(loaded, monkeypatch), 9999, action="采纳", elapsed_seconds=5)
    assert res.status_code == 400 and "不存在" in res.json()["detail"]


# ---------- elapsed_seconds ----------

def test_elapsed_seconds_reaches_the_revision_log(loaded, monkeypatch):
    """§6.3 的「秒级采纳 = 没看」全靠这个数。断了就没法识别那个失败模式。"""
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)

    assert review(api, brief.id, action="采纳", elapsed_seconds=4).json()["elapsed_seconds"] == 4

    rev = loaded.scalars(select(BriefRevision).where(BriefRevision.brief_id == brief.id)).one()
    assert rev.elapsed_seconds == 4, "页面上报的阅读秒数必须原样落到纠错日志"


def test_elapsed_seconds_must_be_a_number(loaded, monkeypatch):
    api = client(loaded, monkeypatch)
    brief = make_brief(loaded)
    res = review(api, brief.id, action="采纳", elapsed_seconds="一会儿")
    assert res.status_code == 400 and "秒数" in res.json()["detail"]


def test_elapsed_seconds_is_timed_in_the_browser_not_on_the_server():
    """服务端算不出来：简报可能 8 点生成、下午 2 点提交，但只被看了 3 秒。

    所以计时必须在页面上做，且要能处理「切走标签页」——挂着页面去开会再回来点采纳，
    不能被记成看了两小时。
    """
    from tin.web import app as web

    js = (web.HERE / "static" / "app.js").read_text(encoding="utf-8")
    html = (web.HERE / "templates" / "home.html").read_text(encoding="utf-8")

    timer = js[js.index("const briefTimer"):js.index("let briefAction")]
    assert "visibilitychange" in timer, "切走标签页必须停表"
    assert "IntersectionObserver" in timer, "简报区没进视口不算在阅读时间里"
    assert "briefElapsed()" in js and "elapsed_seconds: briefElapsed()" in js
    assert 'id="brief-timer"' in html, "把秒数显示出来，研究员才知道系统在量什么"


def test_review_buttons_report_errors_inline_not_through_a_toast():
    """规则挡下时要在按钮旁边就地说清楚。浮层在右下角，正好压住这排按钮。"""
    from tin.web import app as web

    html = (web.HERE / "templates" / "home.html").read_text(encoding="utf-8")
    js = (web.HERE / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="brief-err"' in html
    anchor = html[html.index('id="brief-err"'):]
    assert "min-h-[16px]" in anchor[:anchor.index(">")], "常驻高度，报错不把按钮顶走"
    post = js[js.index("async function briefPost"):js.index("function briefReasonText")]
    assert "briefError(err.message)" in post, "后端的原文案要原样显示，不能换成通用提示"
