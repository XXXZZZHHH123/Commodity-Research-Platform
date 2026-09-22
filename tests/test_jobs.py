import json
from contextlib import nullcontext
from types import SimpleNamespace

from tests.conftest import D
from tin.jobs import __main__ as jobs


def test_snapshot_supports_exports_outside_project(tmp_path, monkeypatch, capsys):
    exports = tmp_path / "var" / "lib" / "commodity-research-platform" / "exports"
    monkeypatch.setattr(jobs.settings, "exports_dir", exports)
    monkeypatch.setattr(jobs, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(jobs, "latest_trade_date", lambda _session: D)
    monkeypatch.setattr(jobs, "snapshot", lambda _session, variety, day: {
        "variety": variety,
        "as_of": day.isoformat(),
    })

    jobs.cmd_snapshot(SimpleNamespace(date=None))

    output = exports / "SN" / f"{D.isoformat()}.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "variety": "SN",
        "as_of": D.isoformat(),
    }
    assert str(output) in capsys.readouterr().out
