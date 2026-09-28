"""命令行入口：python -m tin.jobs <命令>。cron 每个交易日 16:30 与次日 08:00 各跑一次 daily。"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from tin.compute.engine import compute_day, latest_trade_date
from tin.config import ROOT, SHANGHAI, settings
from tin.db import SessionLocal
from tin.export.board import snapshot
from tin.ingest.record import record
from tin.ingest.runner import fetch_all
from tin.jobs.seed import seed_indicators, seed_judgment, seed_researcher
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
        r = seed_researcher(s)
        print(f"研究员 {r.display_name}（{r.username}，id={r.id}）")
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


def cmd_signals(a):
    """把当日事实与生效判断逐条对上。纯确定性，不经过 LLM。"""
    from tin.compute.signals import evaluate_signals

    with SessionLocal() as s:
        d = date.fromisoformat(a.date) if a.date else latest_trade_date(s)
        if d is None:
            sys.exit("交易日历为空：请先 fetch 行情")
        signals, tasks = evaluate_signals(s, settings.variety, d)
        for x in signals:
            print(f"{d} {x.rule_type:10s} {x.rule_id:22s} {x.state:4s}  {x.gap_note or ''}")
        hit = sum(1 for x in signals if x.state == "触发")
        gap = sum(1 for x in signals if x.state == "数据缺失")
        print(f"共 {len(signals)} 条：触发 {hit}，数据缺失 {gap}；新建复盘待办 {len(tasks)} 条")


def cmd_brief(a):
    """出当日简报草稿。LLM 只在这一步出现，且它看到的信号已经是算好的事实。"""
    from tin.brief.compose import ComposeError, compose_daily
    from tin.llm.base import LlmError

    with SessionLocal() as s:
        d = date.fromisoformat(a.date) if a.date else latest_trade_date(s)
        if d is None:
            sys.exit("交易日历为空：请先 fetch 行情")
        me = seed_researcher(s)
        try:
            b = compose_daily(s, settings.variety, d, researcher_id=me.id)
        except (ComposeError, LlmError) as exc:
            # SPEC §7：失败时降级为「只做确定性信号，不出简报」，而不是出一份差的。
            # daily 不因此中断——信号和快照照常完成。
            print(f"{d} 未生成简报：{exc}")
            return
        print(f"{d} 简报 #{b.id}  {b.tone} / {b.stance or '—'} / "
              f"区间 {b.range_low}–{b.range_high}")
        print(f"  引用思路 {b.playbook_item_ids or '无（冷启动）'}；"
              f"标灰未通过校验 {len(b.unverified)} 条")


def cmd_snapshot(a):
    with SessionLocal() as s:
        d = date.fromisoformat(a.date) if a.date else latest_trade_date(s)
        out = settings.exports_dir / settings.variety / f"{d.isoformat()}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot(s, settings.variety, d), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"快照已写入 {out}")


def cmd_daily(a):
    cmd_fetch(a)
    a.date = None
    cmd_compute(a)
    # 信号必须在快照之前：快照只读已落库的信号，不自己重算（两处各算一遍必然对不上）
    cmd_signals(a)
    cmd_brief(a)
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
    """下载官方源原件并落盘留证。不连数据库，可跑在任何能上网的机器上。"""
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
    """回放留证原件入库。与当天在线跑 fetch 等价，且可重复执行。

    用于服务器直连不到的数据源：在能上网的机器上 snap-raw，把 raw/ 拷过来再 import-raw。
    """
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


def cmd_import_terminal(a):
    """把数据商终端导出的整本工作簿入库（SMM / 钢联，几十万条，网页导入装不下）。"""
    from tin.ingest.vendor_terminal import load

    def show(done, total, rep):
        print(f"  {done}/{total} 条序列…  写入 {rep.written:,}", flush=True)

    with SessionLocal() as s:
        rep = load(s, a.file, entered_by=a.by, dry_run=a.dry_run,
                   register_new=not a.no_register, catalog_only=a.catalog_only,
                   progress=None if a.dry_run else show)
    print(("[试算] " if a.dry_run else "") + rep.line())
    for r in rep.rejected[:10]:
        print("  拒绝：", r)


def cmd_map_columns(a):
    """列出终端导出里还没对上指标的列，并给出候选。

    这是「每次导出都要拿给 AI 认列」→「只有新格式才要人看」的那一步：
    已确认过的映射按表头指纹自动命中，这里只剩真正需要人拍板的。
    """
    from tin.ingest import vendor_terminal
    from tin.ingest.column_map import suggest

    with SessionLocal() as s:
        rep = vendor_terminal.summarize(s, a.file, settings.variety)
        unmapped = rep.get("unmapped_columns") or []
        print(f"缓存命中 {rep.get('column_cache_hits', 0)} 列；待确认 {len(unmapped)} 列")
        for sheet, fp in (rep.get("column_fingerprints") or {}).items():
            print(f"  工作表「{sheet}」指纹 {fp}")
        if not unmapped:
            return
        by_sheet: dict[str, list[dict]] = {}
        for u in unmapped:
            by_sheet.setdefault(u.get("sheet") or "", []).append(u)
        for sheet, items in by_sheet.items():
            names = [u["column_name"] for u in items]
            for sug in suggest(s, a.vendor, names, names):
                print(f"\n  [{sheet}] {sug['column_name']}（{sug['source']}）")
                for c in sug["candidates"][:3]:
                    print(f"      {c['confidence']:3d}%  {c['series_id']:32s} {c['name']}  ← {c['reason']}")
                if not sug["candidates"]:
                    print("      无候选：该列可能是新指标，需先在指标页登记")


def cmd_confirm_columns(a):
    """把人拍板的列映射写进缓存。此后同指纹的导出自动命中，不再问人。"""
    from tin.ingest.column_map import confirm

    mapping = {}
    for pair in a.col:
        if "=" not in pair:
            sys.exit(f"--col 需要写成 「列名=series_id」，收到：{pair}")
        name, sid = pair.split("=", 1)
        mapping[name.strip()] = sid.strip()
    with SessionLocal() as s:
        try:
            n = confirm(s, a.vendor, a.fingerprint, mapping, actor=a.by)
            s.commit()
        except IntegrityError as e:
            s.rollback()
            sys.exit(f"映射写入失败——series_id 必须是已登记指标：{e.orig}")
    print(f"已确认 {n} 列映射（指纹 {a.fingerprint}）")


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
                        ("signals", cmd_signals, "比对事实与判断，产出信号与复盘待办"),
                        ("brief", cmd_brief, "出当日简报草稿（唯一用到 LLM 的一步）"),
                        ("snapshot", cmd_snapshot, "导出 JSON 快照"),
                        ("daily", cmd_daily, "取数+计算+信号+简报+快照")):
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
    sp = sub.add_parser("import-terminal", help="导入数据商终端导出的整本工作簿（SMM / 钢联）")
    sp.add_argument("file", help="终端导出的 .xlsx")
    sp.add_argument("--by", required=True, help="导入操作人，写入每条观测的录入人")
    sp.add_argument("--dry-run", action="store_true", help="只统计不入库")
    sp.add_argument("--catalog-only", action="store_true",
                    help="只登记指标不写观测：冷启动建目录用，终端按目录整批只导一天即可")
    sp.add_argument("--no-register", action="store_true",
                    help="拒绝库里没见过的编码，只更新已登记序列；无人值守的定时导入必须加这个")
    sp.set_defaults(fn=cmd_import_terminal)
    sp = sub.add_parser("map-columns", help="列出终端导出里待确认的列映射与候选")
    sp.add_argument("--file", required=True, help="终端导出的 .xlsx")
    sp.add_argument("--vendor", default="Mysteel", help="数据商：SMM / Mysteel")
    sp.set_defaults(fn=cmd_map_columns)
    sp = sub.add_parser("confirm-columns", help="确认列映射，此后同指纹导出自动命中")
    sp.add_argument("--fingerprint", required=True, help="map-columns 打印的表头指纹")
    sp.add_argument("--vendor", required=True, help="数据商：SMM / Mysteel")
    sp.add_argument("--by", required=True, help="确认人")
    sp.add_argument("--col", action="append", required=True, metavar="列名=series_id",
                    help="可重复：--col '锡锭社会库存=MYSTEEL.SN.stock.social'")
    sp.set_defaults(fn=cmd_confirm_columns)
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
