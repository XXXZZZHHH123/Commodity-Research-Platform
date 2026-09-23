"""复盘待办的关闭路径（compute/signals.resolve_review_task）。

为什么单独守这个：待办原本只生不死——`create_review_tasks` 只建不关，判断页换版本
也不关。几天之内首屏那块「少而紧急」就变成第二面数据墙，而它短恰恰是全部价值所在。
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from tin.compute.signals import resolve_review_task
from tin.models import Judgment, ReviewTask


@pytest.fixture
def task(session):
    j = session.scalars(select(Judgment)).first()
    if j is None:
        j = Judgment(variety="SN", version=1, author="研究员",
                     written_at=datetime.now(timezone.utc).date(), review_period_days=30,
                     review_due=datetime.now(timezone.utc).date(), contradiction={},
                     marginal_focus=[], pricing_power={}, whitelist=[], thresholds=[],
                     falsifiers=[], status="生效", unstructured=[], review_warnings=[],
                     created_at=datetime.now(timezone.utc))
        session.add(j)
        session.commit()
    t = ReviewTask(judgment_id=j.id, trigger_type="证伪触发", signal_id=None,
                   assignee="研究员", created_at=datetime.now(timezone.utc))
    session.add(t)
    session.commit()
    return t


def test_resolving_closes_the_task_and_keeps_the_disposition(session, task):
    done = resolve_review_task(session, task.id, actor="张三", note="已改判断 v2，基调转空")
    assert done.state == "已处理"
    assert done.resolution_note == "已改判断 v2，基调转空"
    assert done.resolved_at is not None


def test_resolving_requires_a_disposition_note(session, task):
    """处置理由本身就是复盘记录；空着等于把一件事悄悄划掉。"""
    with pytest.raises(ValueError, match="处置说明"):
        resolve_review_task(session, task.id, actor="张三", note="  ")
    session.refresh(task)
    assert task.state == "待处理", "校验失败不得留下半截状态"


def test_a_task_is_not_resolved_twice(session, task):
    resolve_review_task(session, task.id, actor="张三", note="处理完了")
    with pytest.raises(ValueError, match="已经是"):
        resolve_review_task(session, task.id, actor="李四", note="再点一次")


def test_missing_task_is_refused_cleanly(session):
    with pytest.raises(ValueError, match="不存在"):
        resolve_review_task(session, 99999, actor="张三", note="x")
