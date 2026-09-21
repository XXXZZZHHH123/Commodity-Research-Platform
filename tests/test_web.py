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


def test_every_inline_handler_resolves_to_a_real_function():
    """模板里 onclick/onchange 绑定的函数必须真的存在。

    否则页面渲染正常、按钮也在，点下去却毫无反应，控制台外没有任何提示——
    这个哑失败在自定义导出上已经发生过一次。
    """
    import re

    from tin.web import app as web

    sources = [(web.HERE / "static" / "app.js").read_text(encoding="utf-8")]
    attrs: list[tuple[str, str]] = []
    for tpl in sorted((web.HERE / "templates").glob("*.html")):
        text = tpl.read_text(encoding="utf-8")
        sources.extend(re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S))
        attrs += [(tpl.name, v) for v in re.findall(r'\son\w+="([^"]*)"', text)]
    # 模板里 `${...}` 内嵌的字符串同样会被浏览器当成处理器执行
    for src in list(sources):
        attrs += [("app.js", v) for v in re.findall(r'\son\w+="([^"]*)"', src)]

    defined = set()
    for src in sources:
        defined |= set(re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src))
        defined |= set(re.findall(r"(?:^|\n)\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", src))
    builtin = {"event", "window", "alert", "confirm", "this", "return", "if", "typeof"}

    missing = set()
    for where, value in attrs:
        for name in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", value):
            if name not in defined and name not in builtin:
                missing.add(f"{where}: {name}()")
    assert not missing, f"事件绑定指向不存在的函数：{sorted(missing)}"
