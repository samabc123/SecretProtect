"""Offline importer for monitoring task configuration samples only."""
import argparse
from pathlib import Path

from .config import ROOT, settings
from .event_store import ImportConflict, StorageUnavailable
from .models import MonitoringTaskResponse
from .monitoring_store import MonitoringStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "mock" / "monitoring_tasks.json")
    parser.add_argument("--db", type=Path, default=settings.monitoring_database_path)
    parser.add_argument("--init-only", action="store_true")
    args = parser.parse_args()
    store = MonitoringStore(args.db)
    try:
        if args.init_only:
            store.initialize()
            print("Monitoring database initialized. No tasks imported.")
            return 0
        response = MonitoringTaskResponse.model_validate_json(
            args.input.read_text(encoding="utf-8")
        )
        result = store.import_synthetic([item.model_dump() for item in response.items])
        print(f"task samples only: inserted={result['inserted']}, skipped={result['skipped']}")
        return 0
    except ImportConflict:
        print("Task import rejected: task ID conflict. Entire batch rolled back; existing records unchanged.")
        return 1
    except (OSError, ValueError, StorageUnavailable):
        print("Task import rejected or storage unavailable. No network access or task execution was attempted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
