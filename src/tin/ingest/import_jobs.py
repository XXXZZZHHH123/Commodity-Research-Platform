"""大文件导入的后台任务（方案 05 §9 / 方案 07 §8）。

SMM 终端整本工作簿入库要跑几分钟，撑不住一个同步 HTTP 请求——nginx 会超时，
用户手滑刷新就得重来。所以确认后立刻返回任务号，页面轮询进度。

任务状态放进程内存：一期是单进程 uvicorn，够用；进程重启会丢失进行中的任务，
但因为 `record()` 幂等，重跑一次即可，不会写重。
"""

import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tin.config import settings

TOKEN = re.compile(r"^[0-9a-f]{32}$")  # 暂存文件名只许是 uuid4 的 hex，杜绝路径穿越
JOB_TTL = timedelta(hours=6)

_LOCK = threading.Lock()
_JOBS: dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    kind: str
    filename: str
    actor: str
    state: str = "running"        # running / done / failed
    started_at: str = ""
    finished_at: str | None = None
    done: int = 0                  # 已处理序列数
    total: int = 0
    registered: int = 0
    written: int = 0
    unchanged: int = 0
    rejected: list[str] = field(default_factory=list)
    error: str | None = None
    token: str = ""      # 暂存文件名，不随状态返回给前端
    variety: str = "SN"

    @property
    def percent(self) -> int:
        return int(self.done / self.total * 100) if self.total else 0


def staging_path(token: str) -> Path:
    if not TOKEN.match(token):
        raise ValueError("暂存令牌不合法")
    return Path(settings.staging_dir) / f"{token}.xlsx"


def stage(data: bytes) -> str:
    """把上传的文件落到暂存区，返回令牌。不入库——`import_previews` 明确不保留整表副本。"""
    token = uuid.uuid4().hex
    path = staging_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return token


def purge(now: datetime | None = None) -> int:
    """清掉过期任务与它们的暂存文件。"""
    now = now or datetime.now(timezone.utc)
    cutoff = now - JOB_TTL
    with _LOCK:
        stale = [j for j in _JOBS.values()
                 if j.finished_at and datetime.fromisoformat(j.finished_at) < cutoff]
        for job in stale:
            _JOBS.pop(job.id, None)
    folder = Path(settings.staging_dir)
    removed = 0
    if folder.exists():
        for f in folder.glob("*.xlsx"):
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    return removed


def snapshot(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        state = asdict(job)
        state.pop("token")  # 暂存令牌不外泄
        return {**state, "percent": job.percent}


def create_terminal_job(token: str, actor: str, filename: str, variety: str = "SN") -> str:
    """登记一个任务并返回任务号。真正的执行由调用方丢进后台。"""
    staging_path(token)  # 令牌不合法就在这里炸，别等到后台线程里
    job = Job(id=uuid.uuid4().hex, kind="vendor_terminal", filename=filename, actor=actor,
              started_at=datetime.now(timezone.utc).isoformat(), token=token, variety=variety)
    with _LOCK:
        _JOBS[job.id] = job
    return job.id


def execute(job_id: str) -> None:
    """后台线程里跑。任何异常都落到任务状态上，不吞掉。"""
    from tin.db import SessionLocal
    from tin.ingest.vendor_terminal import load
    from tin.models import AuditLog

    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return
    path = staging_path(job.token)

    def progress(done, total, report):
        with _LOCK:
            job.done, job.total = done, total
            job.written, job.unchanged = report.written, report.unchanged

    try:
        with SessionLocal() as session:
            report = load(session, path, entered_by=job.actor, variety=job.variety,
                          progress=progress)
            session.add(AuditLog(
                at=datetime.now(timezone.utc), actor=job.actor, action="导入",
                target_type="vendor_terminal", target_id=job.filename[:80],
                detail={"registered": report.registered, "written": report.written,
                        "unchanged": report.unchanged, "rejected": len(report.rejected)}))
            session.commit()
        with _LOCK:
            job.registered, job.written = report.registered, report.written
            job.unchanged, job.rejected = report.unchanged, report.rejected[:20]
            job.state = "done"
    except Exception as exc:  # noqa: BLE001 - 后台任务的异常没有别处可去，必须留在状态里
        with _LOCK:
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        path.unlink(missing_ok=True)
        with _LOCK:
            job.finished_at = datetime.now(timezone.utc).isoformat()
        purge()
