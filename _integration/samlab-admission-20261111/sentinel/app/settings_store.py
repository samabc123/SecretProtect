"""SQLite-backed runtime settings with local encryption for sensitive values."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SettingsStorageError(Exception):
    """Safe settings storage error; never contains setting values."""


class SettingsStore:
    def __init__(self, path: Path, master_key_path: Path, timeout: float = 0.5):
        self.path = path.resolve()
        self.master_key_path = master_key_path.resolve()
        self.timeout = timeout

    def _fernet(self) -> Fernet:
        try:
            self.master_key_path.parent.mkdir(parents=True, exist_ok=True)
            if self.master_key_path.is_file():
                key = self.master_key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                self.master_key_path.write_bytes(key)
            return Fernet(key)
        except (OSError, ValueError):
            raise SettingsStorageError("settings_key_unavailable") from None

    def _connect(self, writable: bool = False) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            return connection
        except (OSError, sqlite3.DatabaseError):
            raise SettingsStorageError("settings_database_unavailable") from None

    def initialize(self) -> None:
        with closing(self._connect(writable=True)):
            pass

    def read_all(self, sensitive_keys: set[str]) -> dict[str, Any]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT key, value, sensitive FROM app_settings ORDER BY key"
                ).fetchall()
            fernet = self._fernet()
            result: dict[str, Any] = {}
            for key, value, sensitive in rows:
                if int(sensitive):
                    if key not in sensitive_keys:
                        continue
                    try:
                        decoded = fernet.decrypt(value.encode("ascii")).decode("utf-8")
                    except (InvalidToken, UnicodeError, ValueError):
                        raise SettingsStorageError("settings_ciphertext_invalid") from None
                    result[key] = json.loads(decoded)
                else:
                    result[key] = json.loads(value)
            return result
        except SettingsStorageError:
            raise
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError):
            raise SettingsStorageError("settings_database_invalid") from None

    def read_configured(self) -> dict[str, bool]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute("SELECT key, value, sensitive FROM app_settings").fetchall()
            return {
                key: bool(value)
                for key, value, sensitive in rows
                if int(sensitive) == 0 or bool(value)
            }
        except (OSError, sqlite3.DatabaseError):
            raise SettingsStorageError("settings_database_invalid") from None

    def write_many(self, values: dict[str, Any], sensitive_keys: set[str], now: str) -> list[str]:
        fernet = self._fernet()

        def encode(key: str, value: Any) -> tuple[str, int]:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if key in sensitive_keys:
                return fernet.encrypt(serialized.encode("utf-8")).decode("ascii"), 1
            return serialized, 0

        try:
            with closing(self._connect(writable=True)) as connection:
                changed: list[str] = []
                for key, value in values.items():
                    if value is None or (value == "" and key not in sensitive_keys):
                        connection.execute("DELETE FROM app_settings WHERE key = ?", (key,))
                    else:
                        stored, sensitive = encode(key, value)
                        connection.execute(
                            """
                            INSERT INTO app_settings(key, value, sensitive, updated_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(key) DO UPDATE SET
                                value=excluded.value,
                                sensitive=excluded.sensitive,
                                updated_at=excluded.updated_at
                            """,
                            (key, stored, sensitive, now),
                        )
                    changed.append(key)
                connection.commit()
                return changed
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            raise SettingsStorageError("settings_write_failed") from None
