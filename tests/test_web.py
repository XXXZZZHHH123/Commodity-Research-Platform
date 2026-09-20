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
