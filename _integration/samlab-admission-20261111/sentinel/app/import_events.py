"""Offline importer for masked synthetic records only; never scans content."""
import argparse
import json
from pathlib import Path

from .config import ROOT, settings
from .event_store import EventStore, ImportConflict, StorageUnavailable
from .models import EventResponse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "mock" / "events.json")
    parser.add_argument("--db", type=Path, default=settings.database_path)
    parser.add_argument("--init-only", action="store_true")
    args = parser.parse_args()
    store = EventStore(args.db)
    try:
        if args.init_only:
            store.initialize()
            print("Database initialized. No samples imported.")
            return 0
        response = EventResponse.model_validate_json(args.input.read_text(encoding="utf-8"))
        result = store.import_synthetic([item.model_dump() for item in response.items])
        print(f"synthetic only: inserted={result['inserted']}, skipped={result['skipped']}")
        return 0
    except ImportConflict:
        print("Import rejected: event ID conflict. Entire batch rolled back; existing records unchanged.")
        return 1
    except (OSError, ValueError, StorageUnavailable):
        print("Import rejected or storage unavailable. Check schema, synthetic provenance, masked previews and database path. No input contents logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
