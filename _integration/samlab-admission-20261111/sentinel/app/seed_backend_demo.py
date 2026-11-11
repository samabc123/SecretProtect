"""Seed an explicit synthetic-only dataset for the unified UI demo.

This command never reads a token, never accesses the network, and labels every
record as ``合成样例``. It is idempotent and is intended only for local review.
"""

from __future__ import annotations

import json

from .backend_pipeline import backend_store
from .config import ROOT


def main() -> int:
    store = backend_store()
    store.initialize()
    if store.list_sources():
        print("unified demo already initialized")
        return 0
    source = store.create_source({
        "name": "离线合成回放（仅演示）",
        "collector_key": "authorized_jsonl",
        "config": {"path": "fixtures/authorized"},
        "mode": "manual",
        "enabled": False,
    })
    run = store.create_run(int(source["task"]["id"]), "demo_seed")
    payload = json.loads((ROOT / "mock" / "events.json").read_text(encoding="utf-8"))
    events = []
    for item in payload.get("items", []):
        event = dict(item)
        event["source_kind"] = "合成样例"
        event.setdefault("source_locator", f"offline-demo:{event['event_id']}")
        events.append(event)
    result = store.ingest_events(int(source["id"]), int(run["id"]), events)
    store.finish_run(int(run["id"]), "completed", {**result, "stop_reason": "synthetic_demo_seed"})
    print(f"synthetic unified demo: candidates={result['candidates']}, inserted={result['new_occurrences']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
