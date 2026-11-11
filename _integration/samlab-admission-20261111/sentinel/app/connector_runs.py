from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .time_utils import utc_now_iso


class ConnectorRunStore:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS connector_runs (
                    run_id TEXT PRIMARY KEY, connector_name TEXT NOT NULL,
                    target TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, candidates INTEGER NOT NULL,
                    inserted INTEGER NOT NULL, error_code TEXT,
                    task_id TEXT)"""
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS connector_locks (task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS connector_checkpoints (target TEXT PRIMARY KEY, checkpoint TEXT NOT NULL)"
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS connector_file_checkpoints (
                    target TEXT NOT NULL,
                    tree_sha TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_sha TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target, file_path))"""
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(connector_runs)").fetchall()}
                if "task_id" not in columns:
                    connection.execute("ALTER TABLE connector_runs ADD COLUMN task_id TEXT")

    def start(self, run_id: str, connector_name: str, target: str, status: str = "running", task_id: str | None = None) -> None:
        self.initialize()
        now = utc_now_iso()
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO connector_runs VALUES (?, ?, ?, ?, NULL, ?, 0, 0, NULL, ?)",
                    (run_id, connector_name, target, now, status, task_id),
                )

    def acquire(self, task_id: str, run_id: str) -> bool:
        self.initialize()
        with closing(sqlite3.connect(self.path, timeout=0.25)) as connection:
            try:
                with connection:
                    connection.execute("INSERT INTO connector_locks VALUES (?, ?)", (task_id, run_id))
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, task_id: str, run_id: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.25)) as connection:
            with connection:
                connection.execute("DELETE FROM connector_locks WHERE task_id = ? AND run_id = ?", (task_id, run_id))

    def finish(self, run_id: str, status: str, candidates: int, inserted: int, error_code: str | None = None) -> None:
        now = utc_now_iso()
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE connector_runs SET finished_at=?, status=?, candidates=?, inserted=?, error_code=? WHERE run_id=?",
                    (now, status, candidates, inserted, error_code, run_id),
                )

    def get_checkpoint(self, target: str) -> str | None:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT checkpoint FROM connector_checkpoints WHERE target = ?", (target,)).fetchone()
            return row[0] if row else None

    def set_checkpoint(self, target: str, checkpoint: str) -> None:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO connector_checkpoints(target, checkpoint) VALUES (?, ?) "
                    "ON CONFLICT(target) DO UPDATE SET checkpoint=excluded.checkpoint",
                    (target, checkpoint),
                )

    def get_file_checkpoint(self, target: str, file_path: str) -> dict | None:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT target, tree_sha, file_path, file_sha, status, updated_at "
                "FROM connector_file_checkpoints WHERE target = ? AND file_path = ?",
                (target, file_path),
            ).fetchone()
            return dict(row) if row else None

    def set_file_checkpoint(
        self, target: str, tree_sha: str, file_path: str, file_sha: str, status: str = "completed"
    ) -> None:
        self.initialize()
        now = utc_now_iso()
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO connector_file_checkpoints "
                    "(target, tree_sha, file_path, file_sha, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(target, file_path) DO UPDATE SET "
                    "tree_sha=excluded.tree_sha, file_sha=excluded.file_sha, "
                    "status=excluded.status, updated_at=excluded.updated_at",
                    (target, tree_sha, file_path, file_sha, status, now),
                )

    def list_runs(self) -> list[dict]:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM connector_runs ORDER BY started_at DESC"
            ).fetchall()]
