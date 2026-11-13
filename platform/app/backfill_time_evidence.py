"""公开时间证据的只读预览、保守补写和回退工具。

默认只读预览。补写只合并已有字段能够证明的 provenance，不会把发现、
采集、文件修改或当前时间写成公开时间；已有非空证据不会被覆盖。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .time_provenance import legacy_published_entry, merge_time_provenance, time_entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _backup_database(database: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(database)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT id,published_at,published_source,published_confidence,
               source_updated_at,fetched_at,fetched_at_source,time_provenance_json
        FROM source_contents ORDER BY id
        """
    ).fetchall():
        existing = _load(row["time_provenance_json"])
        incoming = {
            "published_at": legacy_published_entry(
                row["published_at"],
                row["published_source"],
                row["published_confidence"],
            ),
            "fetched_at": time_entry(
                row["fetched_at"],
                source=str(row["fetched_at_source"] or "unknown"),
                meaning=(
                    "连接器完成读取当前内容的时间。"
                    if row["fetched_at_source"] == "connector"
                    else "历史记录没有连接器明确采集时间，保持为不可用于精确采集时效的参考值。"
                ),
                confidence="high" if row["fetched_at_source"] == "connector" else "low",
                role="acquisition_completion",
                eligible_for_latency=(
                    row["fetched_at_source"] == "connector" and bool(row["fetched_at"])
                ),
                unknown_reason=(
                    "" if row["fetched_at_source"] == "connector" and row["fetched_at"]
                    else "acquisition_time_not_provided"
                ),
                backfill_reason="由旧 source_contents 字段补写 provenance。",
            ),
        }
        if row["source_updated_at"]:
            incoming["source_updated_at"] = time_entry(
                row["source_updated_at"],
                source="legacy_source_updated_at",
                meaning="历史内容版本更新时间，仅作为内容修订参考，不是公开泄露时间。",
                confidence="low",
                role="content_revision_reference",
                evidence="source_contents.source_updated_at",
                backfill_reason="由旧 source_contents.source_updated_at 补写。",
            )
        merged = merge_time_provenance(existing, incoming)
        if _dump(merged) != _dump(existing):
            result.append({
                "table_name": "source_contents",
                "row_id": int(row["id"]),
                "field_name": "time_provenance_json",
                "old_value": _dump(existing),
                "new_value": _dump(merged),
                "basis": "只合并已有公开/采集/修订字段；不推导首次公开时间。",
            })

    for row in connection.execute(
        """
        SELECT id,first_seen_at,detected_at,first_discovered_at,
               last_discovered_at,time_provenance_json
        FROM occurrences ORDER BY id
        """
    ).fetchall():
        existing = _load(row["time_provenance_json"])
        incoming = {
            "first_observed_at": time_entry(
                row["first_seen_at"],
                source="occurrence_storage",
                meaning="本系统记录的当前内容位置首次观测时间。",
                confidence="medium" if row["first_seen_at"] else "none",
                role="system_observation",
                unknown_reason=(
                    "" if row["first_seen_at"] else "observation_time_not_provided"
                ),
                backfill_reason="由旧 occurrences.first_seen_at 补写。",
            ),
            "detected_at": time_entry(
                row["detected_at"],
                source="occurrence_storage",
                meaning="历史记录中的规则检测完成时间；处理耗时不由墙上时钟推导。",
                confidence="low" if row["detected_at"] else "none",
                role="detection_completion",
                unknown_reason=(
                    "" if row["detected_at"] else "detection_time_not_provided"
                ),
                backfill_reason="由旧 occurrences.detected_at 补写。",
            ),
        }
        if row["first_discovered_at"]:
            incoming["first_discovered_at"] = time_entry(
                row["first_discovered_at"],
                source="occurrence_storage",
                meaning="历史记录中的首次发现时间，仅保留为待核查参考。",
                confidence="low",
                role="historical_first_valid_detection",
                eligible_for_latency=False,
                unknown_reason="historical_first_discovery_time_unverified",
                backfill_reason="由已有 occurrences.first_discovered_at 描述 provenance。",
            )
        if row["last_discovered_at"]:
            incoming["last_discovered_at"] = time_entry(
                row["last_discovered_at"],
                source="occurrence_storage",
                meaning="该内容位置最近一次形成有效凭据检测结果的时间。",
                confidence="low",
                role="latest_valid_detection",
                backfill_reason="由已有 occurrences.last_discovered_at 描述 provenance。",
            )
        merged = merge_time_provenance(existing, incoming)
        if _dump(merged) != _dump(existing):
            result.append({
                "table_name": "occurrences",
                "row_id": int(row["id"]),
                "field_name": "time_provenance_json",
                "old_value": _dump(existing),
                "new_value": _dump(merged),
                "basis": "只描述已有 occurrence 时间；不跨来源或版本配对。",
            })
    return result


