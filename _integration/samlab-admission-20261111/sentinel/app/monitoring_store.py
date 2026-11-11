import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .event_store import ImportConflict, StorageUnavailable
from .models import MonitoringTask


class MonitoringStore:
    def __init__(self, path: Path, timeout: float = 0.25, attempts: int = 3):
        self.path = path.resolve()
        self.timeout = timeout
        self.attempts = attempts

    def _execute(self, operation, writable=False):
        for attempt in range(self.attempts):
            try:
                uri = self.path.as_uri() + ("?mode=rwc" if writable else "?mode=ro")
                with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as connection:
                    with connection:
                        return operation(connection)
            except sqlite3.OperationalError as error:
                locked = getattr(error, "sqlite_errorcode", None) in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
                if locked and attempt + 1 < self.attempts:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                raise StorageUnavailable("monitoring_database_locked" if locked else "monitoring_database_unavailable") from None
            except (sqlite3.DatabaseError, OSError):
                raise StorageUnavailable("monitoring_database_invalid") from None

        raise StorageUnavailable("monitoring_database_unavailable")

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise StorageUnavailable("monitoring_database_unavailable") from None
        self._execute(
            lambda connection: connection.execute(
                "CREATE TABLE IF NOT EXISTS monitoring_tasks "
                "(task_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            ),
            writable=True,
        )

    def import_synthetic(self, items: list[dict]) -> dict[str, int]:
        records = [MonitoringTask.model_validate(item) for item in items]
        self.initialize()

        def write(connection):
            inserted = 0
            for record in records:
                payload = record.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM monitoring_tasks WHERE task_id = ?",
                    (record.task_id,),
                ).fetchone()
                if existing:
                    if json.loads(existing[0]) != record.model_dump():
                        raise ImportConflict("monitoring_task_id_conflict")
                    continue
                connection.execute(
                    "INSERT INTO monitoring_tasks(task_id, payload) VALUES (?, ?)",
                    (record.task_id, payload),
                )
                inserted += 1
            return {"inserted": inserted, "skipped": len(records) - inserted}

        return self._execute(write, writable=True)

    def list_tasks(self) -> list[dict]:
        def read(connection):
            rows = connection.execute(
                "SELECT payload FROM monitoring_tasks ORDER BY task_id"
            ).fetchall()
            try:
                return [MonitoringTask.model_validate_json(row[0]).model_dump() for row in rows]
            except ValueError:
                raise StorageUnavailable("monitoring_record_contract_invalid") from None

        return self._execute(read)

    def get_task(self, task_id: str) -> dict | None:
        def read(connection):
            row = connection.execute(
                "SELECT payload FROM monitoring_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                return MonitoringTask.model_validate_json(row[0]).model_dump()
            except ValueError:
                raise StorageUnavailable("monitoring_record_contract_invalid") from None
        return self._execute(read)

    def update_runtime(self, task_id: str, status: str, last_run: str | None, run_result: str | None = None) -> None:
        self.initialize()
        def write(connection):
            row = connection.execute(
                "SELECT payload FROM monitoring_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise StorageUnavailable("monitoring_task_not_found")
            payload = MonitoringTask.model_validate_json(row[0]).model_dump()
            payload["status"] = status
            payload["status_color"] = {"运行中": "low", "采集失败": "high", "待配置": "medium", "已暂停": "medium"}[status]
            payload["last_run"] = last_run
            payload["last_run_status"] = run_result
            connection.execute(
                "UPDATE monitoring_tasks SET payload = ? WHERE task_id = ?",
                (MonitoringTask.model_validate(payload).model_dump_json(), task_id),
            )
        self._execute(write, writable=True)

    def create_task(self, payload: dict) -> dict:
        task = MonitoringTask.model_validate({
            **payload,
            "last_run": None,
            "status": "待配置",
            "status_color": "medium",
            "last_run_status": None,
        })
        self.initialize()
        def write(connection):
            if connection.execute(
                "SELECT 1 FROM monitoring_tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone():
                raise ValueError("monitoring_task_exists")
            connection.execute(
                "INSERT INTO monitoring_tasks(task_id, payload) VALUES (?, ?)",
                (task.task_id, task.model_dump_json()),
            )
            return task.model_dump()
        return self._execute(write, writable=True)

    def update_task(self, task_id: str, payload: dict) -> dict | None:
        self.initialize()
        def write(connection):
            row = connection.execute(
                "SELECT payload FROM monitoring_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            current = MonitoringTask.model_validate_json(row[0]).model_dump()
            current.update(payload)
            current["task_id"] = task_id
            updated = MonitoringTask.model_validate(current)
            connection.execute(
                "UPDATE monitoring_tasks SET payload = ? WHERE task_id = ?",
                (updated.model_dump_json(), task_id),
            )
            return updated.model_dump()
        return self._execute(write, writable=True)

    def set_paused(self, task_id: str, paused: bool) -> dict | None:
        self.initialize()
        def write(connection):
            row = connection.execute(
                "SELECT payload FROM monitoring_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            current = MonitoringTask.model_validate_json(row[0]).model_dump()
            current["status"] = "已暂停" if paused else "待配置"
            current["status_color"] = "medium"
            return self.update_task(task_id, current)
        return self._execute(write, writable=True)

    def delete_task(self, task_id: str) -> bool:
        self.initialize()
        def write(connection):
            cursor = connection.execute("DELETE FROM monitoring_tasks WHERE task_id = ?", (task_id,))
            return cursor.rowcount == 1
        return self._execute(write, writable=True)
