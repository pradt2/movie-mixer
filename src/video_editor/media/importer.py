"""Background import jobs: probe files off the UI thread, cancellable.

Pure threads (no Qt). The UI bridges callbacks to the main thread with Qt
queued signals, so results from multiple workers never race on shared state.
Media files are read-only; nothing is copied.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .assets import Asset
from .ffprobe import ProbeResult, probe_file


@dataclass
class ImportJob:
    id: str
    path: Path
    cancelled: bool = False
    process: subprocess.Popen | None = None
    finished: bool = False


class ImportController:
    """Runs one probe thread per file; supports per-job and global cancellation."""

    def __init__(
        self,
        on_started: Callable[[ImportJob], None],
        on_finished: Callable[[ImportJob, Asset | None, str | None], None],
    ) -> None:
        self._on_started = on_started
        self._on_finished = on_finished
        self._jobs: dict[str, ImportJob] = {}
        self._lock = threading.Lock()

    def import_paths(self, paths: list[Path]) -> list[ImportJob]:
        jobs = []
        for path in paths:
            job = ImportJob(id=uuid.uuid4().hex, path=path)
            with self._lock:
                self._jobs[job.id] = job
            thread = threading.Thread(
                target=self._run, args=(job,), name=f"probe-{path.name}", daemon=True
            )
            thread.start()
            jobs.append(job)
        return jobs

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.finished:
                return False
            job.cancelled = True
            proc = job.process
        if proc is not None:
            import contextlib

            with contextlib.suppress(OSError):  # race with process exit
                proc.kill()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            ids = [j.id for j in self._jobs.values() if not j.finished]
        for job_id in ids:
            self.cancel(job_id)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if not j.finished)

    def _run(self, job: ImportJob) -> None:
        self._on_started(job)
        if job.cancelled:
            self._finish(job, None, "cancelled")
            return
        if not job.path.is_file():
            self._finish(job, None, "File not found")
            return
        result: ProbeResult = probe_file(job.path)
        if job.cancelled:
            self._finish(job, None, "cancelled")
            return
        if result.metadata is None:
            self._finish(job, None, result.error or "Probe failed")
            return
        try:
            stat = job.path.stat()
        except OSError as exc:
            self._finish(job, None, f"File vanished during import: {exc}")
            return
        asset = Asset(
            id=Asset.new_id(),
            path=job.path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            metadata=result.metadata,
        )
        self._finish(job, asset, None)

    def _finish(self, job: ImportJob, asset: Asset | None, error: str | None) -> None:
        with self._lock:
            job.finished = True
        self._on_finished(job, asset, error)
