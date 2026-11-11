"""One-shot scheduler facade for bounded connector execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from .time_utils import utc_now_iso
import uuid
from typing import Callable

from .connector_runs import ConnectorRunStore


@dataclass(frozen=True)
class ScheduledConnectorTask:
    task_id: str
    connector_name: str
    target: str
    interval_seconds: int
    enabled: bool


class ConnectorBusyError(RuntimeError):
    pass


class ConnectorScheduler:
    """Stage 8 scheduler primitive; external cron can invoke run_once safely."""

    def __init__(self, run_store: ConnectorRunStore):
        self.run_store = run_store

    @staticmethod
    def _notify(callback: Callable[[str, str | None, str], None] | None, status: str, last_run: str | None, result: str) -> None:
        if callback:
            try:
                callback(status, last_run, result)
            except Exception:
                pass

    def run_once(self, task: ScheduledConnectorTask, runner: Callable[[], tuple[int, int]], status_callback: Callable[[str, str | None, str], None] | None = None) -> dict:
        run_id = f"RUN-{uuid.uuid4().hex[:16].upper()}"
        if not task.enabled:
            self.run_store.start(run_id, task.connector_name, task.target, "dry_run", task.task_id)
            self.run_store.finish(run_id, "dry_run", 0, 0)
            return {"status": "dry_run", "candidates": 0, "inserted": 0}
        if not self.run_store.acquire(task.task_id, run_id):
            raise ConnectorBusyError("connector_task_busy")
        lock_acquired = True
        try:
            self.run_store.start(run_id, task.connector_name, task.target, "running", task.task_id)
            self._notify(status_callback, "运行中", utc_now_iso(), "running")
            candidates, inserted = runner()
        except Exception:
            try:
                self.run_store.finish(run_id, "failed", 0, 0, "connector_failed")
                self._notify(status_callback, "采集失败", utc_now_iso(), "failed")
            finally:
                if lock_acquired:
                    self.run_store.release(task.task_id, run_id)
            raise
        try:
            self.run_store.finish(run_id, "succeeded", candidates, inserted)
            self._notify(status_callback, "运行中", utc_now_iso(), "succeeded")
        finally:
            if lock_acquired:
                self.run_store.release(task.task_id, run_id)
        return {"status": "succeeded", "candidates": candidates, "inserted": inserted}
