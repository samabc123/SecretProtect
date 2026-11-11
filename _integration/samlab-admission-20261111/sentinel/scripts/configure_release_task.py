"""Restore one released local task using the recipient's own input path."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--path", required=True)
    args = parser.parse_args()

    policy_path = ROOT / "release-task-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if args.source_id not in policy["hidden_source_ids"]:
        parser.error("Source is not a hidden release task. Edit it in the UI.")
    target = Path(args.path).expanduser().resolve()

    # This utility must operate on this release, never on an author's DB override.
    os.environ["BACKEND_DB_PATH"] = str(ROOT / "data" / "backend.sqlite3")
    from app.backend_pipeline import backend_store

    store = backend_store()
    source = store.get_source(args.source_id)
    if source is None:
        parser.error("Source not found.")
    collector = source["collector_key"]
    if collector not in {"local_dir", "controlled_fixtures", "authorized_jsonl", "apk", "wxapkg"}:
        parser.error("Online sources must be created in the UI with your own authorization.")
    if collector in {"apk", "wxapkg"}:
        if not target.is_file():
            parser.error("APK/wxapkg path must be an existing file.")
        if target.suffix.lower() != "." + collector:
            parser.error("File extension does not match the collector.")
    elif not target.is_dir():
        parser.error("Path must be an existing directory.")
    if collector == "authorized_jsonl" and not (target / "manifest.json").is_file():
        parser.error("JSONL directory must contain manifest.json.")

    # Caller owns the path; do not start a scan or change historical evidence.
    config = dict(source["config"])
    config["path"] = target.as_posix()
    store.update_source(
        args.source_id, {"config": config, "enabled": True, "mode": "manual"}
    )
    task_id = int(source["task"]["id"])
    store.task_action(task_id, "retry")
    policy["hidden_source_ids"] = [
        value for value in policy["hidden_source_ids"] if value != args.source_id
    ]
    policy["hidden_task_ids"] = [
        value for value in policy["hidden_task_ids"] if value != task_id
    ]
    temporary = policy_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(policy_path)
    print("Task restored. Refresh Monitoring Tasks. No scan was started.")


if __name__ == "__main__":
    main()
