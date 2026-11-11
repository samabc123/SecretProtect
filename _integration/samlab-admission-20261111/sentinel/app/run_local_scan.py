"""Scan an explicitly authorized local directory with the YAML rule engine."""

from __future__ import annotations

import argparse
from pathlib import Path

from .alert_store import AlertStore
from .config import settings
from .event_store import EventStore, ImportConflict, StorageUnavailable
from .local_scan import LocalScanError, scan_local_directory
from .settings_service import effective_settings
from .backend_pipeline import backend_store


def main() -> int:
    runtime = effective_settings(settings)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="explicitly authorized real local directory",
    )
    parser.add_argument("--db", type=Path, default=runtime.database_path)
    args = parser.parse_args()
    if not args.root:
        print("Local rule scan requires --root pointing to an explicitly authorized directory.")
        return 1
    try:
        backend = backend_store()
        backend.initialize()
        enabled_rule_ids = frozenset(
            str(rule["id"])
            for rule in backend.list_rules()
            if bool(rule.get("enabled"))
        )
        records = scan_local_directory(
            args.root,
            hmac_key=runtime.credential_hmac_key,
            enabled_rule_ids=enabled_rule_ids,
        )
        result = EventStore(args.db).import_detected(records)
        response = AlertStore(runtime.response_database_path, event_path=args.db)
        for record in records:
            response.ensure_alert(record)
        print(
            "local rule scan: "
            f"candidates={len(records)}, inserted={result['inserted']}, "
            f"skipped={result['skipped']}"
        )
        return 0
    except ImportConflict:
        print("Local rule scan rejected: event ID conflict; batch unchanged.")
        return 1
    except (LocalScanError, OSError, StorageUnavailable, ValueError):
        print(
            "Local rule scan failed. Check the explicit authorized directory, "
            "HMAC key and database path; no matched contents were logged."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
