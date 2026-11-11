"""Import a synthetic authorized JSONL export without network access."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import ROOT, settings
from .connectors import AuthorizedJsonlConnector, ConnectorInputError
from .event_store import EventStore, ImportConflict, StorageUnavailable
from .settings_service import effective_settings


def main() -> int:
    runtime = effective_settings(settings)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "fixtures" / "authorized")
    parser.add_argument("--db", type=Path, default=runtime.database_path)
    args = parser.parse_args()
    try:
        records = AuthorizedJsonlConnector(args.root, runtime.credential_hmac_key).collect()
        result = EventStore(args.db).import_authorized(records)
        print(f"authorized offline import: candidates={len(records)}, inserted={result['inserted']}, skipped={result['skipped']}")
        return 0
    except ImportConflict:
        print("Authorized import rejected: event ID conflict. Batch unchanged.")
        return 1
    except (ConnectorInputError, OSError, StorageUnavailable, ValueError):
        print("Authorized import failed. Check manifest, JSONL contract, authorization scope and database path; no candidate contents were logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
