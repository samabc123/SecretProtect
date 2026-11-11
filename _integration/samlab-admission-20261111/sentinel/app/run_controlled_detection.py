"""Run the local controlled-fixture detector and persist masked events."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import ROOT, settings
from .controlled_detector import ControlledFixtureError, detect_controlled_fixtures
from .event_store import EventStore, ImportConflict, StorageUnavailable
from .settings_service import effective_settings
from .backend_pipeline import backend_store


def main() -> int:
    runtime = effective_settings(settings)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "fixtures" / "controlled")
    parser.add_argument("--db", type=Path, default=runtime.database_path)
    args = parser.parse_args()
    try:
        default_root = (ROOT / "fixtures" / "controlled").resolve()
        requested_root = args.root.expanduser().resolve()
        if args.root == parser.get_default("root") and requested_root != default_root:
            raise ControlledFixtureError("default controlled root is unavailable")
        backend = backend_store()
        backend.initialize()
        enabled_rule_ids = frozenset(
            str(rule["id"])
            for rule in backend.list_rules()
            if bool(rule.get("enabled"))
        )
        items = detect_controlled_fixtures(
            args.root,
            hmac_key=runtime.credential_hmac_key,
            fingerprint_key_version="v1",
            enabled_rule_ids=enabled_rule_ids,
        )
        result = EventStore(args.db).import_detected(items)
        print(f"controlled fixture detection: candidates={len(items)}, inserted={result['inserted']}, skipped={result['skipped']}")
        return 0
    except ImportConflict:
        print("Controlled detection rejected: event ID conflict. Batch unchanged.")
        return 1
    except (ControlledFixtureError, OSError, StorageUnavailable, ValueError):
        print("Controlled detection failed. Check the explicit local fixture root and database path; no matched contents were logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
