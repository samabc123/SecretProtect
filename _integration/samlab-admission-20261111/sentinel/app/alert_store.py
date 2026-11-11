"""SQLite alert queue and event feedback audit for the offline response loop."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .time_utils import utc_now_iso

from .event_store import StorageUnavailable
from .models import EventRecord


class AlertStore:
    TRANSITIONS = {
        "confirm_public": "已确认公开泄露",
        "mark_duplicate": "重复事件",
        "mark_false_positive": "误报待复核",
        "recheck": "已复查",
        "close_event": "已关闭",
    }

    def __init__(self, path: Path, timeout: float = 0.25, attempts: int = 3, event_path: Path | None = None):
        self.path = path.resolve()
        self.event_path = (event_path or path).resolve()
        self.timeout = timeout
        self.attempts = attempts

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
                with connection:
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS alert_outbox (
                        alert_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        severity TEXT NOT NULL,
                        status TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT,
                        delivery_note TEXT NOT NULL)"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS event_feedback (
                        feedback_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        disposition TEXT NOT NULL DEFAULT '',
                        note TEXT NOT NULL,
                        created_at TEXT NOT NULL)"""
                    )
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(event_feedback)").fetchall()
                    }
                    if "disposition" not in columns:
                        connection.execute(
                            "ALTER TABLE event_feedback ADD COLUMN disposition TEXT NOT NULL DEFAULT ''"
                        )
        except (sqlite3.DatabaseError, OSError):
            raise StorageUnavailable("response_database_unavailable") from None

    @staticmethod
    def _now() -> str:
        return utc_now_iso()

    @staticmethod
    def _safe_note(note: str) -> str:
        if len(note) > 500 or "\n" in note or "\r" in note:
            raise ValueError("feedback_note_invalid")
        return "备注已省略，避免持久化潜在敏感信息。" if note.strip() else ""

    @staticmethod
    def _row_to_alert(row) -> dict:
        return dict(zip(
            ("alert_id", "event_id", "severity", "status", "channel",
             "created_at", "delivered_at", "delivery_note"),
            row,
        ))

    def ensure_alert(self, event: dict) -> dict:
        record = EventRecord.model_validate(event)
        self.initialize()
        alert_id = f"ALERT-{record.event_id}"
        now = self._now()
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO alert_outbox "
                    "(alert_id,event_id,severity,status,channel,created_at,delivered_at,delivery_note) "
                    "VALUES (?, ?, ?, 'queued', 'offline-sink', ?, NULL, ?)",
                    (
                        alert_id,
                        record.event_id,
                        record.risk_level,
                        now,
                        "离线通知桩：待手动记录，仅写入本地 SQLite，未发送外部消息。",
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM alert_outbox WHERE alert_id = ?", (alert_id,)
                ).fetchone()
                return self._row_to_alert(row)

    def get_alert_for_event(self, event_id: str) -> dict | None:
        if not self.path.exists():
            raise StorageUnavailable("response_database_unavailable")
        try:
            with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
                row = connection.execute(
                    "SELECT * FROM alert_outbox WHERE event_id = ?", (event_id,)
                ).fetchone()
                return self._row_to_alert(row) if row else None
        except (sqlite3.DatabaseError, OSError):
            raise StorageUnavailable("response_database_invalid") from None

    def list_alerts(self, limit: int = 100) -> list[dict]:
        if not self.path.exists():
            raise StorageUnavailable("response_database_unavailable")
        try:
            with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(
                    "SELECT * FROM alert_outbox ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()]
        except (sqlite3.DatabaseError, OSError):
            raise StorageUnavailable("response_database_invalid") from None

    def deliver_offline(self, alert_id: str) -> dict | None:
        self.initialize()
        now = self._now()
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
            with connection:
                connection.execute(
                    "UPDATE alert_outbox SET status='offline_recorded', delivered_at=?, "
                    "delivery_note=? WHERE alert_id=?",
                    (now, "离线通知桩已记录；未访问外部通知通道。", alert_id),
                )
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM alert_outbox WHERE alert_id=?", (alert_id,)
                ).fetchone()
                return dict(row) if row else None

    def add_feedback(self, event_id: str, action: str, note: str = "") -> dict:
        if action not in self.TRANSITIONS:
            raise ValueError("feedback_action_not_allowed")
        safe_note = self._safe_note(note)
        self.initialize()
        now = self._now()
        feedback_id = f"FB-{uuid.uuid4().hex[:16].upper()}"
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
            same_database = self.path == self.event_path
            event_connection = connection if same_database else sqlite3.connect(
                self.event_path, timeout=self.timeout
            )
            try:
                with connection:
                    event_connection.row_factory = sqlite3.Row
                    row = event_connection.execute(
                        "SELECT payload FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if row is None:
                        raise StorageUnavailable("event_not_found")
                    record = EventRecord.model_validate_json(row[0])
                    allowed = {
                        "待确认": {"confirm_public", "mark_duplicate", "mark_false_positive", "recheck"},
                        "已复查": {"confirm_public", "mark_duplicate", "mark_false_positive", "close_event"},
                        "已确认公开泄露": {"recheck", "close_event"},
                        "重复事件": {"recheck", "close_event"},
                        "误报待复核": {"recheck", "close_event"},
                        "已关闭": set(),
                        "受控环境确认": {"close_event"},
                    }
                    if action not in allowed.get(record.disposition, set()):
                        raise ValueError("feedback_transition_not_allowed")
                    disposition = self.TRANSITIONS[action]
                    if action == "confirm_public" and record.source_kind != "真实检测结果":
                        disposition = "受控环境确认"
                    payload = record.model_dump()
                    payload["disposition"] = disposition
                    updated = EventRecord.model_validate(payload)
                    event_connection.execute(
                        "UPDATE events SET payload=? WHERE event_id=?",
                        (updated.model_dump_json(), event_id),
                    )
                    if event_connection is not connection:
                        event_connection.commit()
                    connection.execute(
                        "INSERT INTO event_feedback"
                        "(feedback_id,event_id,action,disposition,note,created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (feedback_id, event_id, action, disposition, safe_note, now),
                    )
            except StorageUnavailable:
                raise
            except ValueError:
                raise
            except (sqlite3.DatabaseError, ValueError):
                raise StorageUnavailable("response_database_invalid") from None
            finally:
                if event_connection is not connection:
                    event_connection.close()
        return {
            "feedback_id": feedback_id,
            "event_id": event_id,
            "action": action,
            "disposition": disposition,
            "created_at": now,
            "note": safe_note,
        }

    def list_feedback(self, event_id: str) -> list[dict]:
        if not self.path.exists():
            raise StorageUnavailable("response_database_unavailable")
        try:
            self.initialize()
            with closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT feedback_id,event_id,action,disposition,note,created_at "
                    "FROM event_feedback WHERE event_id=? ORDER BY created_at ASC",
                    (event_id,),
                ).fetchall()
                return [dict(row) for row in rows]
        except (sqlite3.DatabaseError, OSError):
            raise StorageUnavailable("response_database_invalid") from None