def preview(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        items = _candidates(connection)
    finally:
        connection.close()
    return {
        "schema_version": "1.0",
        "mode": "preview",
        "database": str(database.resolve()),
        "generated_at": _now(),
        "candidate_count": len(items),
        "items": items,
        "note": "预览只读；没有证据的公开时间保持未知。",
    }


def apply(database: Path, backup: Path | None = None) -> dict[str, Any]:
    operation_id = f"TB-{uuid.uuid4().hex[:16].upper()}"
    if backup:
        _backup_database(database, backup)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    started = _now()
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        items = _candidates(connection)
        connection.execute(
            """
            INSERT INTO time_backfill_runs(
                operation_id,mode,status,started_at,backup_path,candidate_count
            ) VALUES (?, 'apply', 'running', ?, ?, ?)
            """,
            (operation_id, started, str(backup or ""), len(items)),
        )
        for item in items:
            connection.execute(
                f"UPDATE {item['table_name']} SET {item['field_name']}=? WHERE id=?",
                (item["new_value"], item["row_id"]),
            )
            connection.execute(
                """
                INSERT INTO time_backfill_changes(
                    operation_id,table_name,row_id,field_name,old_value,new_value,basis,changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    item["table_name"],
                    item["row_id"],
                    item["field_name"],
                    item["old_value"],
                    item["new_value"],
                    item["basis"],
                    _now(),
                ),
            )
        connection.execute(
            """
            UPDATE time_backfill_runs
            SET status='completed',completed_at=?,applied_count=?
            WHERE operation_id=?
            """,
            (_now(), len(items), operation_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.execute(
            """
            INSERT OR REPLACE INTO time_backfill_runs(
                operation_id,mode,status,started_at,completed_at,backup_path,candidate_count,notes
            ) VALUES (?, 'apply', 'failed', ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                started,
                _now(),
                str(backup or ""),
                0,
                "补写事务失败，未提交变更。",
            ),
        )
        connection.commit()
        raise
    finally:
        connection.close()
    return {
        "schema_version": "1.0",
        "mode": "apply",
        "operation_id": operation_id,
        "database": str(database.resolve()),
        "backup": str(backup.resolve()) if backup else "",
        "candidate_count": len(items),
        "applied_count": len(items),
        "status": "completed",
    }


def rollback(database: Path, operation_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT table_name,row_id,field_name,old_value,new_value
            FROM time_backfill_changes
            WHERE operation_id=? ORDER BY id DESC
            """,
            (operation_id,),
        ).fetchall()
        if not rows:
            run = connection.execute(
                """
                SELECT mode,status,applied_count
                FROM time_backfill_runs
                WHERE operation_id=?
                """,
                (operation_id,),
            ).fetchone()
            if run is None:
                raise ValueError("backfill_operation_not_found")
            if run["mode"] != "apply" or run["status"] not in {"completed", "rolled_back"}:
                raise ValueError("backfill_operation_not_rollbackable")
            if int(run["applied_count"] or 0) != 0:
                raise ValueError("backfill_change_records_missing")
        for row in rows:
            connection.execute(
                f"UPDATE {row['table_name']} SET {row['field_name']}=? WHERE id=?",
                (row["old_value"], row["row_id"]),
            )
        connection.execute(
            "UPDATE time_backfill_runs SET status='rolled_back',completed_at=? WHERE operation_id=?",
            (_now(), operation_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "schema_version": "1.0",
        "mode": "rollback",
        "operation_id": operation_id,
        "database": str(database.resolve()),
        "rolled_back_count": len(rows),
        "status": "completed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="公开时间证据补写预览与回退工具")
    parser.add_argument("--db", default="data/backend.sqlite3")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--rollback")
    args = parser.parse_args()
    database = Path(args.db).expanduser().resolve()
    if not database.is_file():
        raise SystemExit("数据库文件不存在")
    if args.rollback:
        result = rollback(database, args.rollback)
    elif args.apply:
        result = apply(database, Path(args.backup).expanduser().resolve() if args.backup else None)
    else:
        result = preview(database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
