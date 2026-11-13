"""Export the current masked run-evidence report for现场复核."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .backend_store import BackendStore
from .config import ROOT
from .evidence_export import build_run_evidence_package


def main() -> int:
    parser = argparse.ArgumentParser(description="导出脱敏运行证据包")
    parser.add_argument("--db", default=os.environ.get("BACKEND_DB_PATH", str(ROOT / "data" / "backend.sqlite3")))
    parser.add_argument("--master-key", default=None)
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--run-id", type=int, default=None, help="将证据包登记到指定扫描批次")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    master_key = (
        Path(args.master_key).expanduser().resolve()
        if args.master_key
        else db_path.parent / "backend.master.key"
    )
    output = Path(args.output).expanduser().resolve()
    store = BackendStore(db_path, master_key_path=master_key)
    store.initialize()
    package = build_run_evidence_package(store.evidence_report(limit=args.limit))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    try:
        relative_path = output.relative_to(ROOT).as_posix()
    except ValueError:
        relative_path = output.name
    artifact = store.register_evidence_artifact({
        "artifact_id": f"evidence-report-{digest[:16]}",
        "run_id": args.run_id,
        "artifact_type": "masked_run_evidence_json",
        "relative_path": relative_path,
        "sha256": digest,
        "record_count": len(package.get("runs") or []),
        "source_kind": "脱敏运行证据",
        "metadata": {
            "summary": package.get("summary") or {},
            "raw_secret_included": False,
        },
    })
    print(json.dumps({
        "output": str(output),
        "sha256": digest,
        "artifact_id": artifact["artifact_id"],
        "run_count": len(package.get("runs") or []),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
