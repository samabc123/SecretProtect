"""Local accounts and revocable server-side sessions; never store passwords."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
USERNAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{2,31}\Z")


class AuthError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> None:
    if not 6 <= len(password) <= 128:
        raise AuthError("密码长度须为 6 至 128 个字符。")


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._dummy_hash = ""

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    needs_password_change INTEGER NOT NULL DEFAULT 0,
                    password_version INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    bucket TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_attempts_bucket
                    ON auth_attempts(bucket, created_at);
                CREATE TABLE IF NOT EXISTS auth_audit (
                    id INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            if "password_version" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(auth_users)")
            }:
                connection.execute(
                    "ALTER TABLE auth_users ADD COLUMN password_version INTEGER NOT NULL DEFAULT 0"
                )
            if "role" not in {row["name"] for row in connection.execute("PRAGMA table_info(auth_users)")}:
                connection.execute("ALTER TABLE auth_users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
                connection.execute("UPDATE auth_users SET role='admin' WHERE username='admin'")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(auth_users)")}
            if "is_superadmin" not in columns:
                connection.execute("ALTER TABLE auth_users ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE auth_users SET is_superadmin=1 WHERE username='admin' AND role='admin'")
            if "is_active" not in columns:
                connection.execute("ALTER TABLE auth_users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            if not connection.execute(
                "SELECT 1 FROM auth_users WHERE username='admin'"
            ).fetchone():
                connection.execute(
                    """INSERT OR IGNORE INTO auth_users
                       (username,password_hash,needs_password_change,created_at,role,is_superadmin)
                       VALUES ('admin',?,1,?,'admin',1)""",
                    (password_hash("123456"), time.time()),
                )
        self._dummy_hash = password_hash(secrets.token_urlsafe(24))

    @staticmethod
    def _audit(connection: sqlite3.Connection, action: str, username: str, outcome: str) -> None:
        connection.execute(
            "INSERT INTO auth_audit(action,username,outcome,created_at) VALUES (?,?,?,?)",
            (action, username, outcome, time.time()),
        )

    def _rate_limit(self, client: str, action: str, limit: int = 8) -> None:
        bucket = hashlib.sha256(f"{client}|{action}".encode()).hexdigest()
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM auth_attempts WHERE created_at<?", (now - 300,))
            count = connection.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE bucket=?", (bucket,)
            ).fetchone()[0]
            if count >= limit:
                raise AuthError("尝试次数过多，请等待 5 分钟后重试。", 429)
            connection.execute(
                "INSERT INTO auth_attempts(bucket,created_at) VALUES (?,?)", (bucket, now)
            )

    @staticmethod
    def _user_output(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "needs_password_change": bool(row["needs_password_change"]),
            "role": row["role"],
            "is_superadmin": bool(row["is_superadmin"]),
            "permission_mode": "超级管理员" if row["is_superadmin"] else "管理员",
            "account_version": row["password_version"],
        }

    def register(self, username: str, password: str, client: str) -> dict[str, Any]:
        raise AuthError("安全平台不开放注册或人员申请；管理员只能由超级管理员创建。", 403)

    def authenticate(self, username: str, password: str, client: str) -> dict[str, Any]:
        self._rate_limit(client, "login")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE username=?", (username.strip(),)
            ).fetchone()
        matched = password_matches(password, row["password_hash"] if row else self._dummy_hash)
        if not row or not matched or row["role"] != "admin" or not row["is_active"]:
            with self.connect() as connection:
                self._audit(connection, "login", "", "denied")
            raise AuthError("用户名或密码错误。", 401)
        with self.connect() as connection:
            self._audit(connection, "login", row["username"], "succeeded")
        return self._user_output(row)

    def create_session(self, user_id: int, expected_version: int | None = None) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT password_version,role,is_active FROM auth_users WHERE id=?", (user_id,)
            ).fetchone()
            if (not user or user["role"] != "admin" or not user["is_active"]
                    or (expected_version is not None and user["password_version"] != expected_version)):
                raise AuthError("账号密码已发生变化，请重新登录。", 401)
            connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            connection.execute(
                """INSERT INTO auth_sessions
                   (token_hash,user_id,csrf_token,expires_at,created_at) VALUES (?,?,?,?,?)""",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, now + SESSION_SECONDS, now),
            )
        return token, csrf

    def session(self, token: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token or ""):
            return None
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.*,s.csrf_token,s.expires_at
                   FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>? AND u.role='admin' AND u.is_active=1""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if not row:
            return None
        return {**self._user_output(row), "csrf_token": row["csrf_token"], "expires_at": row["expires_at"]}

    def logout(self, token: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.username FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                   WHERE s.token_hash=?""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            if row:
                self._audit(connection, "logout", row["username"], "succeeded")

    def change_password(self, user_id: int, current: str, new: str, client: str) -> dict[str, Any]:
        self._rate_limit(client, "password")
        validate_password(new)
        if hmac.compare_digest(current.encode("utf-8"), new.encode("utf-8")):
            raise AuthError("新密码不能与当前密码相同。")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
        if not row or not password_matches(current, row["password_hash"]):
            raise AuthError("当前密码不正确。", 400)
        encoded = password_hash(new)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE auth_users SET password_hash=?,needs_password_change=0,
                       password_version=password_version+1
                   WHERE id=? AND password_hash=?""",
                (encoded, user_id, row["password_hash"]),
            )
            if cursor.rowcount != 1:
                raise AuthError("账号密码已发生变化，请重新登录。", 409)
            connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self._audit(connection, "password.change", row["username"], "succeeded")
            updated = connection.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
            return self._user_output(updated)

    @staticmethod
    def _require_superadmin(connection, actor_id: int):
        actor = connection.execute("SELECT * FROM auth_users WHERE id=?", (actor_id,)).fetchone()
        if (not actor or actor["role"] != "admin" or not actor["is_superadmin"]
                or not actor["is_active"] or actor["needs_password_change"]):
            raise AuthError("仅已修改初始密码的超级管理员可以管理管理员账号。", 403)
        return actor

    def list_admins(self, actor_id: int) -> list[dict]:
        with self.connect() as connection:
            self._require_superadmin(connection, actor_id)
            return [{
                "id": row["id"], "username": row["username"],
                "is_superadmin": bool(row["is_superadmin"]), "is_active": bool(row["is_active"]),
                "needs_password_change": bool(row["needs_password_change"]), "created_at": row["created_at"],
            } for row in connection.execute("SELECT * FROM auth_users WHERE role='admin' ORDER BY id")]

    def create_admin(self, actor_id: int, username: str, password: str) -> dict:
        username = username.strip()
        if not USERNAME.fullmatch(username):
            raise AuthError("用户名须为 3 至 32 位英文字母、数字、下划线、点或连字符。")
        validate_password(password)
        with self.connect() as connection:
            self._require_superadmin(connection, actor_id)
        encoded = password_hash(password)
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                actor = self._require_superadmin(connection, actor_id)
                cursor = connection.execute(
                    "INSERT INTO auth_users(username,password_hash,needs_password_change,created_at,role,"
                    "is_superadmin,is_active) VALUES (?,?,1,?,'admin',0,1)",
                    (username, encoded, time.time()),
                )
                self._audit(connection, "admin.create", actor["username"], f"created:{cursor.lastrowid}")
                return {"id": cursor.lastrowid, "username": username, "needs_password_change": True}
        except sqlite3.IntegrityError:
            raise AuthError("用户名已存在，请更换。", 409) from None

    def set_admin_status(self, actor_id: int, target_id: int, is_active: bool) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = self._require_superadmin(connection, actor_id)
            target = connection.execute(
                "SELECT * FROM auth_users WHERE id=? AND role='admin'", (target_id,),
            ).fetchone()
            if not target:
                raise AuthError("管理员不存在。", 404)
            if target["is_superadmin"] or target_id == actor_id:
                raise AuthError("不能通过该接口停用或修改超级管理员。", 403)
            connection.execute("UPDATE auth_users SET is_active=?,password_version=password_version+1 WHERE id=?",
                               (int(is_active), target_id))
            connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (target_id,))
            self._audit(connection, "admin.status", actor["username"], f"target:{target_id};active:{int(is_active)}")
