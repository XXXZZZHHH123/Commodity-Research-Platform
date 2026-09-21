"""命令行入口：python -m tin.jobs <命令>。cron 每个交易日 16:30 与次日 08:00 各跑一次 daily。"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from tin.compute.engine import compute_day, latest_trade_date
from tin.config import ROOT, SHANGHAI, settings
from tin.db import SessionLocal
from tin.export.board import snapshot
from tin.ingest.record import record
from tin.ingest.runner import fetch_all
from tin.jobs.seed import seed_indicators, seed_judgment
from tin.models import Indicator
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn


def _date(s: str | None) -> date:
    return date.fromisoformat(s) if s else datetime.now(SHANGHAI).date()


def cmd_init(_a):
    (ROOT / "data").mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)
    with SessionLocal() as s:
        print(f"登记指标 {seed_indicators(s)} 个")
        j = seed_judgment(s)
        print(f"导入判断 v{j.version}（{j.status}）" if j else "判断已存在，跳过导入")


def cmd_fetch(a):
    with SessionLocal() as s:
        for r in fetch_all(s, _date(a.date), set(a.only) if a.only else None):
            print(f"{r.fetcher:18s} {r.status:4s} 写入 {r.written:3d} 条  {r.error or ''}")


def cmd_compute(a):
    with SessionLocal() as s:
        d = date.fromisoformat(a.date) if a.date else latest_trade_date(s)
        if d is None:
            sys.exit("交易日历为空：请先 fetch 行情")
        for r in compute_day(s, d):
            shown = f"{r.value:,.2f}" if r.value is not None else "—"
            print(f"{d} {r.formula_id:12s} {r.status:28s} {shown:>12s}  {r.note or ''}")


def cmd_snapshot(a):
    with SessionLocal() as s:
        d = date.fromisoformat(a.date) if a.date else latest_trade_date(s)
        out = settings.exports_dir / settings.variety / f"{d.isoformat()}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot(s, settings.variety, d), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"快照已写入 {out.relative_to(ROOT)}")


def cmd_daily(a):
    cmd_fetch(a)
    a.date = None
    cmd_compute(a)
    cmd_snapshot(a)


def cmd_backfill(a):
    end = _date(a.date)
    with SessionLocal() as s:
        for i in range(a.days, -1, -1):
            d = end - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            runs = fetch_all(s, d, {"shfe_quotes", "shfe_warrant", "shfe_weekly_stock", "cboe_vix"})
            print(d, " ".join(f"{r.fetcher}={r.status}" for r in runs))
            if any(r.fetcher == "shfe_quotes" and r.status == "ok" for r in runs):
                compute_day(s, d)


def _weekdays(end: date, days: int) -> list[date]:
    """目标日往前 N 个自然日里的工作日。周末各源都不发布，抓了只是白白 404。"""
    return [d for d in (end - timedelta(days=i) for i in range(days, -1, -1)) if d.weekday() < 5]


def cmd_snap_raw(a):
    """下载官方源原件并落盘留证。不连数据库，GitHub Actions 上跑的就是这一个命令。"""
    from tin.ingest.raw import FAILED, snapshot_day

    out = Path(a.out)
    failed = False
    for d in _weekdays(_date(a.date), a.days):
        manifest = snapshot_day(d, out)
        for name, e in manifest["sources"].items():
            n = len(e["files"])
            print(f"{d} {name:18s} {e['status']:4s} {n:2d} 件  {e.get('error') or ''}")
            failed |= e["status"] == FAILED
    # 「未发布」是节假日的正常结果，不算失败；只有真实故障才让 workflow 变红
    sys.exit(1 if failed else 0)


def cmd_import_raw(a):
    """回放留证原件入库。与当天在线跑 fetch 等价，且可重复执行。"""
    from tin.ingest.raw import replay_day

    raw = Path(a.raw)
    with SessionLocal() as s:
        for d in _weekdays(_date(a.date), a.days):
            if not (raw / "manifest" / f"{d.isoformat()}.json").exists():
                print(f"{d} 无留证，跳过")
                continue
            runs = replay_day(s, d, raw)
            print(f"{d} " + "  ".join(f"{r.fetcher}={r.status}({r.written})" for r in runs))
            if any(r.fetcher == "shfe_quotes" and r.status == "ok" for r in runs):
                compute_day(s, d)


def cmd_enter(a):
    with SessionLocal() as s:
        ind = s.get(Indicator, a.series_id)
        if ind is None:
            sys.exit(f"指标未登记：{a.series_id}")
        as_of = datetime.fromisoformat(a.as_of)
        as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=SHANGHAI)
        row = record(s, ObservationIn(series_id=a.series_id, value=a.value, as_of=as_of,
                                      caliber=Caliber.model_validate(ind.caliber), source="人工",
                                      entered_by=a.by, note=a.note))
        s.commit()
        print(f"已录入 {a.series_id} = {a.value}（{as_of.isoformat()}，修订 {row.revision}）" if row else "与已有值相同，未重复写入")


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m tin.jobs")
    sub = p.add_subparsers(required=True)
    sub.add_parser("init", help="建库并导入种子").set_defaults(fn=cmd_init)
    for name, fn, h in (("fetch", cmd_fetch, "取数"), ("compute", cmd_compute, "计算派生值"),
                        ("snapshot", cmd_snapshot, "导出 JSON 快照"), ("daily", cmd_daily, "取数+计算+快照")):
        sp = sub.add_parser(name, help=h)
        sp.add_argument("--date", help="YYYY-MM-DD，默认今天/最近交易日")
        if name in ("fetch", "daily"):
            sp.add_argument("--only", nargs="*", help="只跑指定数据源")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("backfill", help="回补最近 N 天的上期所数据与 VIX")
    sp.add_argument("--days", type=int, default=10)
    sp.add_argument("--date")
    sp.set_defaults(fn=cmd_backfill)
    sp = sub.add_parser("snap-raw", help="下载官方源原件留证（不入库）")
    sp.add_argument("--out", default=str(ROOT / "raw"), help="留证根目录")
    sp.add_argument("--days", type=int, default=0, help="连同前 N 个自然日一起抓，用于补漏")
    sp.add_argument("--date")
    sp.set_defaults(fn=cmd_snap_raw)
    sp = sub.add_parser("import-raw", help="回放留证原件入库并计算派生值")
    sp.add_argument("--raw", default=str(ROOT / "raw"), help="留证根目录")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--date")
    sp.set_defaults(fn=cmd_import_raw)
    sp = sub.add_parser("enter", help="人工录入一条观测")
    sp.add_argument("series_id")
    sp.add_argument("value", type=float)
    sp.add_argument("--as-of", required=True, help="数据时点，如 '2026-09-18 11:30'（默认北京时间）")
    sp.add_argument("--by", required=True, help="录入人")
    sp.add_argument("--note", required=True, help="来源说明")
    sp.set_defaults(fn=cmd_enter)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
