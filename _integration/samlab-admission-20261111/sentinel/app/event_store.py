import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, TypeVar

from .models import EventRecord


T = TypeVar("T")


class StorageUnavailable(Exception):
    """Contains a safe error code, never database contents or input previews."""


class ImportConflict(Exception):
    """An existing event ID has a different immutable payload."""


class EventStore:
    def __init__(self, path: Path, timeout: float = 0.25, attempts: int = 3):
        self.path = path.resolve()
        self.timeout = timeout
        self.attempts = attempts

    def _execute(self, operation: Callable[[sqlite3.Connection], T], writable: bool = False) -> T:
        for attempt in range(self.attempts):
            try:
                uri = self.path.as_uri() + ("?mode=rwc" if writable else "?mode=ro")
                with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as connection:
                    with connection:
                        return operation(connection)
            except sqlite3.OperationalError as error:
                locked = getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if locked and attempt + 1 < self.attempts:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                raise StorageUnavailable("database_locked" if locked else "database_unavailable") from None
            except (sqlite3.DatabaseError, OSError, ValueError):
                raise StorageUnavailable("database_invalid") from None
        raise StorageUnavailable("database_unavailable")

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StorageUnavailable("database_unavailable") from None
        self._execute(
            lambda connection: connection.execute(
                "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            ),
            writable=True,
        )

    def import_synthetic(self, items: list[dict]) -> dict[str, int]:
        return self._import(items, "合成样例")

    def import_detected(self, items: list[dict]) -> dict[str, int]:
        return self._import(items, "受控投放检出")

    def import_authorized(self, items: list[dict]) -> dict[str, int]:
        return self._import(items, "离线授权导入")

    def import_real(self, items: list[dict]) -> dict[str, int]:
        return self._import(items, "真实检测结果")

    def _import(self, items: list[dict], source_kind: str) -> dict[str, int]:
        # Connector-only provenance fields stay inside the ingestion pipeline.
        # The public EventRecord contract intentionally forbids underscore
        # fields so they cannot leak through legacy API responses.
        records = [
            EventRecord.model_validate({
                key: value for key, value in item.items()
                if not str(key).startswith("_")
            })
            for item in items
        ]
        if any(record.source_kind != source_kind for record in records):
            raise ValueError(f"this importer only accepts {source_kind} records")
        self.initialize()

        def write(connection: sqlite3.Connection) -> dict[str, int]:
            inserted = 0
            for record in records:
                existing = connection.execute(
                    "SELECT payload FROM events WHERE event_id = ?", (record.event_id,)
                ).fetchone()
                payload = record.model_dump_json()
                if existing:
                    try:
                        existing_record = EventRecord.model_validate_json(existing[0])
                    except ValueError:
                        raise StorageUnavailable("record_contract_invalid") from None
                    existing_payload = existing_record.model_dump()
                    new_payload = record.model_dump()
                    if source_kind in {"受控投放检出", "离线授权导入", "真实检测结果"}:
                        for payload in (existing_payload, new_payload):
                            payload.pop("detected_at", None)
                            payload.pop("first_observed_at", None)
                        if (
                            existing_payload.get("credential_fingerprint") is None
                            and new_payload.get("credential_fingerprint")
                            and all(
                                existing_payload.get(field) == new_payload.get(field)
                                for field in (
                                    "event_id", "source_kind", "channel", "platform",
                                    "credential_type", "redacted_value", "evidence_level",
                                    "risk_level", "disposition", "source_locator",
                                    "detection_method",
                                )
                            )
                        ):
                            connection.execute(
                                "UPDATE events SET payload = ? WHERE event_id = ?",
                                (record.model_dump_json(), record.event_id),
                            )
                            continue
                    if existing_payload != new_payload:
                        raise ImportConflict("event_id_conflict")
                    continue
                connection.execute(
                    "INSERT INTO events(event_id, payload) VALUES (?, ?)", (record.event_id, payload)
                )
                inserted += 1
            return {"inserted": inserted, "skipped": len(records) - inserted}

        return self._execute(write, writable=True)

    def list_events(self) -> list[dict]:
        def read(connection: sqlite3.Connection) -> list[dict]:
            rows = connection.execute("SELECT payload FROM events ORDER BY event_id").fetchall()
            try:
                return [EventRecord.model_validate_json(row[0]).model_dump() for row in rows]
            except ValueError:
                raise StorageUnavailable("record_contract_invalid") from None

        return self._execute(read)

    def get_event(self, event_id: str) -> dict | None:
        def read(connection: sqlite3.Connection) -> dict | None:
            row = connection.execute(
                "SELECT payload FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                return EventRecord.model_validate_json(row[0]).model_dump()
            except ValueError:
                raise StorageUnavailable("record_contract_invalid") from None

        return self._execute(read)

    def update_disposition(self, event_id: str, disposition: str) -> dict | None:
        """Update only the workflow disposition while preserving masked evidence."""
        if not disposition:
            raise ValueError("disposition_required")

        def write(connection: sqlite3.Connection) -> dict | None:
            row = connection.execute(
                "SELECT payload FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                record = EventRecord.model_validate_json(row[0])
            except ValueError:
                raise StorageUnavailable("record_contract_invalid") from None
            payload = record.model_dump()
            payload["disposition"] = disposition
            updated = EventRecord.model_validate(payload)
            connection.execute(
                "UPDATE events SET payload = ? WHERE event_id = ?",
                (updated.model_dump_json(), event_id),
            )
            return updated.model_dump()

        return self._execute(write, writable=True)
