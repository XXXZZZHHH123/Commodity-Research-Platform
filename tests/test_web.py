from datetime import date

import pytest
from fastapi import HTTPException

from tin.web.app import _board_date, _shell


def test_calendar_date_uses_nearest_previous_trading_day(loaded):
    assert _board_date(loaded, "2026-09-18") == date(2026, 9, 18)
    assert _board_date(loaded, "2026-09-19") == date(2026, 9, 18)


def test_calendar_rejects_date_before_available_history(loaded):
    with pytest.raises(HTTPException) as exc:
        _board_date(loaded, "2026-01-01")
    assert exc.value.status_code == 404


def test_shell_exposes_calendar_bounds(loaded):
    ctx = _shell(loaded, "variety", date(2026, 9, 18))
    assert ctx["min_date"] == "2026-09-17"
    assert ctx["max_date"] == "2026-09-18"


def test_static_assets_carry_a_version_fingerprint(tmp_path, monkeypatch):
    """页面每次渲染都带静态资源指纹：否则浏览器会拿旧 app.js 配新 HTML，
    表现为按钮在、点了没反应，且没有任何报错提示。"""
    from tin.web import app as web

    first = web.static_version()
    assert first.isdigit()

    js = web.HERE / "static" / "app.js"
    original = js.stat().st_mtime
    try:
        import os
        os.utime(js, (original + 10, original + 10))
        assert web.static_version() != first, "静态文件变化后指纹必须随之变化"
    finally:
        os.utime(js, (original, original))
