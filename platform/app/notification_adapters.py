"""统一通知适配器契约。

当前默认适配器只写本地 SQLite outbox，不访问邮件、Webhook 或第三方消息
服务。通知事件只包含脱敏元数据，适配器不会接收完整凭据原文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


NOTIFICATION_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class NotificationEvent:
    event_id: str
    severity: str
    source_kind: str
    fingerprint: str | None
    created_at: str

    def safe_payload(self) -> dict[str, Any]:
        return {
            "schema_version": NOTIFICATION_SCHEMA_VERSION,
            "event_id": self.event_id,
            "severity": self.severity,
            "source_kind": self.source_kind,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


class OfflineSQLiteNotificationAdapter:
    """SQLite-only notification sink used by the offline demo and audit path."""

    adapter_id = "offline-sqlite"
    channel = "offline-sqlite"

    @staticmethod
    def create_event(
        *,
        event_id: str,
        severity: str,
        source_kind: str,
        fingerprint: str | None,
        created_at: str,
    ) -> NotificationEvent:
        return NotificationEvent(
            event_id=str(event_id),
            severity=str(severity or "unknown"),
            source_kind=str(source_kind or "未知"),
            fingerprint=str(fingerprint) if fingerprint else None,
            created_at=str(created_at),
        )

    def enqueue(self, connection, event: NotificationEvent, *, note: str) -> None:
        """Insert a notification idempotently.

        The connection and transaction belong to ``BackendStore``. Keeping the
        adapter transaction-neutral makes it replaceable by a real provider
        without moving database commit boundaries.
        """
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox(
                notification_id,event_id,event_schema_version,severity,status,channel,
                created_at,delivered_at,delivery_note,attempt_count,last_error,
                next_attempt_at,payload_json
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, NULL, ?, 0, NULL, NULL, ?)
            """,
            (
                f"NOTIFY-{event.event_id}",
                event.event_id,
                NOTIFICATION_SCHEMA_VERSION,
                event.severity,
                self.channel,
                event.created_at,
                note,
                _dump_safe(event.safe_payload()),
            ),
        )

    def record(self, connection, notification_id: str, *, now: str, note: str) -> None:
        connection.execute(
            """
            UPDATE notification_outbox
            SET status='offline_recorded',delivered_at=?,delivery_note=?,
                attempt_count=attempt_count+1,last_error=NULL,next_attempt_at=NULL
            WHERE notification_id=?
            """,
            (now, note, notification_id),
        )

    def retry(self, connection, notification_id: str, *, now: str, note: str) -> None:
        connection.execute(
            """
            UPDATE notification_outbox
            SET status='queued',delivered_at=NULL,delivery_note=?,
                last_error=NULL,next_attempt_at=?,attempt_count=attempt_count+1
            WHERE notification_id=?
            """,
            (note, now, notification_id),
        )


def _dump_safe(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

