"""命令行运行发布包运行时扩展性检查。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backend_store import BackendStore
from .experiment_runner import run_extensibility_experiments


def main() -> int:
    parser = argparse.ArgumentParser(description="运行发布包运行时扩展性检查并写入统一 SQLite")
    parser.add_argument("--db", type=Path, default=Path("data/backend.sqlite3"))
    parser.add_argument("--master-key", type=Path, default=Path("data/backend.master.key"))
    args = parser.parse_args()
    store = BackendStore(args.db, master_key_path=args.master_key)
    results = run_extensibility_experiments(store)
    print(json.dumps({
        "ok": True,
        "count": len(results),
        "passed": sum(item["execution_status"] == "passed" for item in results),
        "failed": sum(item["execution_status"] == "failed" for item in results),
        "not_run": sum(item["execution_status"] == "not_run" for item in results),
        "real_platform_tested": False,
        "items": [
            {
                "connector_key": item["connector_key"],
                "execution_status": item["execution_status"],
                "code_lines": item["code_lines"],
                "config_fields": item["config_fields"],
                "test_duration_ms": item["test_duration_ms"],
            }
            for item in results
        ],
    }, ensure_ascii=False))
    return 0 if not any(item["execution_status"] == "failed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

