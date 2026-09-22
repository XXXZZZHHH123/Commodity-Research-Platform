"""原件留证与离线回放。

日常取数走 `tin.jobs daily`，由服务器上的 systemd timer 调度，直连各官方源——这条路不经过
这个模块。这里解决的是另一件事：**服务器出不了网的那些源怎么办**。

- `snapshot_day` 在能上网的机器上跑：下载官方源原件、落盘、写台账。全程不碰数据库，
  `tin.models` 只在 `replay_day` 里按需导入，所以它可以跑在任何装了依赖的机器上。
- `replay_day` 在服务器上跑：读同一批原件，喂给与在线采集完全相同的 `parse_*` 与
  `record()` 闸门。已验证两条路径产生逐行相同的观测。

把 `raw/` 目录拷过去即可，不需要两台机器互通。顺带产出一份原件级的证据链：每个数字都能
追到当天那份交易所原文件与它的 sha256。

落盘分两类：交易所当日件是不可变证据，按日期分文件；VIX 与 FRED 每次返回的是全量历史，
覆盖同一路径。
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from tin.config import settings
from tin.ingest import NotPublished, cboe, cfets, fred, shfe
from tin.ingest.shfe import Batch

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

OK, NOT_PUBLISHED, FAILED = "ok", "未发布", "失败"


@dataclass(frozen=True)
class Request:
    path: str          # 相对 raw 根目录的落盘路径
    url: str
    method: str = "GET"
    key: str = ""      # 同一源内区分多个文件（FRED 用 fred_id）
    browser_ua: bool = True
    dated: bool = True  # True=按日不可变；False=全量历史，覆盖同一路径


def plan_day(d: date) -> dict[str, tuple[Request, ...]]:
    """列出某日要抓的全部原件。源名与 `runner.FETCHERS` 一一对应，不新增任何数据源。"""
    ymd, iso = d.strftime("%Y%m%d"), d.isoformat()
    return {
        "shfe_quotes": (Request(f"shfe/quotes/{iso}.dat", shfe.QUOTE_URL.format(d=ymd)),),
        "shfe_warrant": (Request(f"shfe/warrant/{iso}.html", shfe.WARRANT_URL.format(d=ymd)),),
        "shfe_weekly_stock": (Request(f"shfe/weekly/{iso}.html", shfe.WEEKLY_URL.format(d=ymd)),),
        "cfets_fx": (Request(f"cfets/ccpr/{iso}.json", cfets.CCPR_URL, "POST"),),
        "cboe_vix": (Request("cboe/VIX_History.csv", cboe.VIX_URL, dated=False),),
        # FRED 不能带浏览器 UA：并发请求时对方会挂住不响应，直到读超时。
        # 现有 runner._fred 用的是不带 header 的 httpx.get，恰好绕开了这一点。
        "fred_macro": tuple(
            Request(f"fred/{spec.fred_id}.csv", fred.url_for(spec, d), key=spec.fred_id,
                    browser_ua=False, dated=False)
            for spec in fred.FETCHABLE_SERIES
        ),
    }


def _text(blob: bytes) -> str:
    return blob.decode("utf-8", errors="replace")


def parse_source(name: str, blobs: dict[str, bytes], d: date) -> Batch:
    """把原件解析成 Batch。与在线采集调用的是同一批纯函数。

    `blobs` 的键是 `Request.key`（FRED）或落盘路径（其余单文件源）。
    """
    one = next(iter(blobs.values())) if blobs else b""
    if name == "shfe_quotes":
        return shfe.parse_quotes(json.loads(_text(one)))
    if name == "shfe_warrant":
        return shfe.parse_daily_warrant(_text(one), d)
    if name == "shfe_weekly_stock":
        return shfe.parse_weekly_stock(_text(one), d)
    if name == "cfets_fx":
        batch = cfets.parse_ccpr(json.loads(_text(one)))
        got = batch.observations[0].as_of.date()
        if got != d:
            raise NotPublished(f"中间价原件为 {got} 的数据，与目标日 {d} 不符")
        return batch
    if name == "cboe_vix":
        batch = cboe.parse_vix(_text(one), since=d - timedelta(days=10))
        # 滚动文件里会有目标日之后的行；截断到目标日，回放才与「当天在线抓取」等价
        batch.observations[:] = [o for o in batch.observations if o.as_of.date() <= d]
        if not batch.observations:
            raise NotPublished(f"VIX 截至 {d} 前 10 日无数据")
        return batch
    if name == "fred_macro":
        merged = Batch()
        for spec in fred.FETCHABLE_SERIES:
            blob = blobs.get(spec.fred_id)
            if blob is None:
                continue
            try:
                sub = fred.parse_series(_text(blob), spec, through=d, source_url=fred.url_for(spec, d))
            except Exception as exc:  # noqa: BLE001 - 单序列解析失败不影响同批其他序列
                merged.errors.append(f"{spec.fred_id}: {type(exc).__name__}: {exc}")
                continue
            merged.indicators.extend(sub.indicators)
            merged.observations.extend(sub.observations)
        if not merged.observations:
            raise ValueError("FRED 全部序列解析失败：" + "；".join(merged.errors[:3]))
        return merged
    raise ValueError(f"未知数据源：{name}")


def _download(client: httpx.Client | None, req: Request) -> tuple[int, bytes]:
    """client 为 None 时用独立连接——FRED 那 21 个序列要并发取，
    共用连接池在代理环境下会让线程互相阻塞（runner._fred 有同款处置）。"""
    if client is None:
        r = httpx.request(req.method, req.url, headers=UA if req.browser_ua else None,
                          timeout=settings.http_timeout, follow_redirects=True)
    else:
        r = client.request(req.method, req.url,
                           headers=None if req.browser_ua else {"User-Agent": "python-httpx"})
    if r.status_code == 404:
        raise NotPublished(f"{req.url} 返回 404")
    r.raise_for_status()
    return r.status_code, r.content


def _download_many(requests: tuple[Request, ...]) -> dict[str, tuple[int, bytes] | Exception]:
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_download, None, req): req for req in requests}
        out: dict[str, tuple[int, bytes] | Exception] = {}
        for future, req in futures.items():
            try:
                out[req.path] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单序列失败不影响同批其他序列
                out[req.path] = exc
        return out


def _drop_dated_files(out_dir: Path, d: date, entry: dict) -> None:
    """删掉按日期命名却装着别的日期的原件。

    中间价接口只返回当日值，抓历史日期时拿到的是今天的数——把它留在 `2026-09-18.json`
    这个路径下，就是一份贴错标签的证据，比没有更糟。滚动文件（VIX / FRED）不受影响。
    """
    kept = []
    for f in entry["files"]:
        req = next((r for r in plan_day(d)[entry["_source"]] if r.path == f["path"]), None)
        if req is not None and req.dated:
            (Path(out_dir) / f["path"]).unlink(missing_ok=True)
        else:
            kept.append(f)
    entry["files"] = kept


def snapshot_day(d: date, out_dir: Path, client: httpx.Client | None = None) -> dict:
    """下载某日全部原件并落盘，返回台账。不写数据库。

    下载后立刻试解析一次：源站改版导致结构变化时，在留证阶段就暴露，而不是等到回放入库。
    """
    owns = client is None
    client = client or httpx.Client(headers=UA, timeout=settings.http_timeout, follow_redirects=True)
    out_dir = Path(out_dir)
    manifest = {"target_date": d.isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "sources": {}}
    try:
        for name, requests in plan_day(d).items():
            entry: dict = {"status": OK, "files": [], "error": None, "parsed": False, "_source": name}
            blobs: dict[str, bytes] = {}
            results = _download_many(requests) if len(requests) > 1 else {}
            errors: list[str] = []
            for req in requests:
                try:
                    got = results[req.path] if results else _download(client, req)
                    if isinstance(got, Exception):
                        raise got
                    status, content = got
                except NotPublished as e:
                    entry["status"], entry["error"] = NOT_PUBLISHED, str(e)
                    break
                except Exception as e:  # noqa: BLE001 - 任一源故障都不能中断其余留证
                    errors.append(f"{req.key or req.path}: {type(e).__name__}: {e}")
                    if len(requests) == 1:  # 单文件源：这个源就是失败了
                        entry["status"] = FAILED
                        break
                    continue
                path = out_dir / req.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                blobs[req.key or req.path] = content
                entry["files"].append({"path": req.path, "url": req.url, "http_status": status,
                                       "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            if errors:
                entry["error"] = "；".join(errors[:3])
                if not blobs and entry["status"] == OK:
                    entry["status"] = FAILED
            if entry["status"] == OK:
                try:
                    parse_source(name, blobs, d)
                    entry["parsed"] = True
                except NotPublished as e:
                    entry["status"], entry["error"] = NOT_PUBLISHED, str(e)
                    _drop_dated_files(out_dir, d, entry)
                except Exception as e:  # noqa: BLE001 - 结构变化要当故障报出来
                    entry["status"] = FAILED
                    entry["error"] = f"解析失败 {type(e).__name__}: {e}"
            entry.pop("_source")
            manifest["sources"][name] = entry
    finally:
        if owns:
            client.close()

    path = out_dir / "manifest" / f"{d.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_manifest(raw_dir: Path, d: date) -> dict | None:
    path = Path(raw_dir) / "manifest" / f"{d.isoformat()}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _verify(raw_dir: Path, file_entry: dict, dated: bool) -> bytes:
    """读原件；按日不可变的那些要与台账对得上。

    滚动文件（VIX、FRED 的全量历史）会被后续每一天的抓取覆盖，台账里的 sha256 只是
    「当时看到的样子」这一条线索，不能拿来做校验——否则回放旧日期必然失败。
    解析时按 `through=目标日` 截断，所以用新文件回放旧日期的结果仍然正确。
    """
    blob = (Path(raw_dir) / file_entry["path"]).read_bytes()
    if dated and hashlib.sha256(blob).hexdigest() != file_entry["sha256"]:
        raise ValueError(f"{file_entry['path']} 与台账不符：原件可能已被改动")
    return blob


def replay_day(session, d: date, raw_dir: Path) -> list:
    """把某日原件回放入库，走与在线采集相同的 `record()` 闸门。

    返回 `FetchRun` 列表，与 `runner.fetch_all` 的返回同形，页面上的采集台账看不出区别。
    """
    # 延迟导入：让 snapshot_day 那条路径（Actions 上运行）完全不牵连数据库模块
    from tin.ingest.runner import store
    from tin.models import FetchRun

    manifest = load_manifest(raw_dir, d)
    if manifest is None:
        raise FileNotFoundError(f"{d} 没有留证台账，无法回放")

    runs = []
    for name, entry in manifest["sources"].items():
        run = FetchRun(fetcher=name, target_date=d.isoformat(), started_at=datetime.now(timezone.utc),
                       status="running", attempts=1)
        if entry["status"] != OK:
            run.status, run.error = entry["status"], entry.get("error")
        else:
            try:
                blobs = {}
                for f in entry["files"]:
                    req = next((r for r in plan_day(d)[name] if r.path == f["path"]), None)
                    blobs[(req.key if req else "") or f["path"]] = _verify(
                        raw_dir, f, dated=req.dated if req else True)
                batch = parse_source(name, blobs, d)
                with session.begin_nested():
                    run.written = store(session, batch)
                run.status = "部分失败" if batch.errors else OK
                run.error = "；".join(batch.errors) if batch.errors else None
            except NotPublished as e:
                run.status, run.error = NOT_PUBLISHED, str(e)
            except Exception as e:  # noqa: BLE001 - 单源回放失败不中断其余
                run.status, run.error = FAILED, f"{type(e).__name__}: {e}"
        run.finished_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        runs.append(run)
    return runs


def latest_snapshot(raw_dir: Path) -> date | None:
    """最新一份台账的日期。用于在页面上暴露「定时任务是否还活着」。"""
    folder = Path(raw_dir) / "manifest"
    if not folder.exists():
        return None
    days = [date.fromisoformat(p.stem) for p in folder.glob("*.json")]
    return max(days) if days else None
