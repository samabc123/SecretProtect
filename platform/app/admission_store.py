"""Single-platform admission. Applicant identities are not platform accounts."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .auth_store import AuthError, USERNAME, password_hash, password_matches


PLATFORM = {"id": "lab-pilot", "name": "实验室试点平台", "identity_method": "实验室管理员人工核验"}
SESSION_SECONDS = 8 * 60 * 60
INVITATION_SECONDS = 24 * 60 * 60


def clean_person(real_name: str, person_no: str, email: str = "") -> tuple[str, str, str]:
    name = unicodedata.normalize("NFC", real_name).strip()
    number = person_no.strip().upper()
    email = email.strip().lower()
    if not 2 <= len(name) <= 80 or any(unicodedata.category(c).startswith("C") for c in name):
        raise AuthError("请填写有效的真实姓名。")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{2,39}", number):
        raise AuthError("学号或工号须为 3 至 40 位字母、数字、点、下划线或连字符。")
    if email and (len(email) > 180 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise AuthError("请填写有效的联系邮箱。")
    return name, number, email


def check_password(password: str) -> None:
    if not 6 <= len(password) <= 128:
        raise AuthError("密码长度须为 6 至 128 个字符。")


def check_username(username: str) -> str:
    username = username.strip()
    if not USERNAME.fullmatch(username):
        raise AuthError("用户名须为 3 至 32 位英文字母、数字、下划线、点或连字符，首位须为字母、数字或下划线；不支持中文。")
    return username


def check_note(note: str, minimum: int = 8) -> str:
    note = note.strip()
    if not minimum <= len(note) <= 500:
        raise AuthError(f"请填写 {minimum} 至 500 字的核验或审批依据，不要填写证件号码或密码。")
    return note


class AdmissionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._dummy_hash = ""

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS lab_people (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT,
                    real_name TEXT NOT NULL,
                    person_no TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    identity_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(identity_status IN ('pending','verified','rejected')),
                    verification_note TEXT NOT NULL DEFAULT '',
                    verified_by TEXT NOT NULL DEFAULT '',
                    verified_at REAL,
                    consented_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_verified_lab_identity
                    ON lab_people(person_no) WHERE identity_status='verified';
                CREATE INDEX IF NOT EXISTS lab_people_number ON lab_people(person_no);
                CREATE TABLE IF NOT EXISTS lab_whitelist (
                    id INTEGER PRIMARY KEY,
                    person_no TEXT NOT NULL UNIQUE,
                    real_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                    expires_at REAL,
                    deleted_at REAL,
                    note TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_applications (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL REFERENCES lab_people(id),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected')),
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    reviewed_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_lab_application
                    ON lab_applications(person_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS lab_accounts (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL UNIQUE REFERENCES lab_people(id),
                    status TEXT NOT NULL
                        CHECK(status IN ('active','pending_activation','suspended')),
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_invitations (
                    token_hash TEXT PRIMARY KEY,
                    person_id INTEGER NOT NULL REFERENCES lab_people(id),
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_sessions (
                    token_hash TEXT PRIMARY KEY,
                    person_id INTEGER NOT NULL REFERENCES lab_people(id),
                    csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_attempts (
                    bucket TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lab_attempts_bucket ON lab_attempts(bucket,created_at);
                CREATE TABLE IF NOT EXISTS lab_admission_audit (
                    id INTEGER PRIMARY KEY,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("BEGIN IMMEDIATE")
            if "deleted_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(lab_whitelist)")}:
                conn.execute("ALTER TABLE lab_whitelist ADD COLUMN deleted_at REAL")
        self._dummy_hash = password_hash(secrets.token_urlsafe(24))

    @staticmethod
    def _audit(conn, actor: str, action: str, subject: Any, reason: str = "", decision: str = "allowed"):
        conn.execute(
            "INSERT INTO lab_admission_audit(actor,action,subject,decision,reason,created_at) VALUES (?,?,?,?,?,?)",
            (actor, action, str(subject), decision, reason[:500], time.time()),
        )

    def audit_denial(self, actor: str, action: str, reason: str):
        with self.connect() as conn:
            self._audit(conn, actor, action, "", reason, "denied")

    def rate_limit(self, client: str, action: str, limit: int = 20):
        bucket = hashlib.sha256(f"{client}|{action}".encode()).hexdigest()
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM lab_attempts WHERE created_at<?", (now - 300,))
            count = conn.execute("SELECT COUNT(*) FROM lab_attempts WHERE bucket=?", (bucket,)).fetchone()[0]
            if count >= limit:
                raise AuthError("操作过于频繁，请等待 5 分钟后重试。", 429)
            conn.execute("INSERT INTO lab_attempts VALUES (?,?)", (bucket, now))

    @staticmethod
    def _person(conn, person_id: int):
        row = conn.execute("SELECT * FROM lab_people WHERE id=?", (person_id,)).fetchone()
        if not row:
            raise AuthError("申请身份不存在。", 404)
        return row

    @staticmethod
    def _eligibility(conn, person) -> dict:
        white = conn.execute("SELECT * FROM lab_whitelist WHERE person_no=?", (person["person_no"],)).fetchone()
        matched = bool(white and white["real_name"] == person["real_name"])
        allowed = bool(matched and white["deleted_at"] is None and white["status"] == "active"
                       and (white["expires_at"] is None or white["expires_at"] > time.time()))
        verified = person["identity_status"] == "verified"
        reason = "eligible" if verified and allowed else ("identity_not_verified" if not verified else "not_allowlisted")
        return {"identity_verified": verified, "allowlisted": allowed,
                "can_register": verified and allowed, "can_reapply": bool(verified and matched and not allowed),
                "reason": reason}

    def _require_eligible(self, conn, person):
        eligibility = self._eligibility(conn, person)
        if not eligibility["identity_verified"]:
            raise AuthError("身份尚未通过管理员核验，不能开通实验室账号。", 403)
        if not eligibility["allowlisted"]:
            raise AuthError("姓名与学号/工号未匹配有效白名单，已拒绝开通；请联系管理员核对准入状态。", 403)

    def profile(self, person_id: int) -> dict:
        with self.connect() as conn:
            person = self._person(conn, person_id)
            account = conn.execute(
                "SELECT id,status,created_at,created_by FROM lab_accounts WHERE person_id=?", (person_id,)
            ).fetchone()
            application = conn.execute(
                "SELECT id,status,reason,review_note,created_at,reviewed_at FROM lab_applications "
                "WHERE person_id=? ORDER BY id DESC LIMIT 1", (person_id,)
            ).fetchone()
            return {
                "id": person["id"], "username": person["username"], "real_name": person["real_name"],
                "person_no": person["person_no"], "email": person["email"],
                "identity_status": person["identity_status"], "verified_at": person["verified_at"],
                "eligibility": self._eligibility(conn, person),
                "account": dict(account) if account else None,
                "application": dict(application) if application else None,
            }

    def signup(self, username: str, password: str, real_name: str, person_no: str, email: str, consent: bool) -> int:
        if not consent:
            raise AuthError("请先确认个人信息核验用途。")
        username = check_username(username)
        check_password(password)
        name, number, email = clean_person(real_name, person_no, email)
        if not email:
            raise AuthError("请填写联系邮箱。")
        encoded = password_hash(password)
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "INSERT INTO lab_people(username,password_hash,real_name,person_no,email,consented_at,created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (username, encoded, name, number, email, time.time(), time.time()),
                )
                self._audit(conn, f"applicant:{cursor.lastrowid}", "identity.submit", cursor.lastrowid)
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            raise AuthError("该申请信息已登记，请登录原申请账号或联系管理员核对。", 409) from None

    def authenticate(self, username: str, password: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM lab_people WHERE username=?", (username.strip(),)).fetchone()
        if not password_matches(password, (row["password_hash"] if row else None) or self._dummy_hash):
            raise AuthError("用户名或密码错误；代注册账号须先激活。", 401)
        with self.connect() as conn:
            self._audit(conn, f"applicant:{row['id']}", "portal.login", row["id"])
        return row["id"]

    def create_session(self, person_id: int) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as conn:
            self._person(conn, person_id)
            conn.execute("DELETE FROM lab_sessions WHERE expires_at<=?", (time.time(),))
            conn.execute("INSERT INTO lab_sessions VALUES (?,?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), person_id, csrf, time.time() + SESSION_SECONDS))
        return token, csrf

    def session(self, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token or ""):
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT person_id,csrf_token FROM lab_sessions WHERE token_hash=? AND expires_at>?",
                               (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token: str):
        with self.connect() as conn:
            conn.execute("DELETE FROM lab_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def apply(self, person_id: int, reason: str) -> int:
        reason = check_note(reason)
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                person = self._person(conn, person_id)
                eligibility = self._eligibility(conn, person)
                if eligibility["allowlisted"]:
                    raise AuthError("已在有效白名单内，无需重复申请。", 409)
                if not eligibility["can_reapply"]:
                    raise AuthError("首次身份核验申请已提交，无需再申请入名单；请等待核验结果或联系管理员。", 409)
                cursor = conn.execute("INSERT INTO lab_applications(person_id,reason,created_at) VALUES (?,?,?)",
                                      (person_id, reason, time.time()))
                self._audit(conn, f"applicant:{person_id}", "whitelist.apply", cursor.lastrowid)
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            raise AuthError("已有待审核的入名单申请，请等待处理。", 409) from None

    def verify_identity(self, person_id: int, decision: str, actor: str, verified_in_person: bool = False):
        if decision not in {"verified", "rejected"}:
            raise AuthError("核验决定无效。")
        if decision == "verified" and verified_in_person is not True:
            raise AuthError("请确认已当面核对本人校园身份证明及申请账号归属。")
        note = ("同意：管理员确认已当面核对本人校园身份证明及申请账号归属。"
                if decision == "verified" else "不同意：管理员未通过本次身份核验。")
        approval_note = "管理员当面核验身份及申请账号归属通过，同意加入实验室白名单。"
        whitelist_added = False
        application_approved = False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            person = self._person(conn, person_id)
            if decision == "verified" and conn.execute(
                "SELECT 1 FROM lab_people WHERE person_no=? AND identity_status='verified' AND id<>?",
                (person["person_no"], person_id),
            ).fetchone():
                raise AuthError("该学号/工号已有核验通过的身份，不能重复绑定账号。", 409)
            if decision == "verified":
                white = conn.execute("SELECT * FROM lab_whitelist WHERE person_no=?",
                                     (person["person_no"],)).fetchone()
                if white and white["real_name"] != person["real_name"]:
                    raise AuthError("该学号/工号与白名单姓名冲突，请先核对并更正白名单。", 409)
                if white and white["deleted_at"] is not None:
                    raise AuthError("该人员已从白名单删除，请由管理员明确重新添加或批准新的入名单申请。", 409)
                if white and (white["status"] != "active" or (
                        white["expires_at"] is not None and white["expires_at"] <= time.time())):
                    raise AuthError("该人员白名单已撤销或到期，请先在白名单中明确恢复或续期，再核验。", 409)
                if not white:
                    self._allow(conn, person["real_name"], person["person_no"], None, approval_note, actor)
                    self._audit(conn, actor, "whitelist.allow", person["person_no"], approval_note)
                    whitelist_added = True
                application = conn.execute(
                    "SELECT id FROM lab_applications WHERE person_id=? AND status='pending'", (person_id,)
                ).fetchone()
                if application:
                    conn.execute(
                        "UPDATE lab_applications SET status='approved',review_note=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                        (approval_note, actor, time.time(), application["id"]),
                    )
                    self._audit(conn, actor, "application.approved", application["id"], approval_note)
                    application_approved = True
            conn.execute("UPDATE lab_people SET identity_status=?,verification_note=?,verified_by=?,verified_at=? WHERE id=?",
                         (decision, note, actor, time.time(), person_id))
            if decision == "rejected":
                conn.execute("UPDATE lab_accounts SET status='suspended',updated_at=? WHERE person_id=?",
                             (time.time(), person_id))
                conn.execute("UPDATE lab_invitations SET used_at=? WHERE person_id=? AND used_at IS NULL",
                             (time.time(), person_id))
            self._audit(conn, actor, f"identity.{decision}", person_id, note)
        return {"whitelist_added": whitelist_added, "application_approved": application_approved}

    @staticmethod
    def _expiry(expires_at):
        if expires_at is not None and (not isinstance(expires_at, (int, float))
                                       or not math.isfinite(expires_at) or expires_at <= time.time()):
            raise AuthError("白名单有效期必须晚于当前时间。")
        return expires_at

    def _allow(self, conn, real_name: str, person_no: str, expires_at, note: str, actor: str):
        conn.execute(
            "INSERT INTO lab_whitelist(person_no,real_name,status,expires_at,note,updated_by,updated_at) "
            "VALUES (?,?,'active',?,?,?,?) ON CONFLICT(person_no) DO UPDATE SET "
            "real_name=excluded.real_name,status='active',expires_at=excluded.expires_at,"
            "note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at,deleted_at=NULL",
            (person_no, real_name, expires_at, note, actor, time.time()),
        )

    def add_whitelist(self, real_name: str, person_no: str, expires_at, note: str, actor: str):
        name, number, _ = clean_person(real_name, person_no)
        note = check_note(note, minimum=5)
        expires_at = self._expiry(expires_at)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT real_name FROM lab_people WHERE person_no=? AND identity_status='verified'",
                                    (number,)).fetchone()
            if existing and existing["real_name"] != name:
                raise AuthError("该学号/工号与已核验姓名冲突，请先核对人员信息。", 409)
            self._allow(conn, name, number, expires_at, note, actor)
            self._audit(conn, actor, "whitelist.allow", number, note)

    def revoke_whitelist(self, whitelist_id: int, note: str, actor: str):
        note = check_note(note)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT person_no FROM lab_whitelist WHERE id=? AND deleted_at IS NULL",
                               (whitelist_id,)).fetchone()
            if not row:
                raise AuthError("白名单记录不存在。", 404)
            conn.execute("UPDATE lab_whitelist SET status='revoked',note=?,updated_by=?,updated_at=? WHERE id=?",
                         (note, actor, time.time(), whitelist_id))
            conn.execute("UPDATE lab_accounts SET status='suspended',updated_at=? WHERE person_id IN "
                         "(SELECT id FROM lab_people WHERE person_no=?)", (time.time(), row["person_no"]))
            conn.execute("UPDATE lab_invitations SET used_at=? WHERE used_at IS NULL AND person_id IN "
                         "(SELECT id FROM lab_people WHERE person_no=?)", (time.time(), row["person_no"]))
            self._audit(conn, actor, "whitelist.revoke", whitelist_id, note)

    def delete_whitelist(self, whitelist_id: int, note: str, actor: str, confirmed: bool = False):
        if confirmed is not True:
            raise AuthError("请确认删除该人员白名单并停止其平台访问。")
        note = check_note(note, minimum=5)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM lab_whitelist WHERE id=?", (whitelist_id,)).fetchone()
            if not row:
                raise AuthError("白名单记录不存在。", 404)
            if row["deleted_at"] is not None:
                raise AuthError("该人员已从白名单删除，请刷新列表。", 409)
            now = time.time()
            conn.execute(
                "UPDATE lab_whitelist SET status='revoked',deleted_at=?,note=?,updated_by=?,updated_at=? WHERE id=?",
                (now, note, actor, now, whitelist_id),
            )
            conn.execute("UPDATE lab_accounts SET status='suspended',updated_at=? WHERE person_id IN "
                         "(SELECT id FROM lab_people WHERE person_no=?)", (now, row["person_no"]))
            conn.execute("UPDATE lab_invitations SET used_at=? WHERE used_at IS NULL AND person_id IN "
                         "(SELECT id FROM lab_people WHERE person_no=?)", (now, row["person_no"]))
            pending = conn.execute(
                "SELECT id FROM lab_applications WHERE status='pending' AND person_id IN "
                "(SELECT id FROM lab_people WHERE person_no=?)", (row["person_no"],)
            ).fetchall()
            for application in pending:
                review_note = f"管理员删除白名单，同步拒绝原待审核申请。原因：{note}"[:500]
                conn.execute(
                    "UPDATE lab_applications SET status='rejected',review_note=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                    (review_note, actor, now, application["id"]),
                )
                self._audit(conn, actor, "application.rejected", application["id"], review_note)
            self._audit(conn, actor, "whitelist.delete", whitelist_id, note)

    def review_application(self, application_id: int, decision: str, note: str, expires_at, actor: str):
        if decision not in {"approved", "rejected"}:
            raise AuthError("审批决定无效。")
        note = check_note(note)
        expires_at = self._expiry(expires_at)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            application = conn.execute("SELECT * FROM lab_applications WHERE id=?", (application_id,)).fetchone()
            if not application:
                raise AuthError("申请不存在。", 404)
            if application["status"] != "pending":
                raise AuthError("该申请已处理，请刷新列表。", 409)
            person = self._person(conn, application["person_id"])
            if decision == "approved":
                if person["identity_status"] != "verified":
                    raise AuthError("须先完成人工身份核验，再批准加入白名单。", 403)
                self._allow(conn, person["real_name"], person["person_no"], expires_at, note, actor)
            conn.execute("UPDATE lab_applications SET status=?,review_note=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                         (decision, note, actor, time.time(), application_id))
            self._audit(conn, actor, f"application.{decision}", application_id, note)

    def _provision(self, conn, person_id: int, status: str, actor: str):
        now = time.time()
        conn.execute(
            "INSERT INTO lab_accounts(person_id,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(person_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",
            (person_id, status, actor, now, now),
        )

    def self_register(self, person_id: int):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            person = self._person(conn, person_id)
            self._require_eligible(conn, person)
            if not person["password_hash"]:
                raise AuthError("请先完成账号激活。", 403)
            account = conn.execute("SELECT status FROM lab_accounts WHERE person_id=?", (person_id,)).fetchone()
            if account and account["status"] == "active":
                raise AuthError("实验室账号已开通，无需重复注册。", 409)
            self._provision(conn, person_id, "active", f"applicant:{person_id}")
            self._audit(conn, f"applicant:{person_id}", "account.self_register", person_id)

    def managed_register(self, username: str, real_name: str, person_no: str, email: str,
                         verified_in_person: bool, note: str, actor: str) -> dict:
        username = check_username(username)
        name, number, email = clean_person(real_name, person_no, email)
        note = check_note(note)
        if not verified_in_person or not email:
            raise AuthError("须填写联系邮箱，并确认已当面核验本人及申请账号。")
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                white = conn.execute("SELECT * FROM lab_whitelist WHERE person_no=?", (number,)).fetchone()
                if not white or white["deleted_at"] is not None or white["real_name"] != name or white["status"] != "active" or (
                        white["expires_at"] is not None and white["expires_at"] <= time.time()):
                    raise AuthError("管理员代注册同样要求姓名和学号/工号匹配有效白名单。", 403)
                person = conn.execute("SELECT * FROM lab_people WHERE username=?", (username,)).fetchone()
                verified = conn.execute(
                    "SELECT id FROM lab_people WHERE person_no=? AND identity_status='verified'", (number,)
                ).fetchone()
                if verified and (not person or verified["id"] != person["id"]):
                    raise AuthError("该学号/工号已有核验账号，不能覆盖本人账号。", 409)
                if person:
                    if person["person_no"] != number or person["real_name"] != name or person["email"] != email:
                        raise AuthError("与已登记的申请身份不一致，请核对；不能覆盖他人的账号。", 409)
                    if person["identity_status"] != "verified":
                        raise AuthError("该人员已有申请，请先在身份核验列表审核本人身份。", 403)
                    person_id = person["id"]
                else:
                    cursor = conn.execute(
                        "INSERT INTO lab_people(username,real_name,person_no,email,identity_status,"
                        "verification_note,verified_by,verified_at,created_at) VALUES (?,?,?,?,'verified',?,?,?,?)",
                        (username, name, number, email, note, actor, time.time(), time.time()),
                    )
                    person_id = cursor.lastrowid
                    self._audit(conn, actor, "identity.verified", person_id, note)
                needs_activation = not (person and person["password_hash"])
                self._provision(conn, person_id, "pending_activation" if needs_activation else "active", actor)
                result = {"person_id": person_id, "requires_activation": needs_activation}
                if needs_activation:
                    token = secrets.token_urlsafe(32)
                    expires = time.time() + INVITATION_SECONDS
                    conn.execute("UPDATE lab_invitations SET used_at=? WHERE person_id=? AND used_at IS NULL",
                                 (time.time(), person_id))
                    conn.execute("INSERT INTO lab_invitations VALUES (?,?,?,NULL,?,?)",
                                 (hashlib.sha256(token.encode()).hexdigest(), person_id, expires, actor, time.time()))
                    result.update({"activation_token": token, "expires_at": expires})
                self._audit(conn, actor, "account.managed_register", person_id, note)
                return result
        except sqlite3.IntegrityError:
            raise AuthError("用户名已被其他人员使用，请更换用户名。", 409) from None

    def activate(self, token: str, password: str, consent: bool) -> int:
        check_password(password)
        if not consent:
            raise AuthError("请先确认个人信息核验用途。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise AuthError("激活邀请无效。", 400)
        encoded = password_hash(password)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invitation = conn.execute("SELECT * FROM lab_invitations WHERE token_hash=?",
                                      (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not invitation or invitation["used_at"] is not None:
                raise AuthError("激活邀请无效或已使用，请联系管理员重新发出。", 409)
            if invitation["expires_at"] <= time.time():
                raise AuthError("激活邀请已过期，请联系管理员重新发出。", 410)
            person = self._person(conn, invitation["person_id"])
            self._require_eligible(conn, person)
            if person["password_hash"]:
                raise AuthError("该账号已激活，请直接登录。", 409)
            conn.execute("UPDATE lab_people SET password_hash=?,consented_at=? WHERE id=?",
                         (encoded, time.time(), person["id"]))
            conn.execute("UPDATE lab_invitations SET used_at=? WHERE person_id=? AND used_at IS NULL",
                         (time.time(), person["id"]))
            self._provision(conn, person["id"], "active", invitation["created_by"])
            self._audit(conn, f"applicant:{person['id']}", "account.activate", person["id"])
            return person["id"]

    def workspace(self, person_id: int) -> dict:
        with self.connect() as conn:
            person = self._person(conn, person_id)
            self._require_eligible(conn, person)
            account = conn.execute("SELECT id,status FROM lab_accounts WHERE person_id=?", (person_id,)).fetchone()
            if not account or account["status"] != "active":
                raise AuthError("实验室账号尚未开通、待激活或已停用。", 403)
            return {"platform": PLATFORM, "account_id": account["id"], "username": person["username"],
                    "real_name": person["real_name"], "person_no": person["person_no"],
                    "permissions": ["profile.read"], "checked_at": time.time()}

    def dashboard(self) -> dict:
        with self.connect() as conn:
            people = []
            for row in conn.execute(
                "SELECT p.id,p.username,p.real_name,p.person_no,p.email,p.identity_status,p.verified_by,"
                "p.verified_at,p.verification_note,p.created_at,a.status AS account_status "
                "FROM lab_people p LEFT JOIN lab_accounts a ON a.person_id=p.id ORDER BY p.id DESC LIMIT 500"
            ):
                item = dict(row)
                item["eligibility"] = self._eligibility(conn, row)
                people.append(item)
            whitelist = [dict(row) for row in conn.execute(
                "SELECT * FROM lab_whitelist WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500"
            )]
            applications = [dict(row) for row in conn.execute(
                "SELECT a.*,p.username,p.real_name,p.person_no,p.identity_status FROM lab_applications a "
                "JOIN lab_people p ON p.id=a.person_id ORDER BY a.id DESC LIMIT 500"
            )]
            audits = [dict(row) for row in conn.execute("SELECT * FROM lab_admission_audit ORDER BY id DESC LIMIT 100")]
        return {"platform": PLATFORM, "people": people, "whitelist": whitelist,
                "applications": applications, "audits": audits, "list_limit": 500}
