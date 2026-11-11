"""规范化后端的轻量持久化调度器。

默认不在 FastAPI 进程内自动启动，避免用户未配置授权范围时产生隐式采集。
可通过 ``python -m app.run_backend_scheduler`` 显式启动；任务本身仍受来源、
HMAC 密钥、GitHub 白名单和联网开关约束。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from .backend_pipeline import backend_store, run_task


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def recover_interrupted() -> int:
    store = backend_store()
    store.initialize()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def write(connection):
        runs = connection.execute(
            "SELECT id,task_id FROM scan_runs WHERE status='running'"
        ).fetchall()
        for run in runs:
            connection.execute(
                "UPDATE scan_runs SET status='interrupted',finished_at=?,"
                "stats_json=json_set(stats_json,'$.stop_reason','interrupted_by_restart') WHERE id=?",
                (now, run["id"]),
            )
            connection.execute(
                "UPDATE tasks SET status='pending',next_run_at=?,updated_at=? WHERE id=? AND status='running'",
                (now, now, run["task_id"]),
            )
        return len(runs)

    return store._execute(write, writable=True)


def run_due_once() -> dict[str, Any]:
    store = backend_store()
    store.initialize()
    now = datetime.now(timezone.utc)
    attempted = 0
    completed = 0
    failed = 0
    for task in store.list_tasks():
        if task["mode"] != "continuous" or task["status"] != "pending":
            continue
        due = _parse(task.get("next_run_at"))
        if due is not None and due > now:
            continue
        attempted += 1
        try:
            run_task(int(task["id"]), "schedule")
            completed += 1
        except Exception:
            failed += 1
    return {
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "checked_at": now.replace(microsecond=0).isoformat(),
    }


def run_forever(interval_seconds: int = 30, stop_event: threading.Event | None = None) -> None:
    event = stop_event or threading.Event()
    recover_interrupted()
    while not event.is_set():
        run_due_once()
        event.wait(max(1, int(interval_seconds)))

