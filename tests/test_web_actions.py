"""两条写路径的 HTTP 层：简报撤回、复盘待办关闭。

它们共同守一件事——**首屏上的东西必须能被处理掉**。简报误点「否」要能退回重出，
待办处理完要能关掉。少任何一条，首屏几天内就从「今天我该干什么」退化成只读的墙。
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tin.jobs.seed import seed_researcher
from tin.models import Judgment, ReviewTask
from tin.web.app import app

from tests.test_web_home import D, client, make_brief


@pytest.fixture
def api(loaded, monkeypatch) -> TestClient:
    return client(loaded, monkeypatch)


@pytest.fixture
def task(loaded) -> ReviewTask:
    j = loaded.scalars(select(Judgment)).first()
    if j is None:
        j = Judgment(variety="SN", version=1, author="研究员", written_at=D,
                     review_period_days=30, review_due=D, contradiction={}, marginal_focus=[],
                     pricing_power={}, whitelist=[], thresholds=[], falsifiers=[],
                     status="生效", unstructured=[], review_warnings=[],
                     created_at=datetime.now(timezone.utc))
        loaded.add(j)
        loaded.commit()
    t = ReviewTask(judgment_id=j.id, trigger_type="证伪触发", signal_id=None,
                   assignee="研究员", created_at=datetime.now(timezone.utc))
    loaded.add(t)
    loaded.commit()
    return t


# ---------- 简报撤回 ----------

def test_reopen_unblocks_a_brief_the_researcher_rejected_by_mistake(api, loaded):
    b = make_brief(loaded)
    assert api.post(f"/api/sn/brief/{b.id}/review",
                    json={"action": "否", "reason": "数据不全"}).status_code == 200
    r = api.post(f"/api/sn/brief/{b.id}/reopen", json={"reason": "数据补齐了"})
    assert r.status_code == 200 and r.json()["status"] == "草稿"


def test_reopen_without_a_reason_is_400_with_the_real_message(api, loaded):
    b = make_brief(loaded)
    api.post(f"/api/sn/brief/{b.id}/review", json={"action": "采纳"})
    r = api.post(f"/api/sn/brief/{b.id}/reopen", json={})
    assert r.status_code == 400 and "原因" in r.json()["detail"]


def test_reopening_a_draft_is_400_not_500(api, loaded):
    b = make_brief(loaded)
    r = api.post(f"/api/sn/brief/{b.id}/reopen", json={"reason": "随便"})
    assert r.status_code == 400 and "不需要撤回" in r.json()["detail"]


# ---------- 复盘待办关闭 ----------

def test_resolving_a_task_closes_it(api, loaded, task):
    r = api.post(f"/api/sn/review-task/{task.id}/resolve",
                 json={"note": "已改判断 v2"})
    assert r.status_code == 200 and r.json()["state"] == "已处理"
    stored = loaded.get(ReviewTask, task.id)
    assert stored.resolution_note == "已改判断 v2" and stored.resolved_at is not None


def test_resolving_without_a_note_is_400(api, task):
    r = api.post(f"/api/sn/review-task/{task.id}/resolve", json={"note": "  "})
    assert r.status_code == 400 and "处置说明" in r.json()["detail"]


def test_resolving_twice_is_400_not_500(api, task):
    api.post(f"/api/sn/review-task/{task.id}/resolve", json={"note": "处理完了"})
    r = api.post(f"/api/sn/review-task/{task.id}/resolve", json={"note": "再来一次"})
    assert r.status_code == 400 and "已经是" in r.json()["detail"]


def test_resolved_tasks_leave_the_home_todo_list(api, loaded, task):
    """关掉的待办必须从首屏消失——只增不减的话这一区就没有意义了。"""
    before = api.get("/").text
    api.post(f"/api/sn/review-task/{task.id}/resolve", json={"note": "处理完了"})
    after = api.get("/").text
    assert before.count("证伪触发") > after.count("证伪触发")


def test_missing_task_is_400_not_500(api):
    assert api.post("/api/sn/review-task/99999/resolve",
                    json={"note": "x"}).status_code == 400


def test_default_actor_falls_back_to_the_seeded_researcher(api, loaded, task):
    r = api.post(f"/api/sn/review-task/{task.id}/resolve", json={"note": "处理完了"})
    assert r.json()["actor"] == seed_researcher(loaded).display_name
