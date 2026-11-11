"""Run configured monitoring tasks in bounded offline or authorized mode."""

from __future__ import annotations

import argparse
import time

from .config import ROOT, settings
from .data_access import run_monitoring_task
from .monitoring_store import MonitoringStore
from .settings_service import effective_settings
from .backend_pipeline import backend_store
from .backend_scheduler import run_due_once


def _display_status(result: dict) -> str:
    """Separate expected authorization blocks from connector execution failures."""
    nested = result.get("result")
    if isinstance(nested, dict) and nested.get("status"):
        return str(nested["status"])
    if result.get("error"):
        error = str(result["error"])
        if "白名单" in error or "授权范围" in error or "联网" in error:
            return "blocked"
        return "failed"
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run each configured task once")
    parser.add_argument("--interval", type=int, default=60, help="seconds between rounds")
    args = parser.parse_args()
    runtime = effective_settings(settings)
    if runtime.monitoring_source != "sqlite":
        print("scheduler refused: MONITORING_SOURCE must be sqlite")
        return 1
    # The product now uses the normalized backend by default. Keep the legacy
    # task scheduler available for isolated/older databases, but do not feed
    # legacy display-only task IDs into the normalized task table.
    if (
        runtime.monitoring_database_path.resolve()
        == (ROOT / "data" / "monitoring.sqlite3").resolve()
        and backend_store().path.exists()
    ):
        if args.once:
            print(run_due_once())
            return 0
        print(
            f"unified scheduler started; checking every {max(1, args.interval)} seconds. "
            "Press Ctrl+C to stop."
        )
        try:
            while True:
                print(run_due_once())
                time.sleep(max(1, args.interval))
        except KeyboardInterrupt:
            return 0
    while True:
        tasks = MonitoringStore(runtime.monitoring_database_path).list_tasks()
        for task in tasks:
            if task["status"] != "已暂停":
                result = run_monitoring_task(task["task_id"])
                print(f'{task["task_id"]}: {_display_status(result)}')
        if args.once:
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
