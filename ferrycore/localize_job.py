"""汉化任务断点：进度落盘，取消/崩溃后可续跑。

译文本身已经在 ferry_cache 里按 hash 缓存；这里额外记「这份游戏翻到哪了」，
方便 UI 显示「继续汉化」和跳过已完成的 apply 提示。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .base import app_dir

JOB_DIR_NAME = "localize_jobs"


@dataclass
class LocalizeJob:
    root: str
    adapter: str = ""
    status: str = "idle"  # idle | running | cancelled | done | failed
    entries: int = 0
    translated: int = 0
    applied: int = 0
    missed: int = 0
    out_path: str = ""
    in_place: bool = False
    updated: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "LocalizeJob":
        return cls(
            root=str(data.get("root") or ""),
            adapter=str(data.get("adapter") or ""),
            status=str(data.get("status") or "idle"),
            entries=int(data.get("entries") or 0),
            translated=int(data.get("translated") or 0),
            applied=int(data.get("applied") or 0),
            missed=int(data.get("missed") or 0),
            out_path=str(data.get("out_path") or ""),
            in_place=bool(data.get("in_place")),
            updated=str(data.get("updated") or ""),
            notes=list(data.get("notes") or []),
        )

    @property
    def resumable(self) -> bool:
        return self.status in ("cancelled", "running", "failed") and self.translated < max(self.entries, 1)

    @property
    def done(self) -> bool:
        return self.status == "done" and self.applied > 0


def _job_path(root: Path) -> Path:
    key = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return app_dir() / "ferry_cache" / JOB_DIR_NAME / f"{key}.json"


def load_job(root: Path) -> LocalizeJob | None:
    path = _job_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return LocalizeJob.from_json(data)


def save_job(job: LocalizeJob) -> Path:
    path = _job_path(Path(job.root))
    path.parent.mkdir(parents=True, exist_ok=True)
    job.updated = datetime.now().isoformat(timespec="seconds")
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(job.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def clear_job(root: Path) -> None:
    path = _job_path(root)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def touch_progress(
    root: Path,
    *,
    adapter: str = "",
    status: str = "running",
    entries: int = 0,
    translated: int = 0,
    applied: int = 0,
    missed: int = 0,
    out_path: str = "",
    in_place: bool = False,
    notes: list[str] | None = None,
) -> LocalizeJob:
    job = load_job(root) or LocalizeJob(root=str(Path(root).resolve()))
    job.adapter = adapter or job.adapter
    job.status = status
    if entries:
        job.entries = entries
    if translated:
        job.translated = translated
    if applied:
        job.applied = applied
    job.missed = missed
    if out_path:
        job.out_path = out_path
    job.in_place = in_place or job.in_place
    if notes is not None:
        job.notes = notes
    save_job(job)
    return job
