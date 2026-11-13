"""Single-platform registration decisions; passwords stay with Samlab."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .auth_store import AuthError, USERNAME


PLATFORM = {"id": "samlab", "name": "Samlab", "url": "https://researchchat.samlab.cn/"}
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
MAX_BULK_MEMBERS = 500


class SamlabAdmissionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

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

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS samlab_members (
                    id TEXT PRIMARY KEY,
                    person_no TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                    deleted_at REAL,
                    expires_at REAL,
                    policy_version INTEGER NOT NULL DEFAULT 1,
                    account_id TEXT UNIQUE,
                    sync_state TEXT NOT NULL DEFAULT 'pending_create'
                        CHECK(sync_state IN ('pending_create','active','pending_revoke','disabled')),
                    synced_version INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samlab_requests (
                    request_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE,
                    person_no TEXT NOT NULL,
                    email TEXT NOT NULL COLLATE NOCASE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected')),
                    member_id TEXT REFERENCES samlab_members(id),
                    submitted_at REAL NOT NULL,
                    reviewed_at REAL,
                    reviewed_by TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS samlab_pending_person
                    ON samlab_requests(person_no) WHERE status='pending';
                CREATE UNIQUE INDEX IF NOT EXISTS samlab_pending_username
                    ON samlab_requests(username) WHERE status='pending';
                CREATE UNIQUE INDEX IF NOT EXISTS samlab_pending_email
                    ON samlab_requests(email) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS samlab_service_nonces (
                    nonce TEXT PRIMARY KEY, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samlab_audit (
                    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                    subject TEXT NOT NULL, created_at REAL NOT NULL, note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS samlab_service_health (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_seen REAL NOT NULL
                );
            """)
            for table in ("samlab_members", "samlab_requests"):
                columns = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
                if "source" not in columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'self'")
                if "display_username" not in columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN display_username TEXT")
            from .roster_import import initialize
            initialize(c)
            self._migrate_permanent(c)

    def _migrate_permanent(self, c):
        # Keep legacy columns for backup compatibility, but never restore expired or removed access.
        c.execute("BEGIN IMMEDIATE")
        c.execute("CREATE TABLE IF NOT EXISTS samlab_schema_migrations "
                  "(id TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
        migration = "permanent-membership-v1"
        if c.execute("SELECT 1 FROM samlab_schema_migrations WHERE id=?", (migration,)).fetchone():
            return
        now = time.time()
        for row in c.execute("SELECT * FROM samlab_members WHERE deleted_at IS NULL").fetchall():
            if row["status"] == "revoked" or (row["expires_at"] is not None and row["expires_at"] <= now):
                c.execute("UPDATE samlab_members SET status='revoked',deleted_at=?,expires_at=NULL,"
                          "policy_version=policy_version+1,sync_state='pending_revoke',updated_at=? WHERE id=?",
                          (now, now, row["id"]))
                self._audit(c, "system:migration", "whitelist.legacy_deleted", row["id"],
                            "合并历史撤销、到期与删除状态，未恢复准入。")
            elif row["expires_at"] is not None:
                c.execute("UPDATE samlab_members SET expires_at=NULL,policy_version=policy_version+1,"
                          "sync_state='pending_create',updated_at=? WHERE id=?", (now, row["id"]))
                self._audit(c, "system:migration", "whitelist.permanent_enabled", row["id"],
                            "有效人员改为永久白名单；原有效期 " + str(row["expires_at"]))
        c.execute("UPDATE samlab_members SET expires_at=NULL WHERE expires_at IS NOT NULL")
        c.execute("INSERT INTO samlab_schema_migrations VALUES (?,?)", (migration, now))

    @staticmethod
    def _audit(c, actor, action, subject, note=""):
        c.execute("INSERT INTO samlab_audit(actor,action,subject,created_at,note) VALUES (?,?,?,?,?)",
                  (actor, action, subject, time.time(), note))

    def consume_nonce(self, nonce: str):
        try:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("DELETE FROM samlab_service_nonces WHERE created_at<?", (time.time() - 600,))
                c.execute("INSERT INTO samlab_service_nonces VALUES (?,?)", (nonce, time.time()))
                c.execute("INSERT INTO samlab_service_health VALUES (1,?) ON CONFLICT(id) "
                          "DO UPDATE SET last_seen=excluded.last_seen", (time.time(),))
        except sqlite3.IntegrityError:
            raise AuthError("服务请求已处理，请使用新的签名随机数重试。", 409) from None

    def submit(self, request_id: str, username: str, person_no: str | None = None, email: str | None = None):
        username, email = username.strip(), (email or "").strip().lower()
        if not REQUEST_ID.fullmatch(request_id):
            raise AuthError("申请编号须为 1 至 100 位字母、数字、下划线或连字符。")
        if not USERNAME.fullmatch(username):
            raise AuthError("用户名须为 3 至 32 位英文字母、数字、下划线、点或连字符。")
        # Retain old delivery compatibility; new people are identified by stable IDs, not student numbers.
        person_no = person_no if person_no and re.fullmatch(r"[0-9]{10}", person_no) else f"request:{request_id}"
        if not 3 <= len(email) <= 180 or not EMAIL.fullmatch(email) or ".." in email:
            raise AuthError("邮箱必填，请填写有效邮箱地址。")
        try:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                existing = c.execute("SELECT * FROM samlab_requests WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    if existing["source"] != "self" or (existing["username"], existing["email"]) != (username, email):
                        raise AuthError("申请编号已绑定其他申请内容。", 409)
                    return self._status(c, request_id)
                c.execute("INSERT INTO samlab_requests(request_id,username,person_no,email,submitted_at) "
                          "VALUES (?,?,?,?,?)", (request_id, username, person_no, email, time.time()))
                self._audit(c, "service:samlab", "application.received", request_id)
                return self._status(c, request_id)
        except sqlite3.IntegrityError:
            raise AuthError("相同用户名或邮箱已有待审核申请。", 409) from None

    def admin_provision(self, request_id: str, username: str, person_no: str, email: str,
                        account_id: str, actor_id: int, source: str, display_username: str):
        email = email.strip().lower()
        if not REQUEST_ID.fullmatch(request_id) or not USERNAME.fullmatch(username):
            raise AuthError("平台准入标识无效。")
        if (source not in {"admin_direct", "admin_invite"} or type(actor_id) is not int or actor_id <= 0
                or not re.fullmatch(r"[1-9][0-9]{0,18}", account_id)):
            raise AuthError("管理员创建来源或账号映射无效。")
        if source == "admin_direct":
            if person_no != f"account:{account_id}" or username != f"samlab_u{account_id}":
                raise AuthError("直接创建须使用稳定的平台账号映射。")
        elif person_no != f"account:{account_id}" and not re.fullmatch(r"[0-9]{10}", person_no):
            raise AuthError("邀请账号映射无效。")
        if not 3 <= len(email) <= 255 or not EMAIL.fullmatch(email) or ".." in email:
            raise AuthError("邮箱必填，请填写有效邮箱地址。")
        if len(display_username) > 100:
            raise AuthError("用户名过长。")
        actor = f"samlab-admin:{actor_id}"
        try:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                existing = c.execute("SELECT * FROM samlab_requests WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    member = c.execute("SELECT account_id FROM samlab_members WHERE id=?", (existing["member_id"],)).fetchone()
                    if (existing["source"], existing["username"], existing["person_no"], existing["email"],
                            existing["reviewed_by"]) != (source, username, person_no, email, actor) or not member or member["account_id"] != account_id:
                        raise AuthError("该记录已绑定其他管理员创建内容。", 409)
                    # Delivery retries cannot undo a later revocation or logical deletion.
                    return self._status(c, request_id)
                now, member_id = time.time(), str(uuid.uuid4())
                imported = c.execute("SELECT * FROM samlab_members WHERE email=?", (email,)).fetchone()
                if imported:
                    if (imported["source"] not in {"roster", "manual"} or imported["account_id"] not in (None, account_id)
                            or imported["status"] != "active" or imported["deleted_at"] is not None
                            or c.execute("SELECT 1 FROM samlab_requests WHERE member_id=?", (imported["id"],)).fetchone()):
                        raise AuthError("邮箱已有绑定或准入已失效，未覆盖原策略。", 409)
                    member_id = imported["id"]
                    c.execute("UPDATE samlab_members SET username=?,display_username=?,account_id=?,updated_at=? WHERE id=?",
                              (username, display_username, account_id, now, member_id))
                else:
                    c.execute("INSERT INTO samlab_members(id,person_no,username,email,status,account_id,"
                              "created_at,updated_at,source,display_username) VALUES (?,?,?,?,'active',?,?,?,?,?)",
                              (member_id, person_no, username, email, account_id, now, now, source, display_username))
                c.execute("INSERT INTO samlab_requests(request_id,username,person_no,email,status,member_id,"
                          "submitted_at,reviewed_at,reviewed_by,source,display_username) "
                          "VALUES (?,?,?,?,'approved',?,?,?,?,?,?)",
                          (request_id, username, person_no, email, member_id, now, now, actor, source, display_username))
                self._audit(c, actor, "whitelist.admin_provisioned", member_id,
                            "Samlab 管理员直接授权，无需另行审批；不代表自动实名认证。")
                self._audit(c, actor, "account.admin_created", request_id)
                return self._status(c, request_id)
        except sqlite3.IntegrityError:
            raise AuthError("人员或账号映射已存在，未覆盖原准入策略。", 409) from None

    @staticmethod
    def _status(c, request_id: str):
        request = c.execute("SELECT * FROM samlab_requests WHERE request_id=?", (request_id,)).fetchone()
        if not request:
            raise AuthError("申请不存在。", 404)
        member = c.execute("SELECT * FROM samlab_members WHERE id=?", (request["member_id"],)).fetchone()
        allowed = bool(request["status"] == "approved" and member and member["status"] == "active"
                       and member["deleted_at"] is None)
        return {
            "request_id": request_id, "decision": request["status"], "allowed": allowed,
            "member_id": member["id"] if member else None,
            "policy_version": member["policy_version"] if member else 0,
            "account_id": member["account_id"] if member else None,
            "account_state": member["sync_state"] if member else "not_created",
            "expires_at": None,
            # A worker must refresh this lease; a cached approval is not permanent authority.
            "valid_until": time.time() + 15 if allowed else 0,
        }

    def status(self, request_id):
        with self.connect() as c:
            return self._status(c, request_id)

    def review(self, request_id: str, decision: str, actor: str):
        if decision not in {"approved", "rejected"}:
            raise AuthError("请选择同意或不同意。")
        try:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM samlab_requests WHERE request_id=?", (request_id,)).fetchone()
                if not row:
                    raise AuthError("申请不存在。", 404)
                if row["status"] != "pending":
                    if row["status"] == decision:
                        return self._status(c, request_id)
                    raise AuthError("该申请已处理，不能覆盖原审批。", 409)
                member_id = None
                if decision == "approved":
                    existing = c.execute("SELECT * FROM samlab_members WHERE email=?", (row["email"],)).fetchone()
                    if existing:
                        member_id = existing["id"]
                        if existing["source"] in {"roster", "manual"}:
                            if existing["account_id"] is not None or c.execute(
                                    "SELECT 1 FROM samlab_requests WHERE member_id=?", (existing["id"],)).fetchone():
                                raise AuthError("该邮箱已绑定账号，请使用原账号或由管理员核实迁移。", 409)
                            if existing["status"] != "active" or existing["deleted_at"] is not None:
                                raise AuthError("导入人员已删除，请先核实准入，不能直接恢复。", 409)
                            c.execute("UPDATE samlab_members SET username=?,display_username=?,expires_at=NULL,"
                                      "policy_version=policy_version+1,sync_state='pending_create',updated_at=? WHERE id=?",
                                      (row["username"], row["username"], time.time(), member_id))
                        else:
                            if existing["username"].lower() != row["username"].lower():
                                raise AuthError("邮箱已关联其他用户名，请先核实人员绑定。", 409)
                            if existing["status"] == "active" and existing["deleted_at"] is None:
                                raise AuthError("该人员已在白名单内，请使用原账号。", 409)
                            c.execute("UPDATE samlab_members SET status='active',deleted_at=NULL,expires_at=NULL,"
                                      "policy_version=policy_version+1,sync_state='pending_create',updated_at=? WHERE id=?",
                                      (time.time(), member_id))
                    else:
                        member_id = str(uuid.uuid4())
                        c.execute("INSERT INTO samlab_members(id,person_no,username,email,status,created_at,updated_at)"
                                  " VALUES (?,?,?,?,'active',?,?)",
                                  (member_id, row["person_no"], row["username"], row["email"], time.time(), time.time()))
                    self._audit(c, actor, "whitelist.approved", member_id, "管理员批准 Samlab 注册准入；不等于实名认证。")
                c.execute("UPDATE samlab_requests SET status=?,member_id=?,reviewed_at=?,reviewed_by=? WHERE request_id=?",
                          (decision, member_id, time.time(), actor, request_id))
                self._audit(c, actor, f"application.{decision}", request_id)
                return self._status(c, request_id)
        except sqlite3.IntegrityError:
            raise AuthError("用户名或邮箱已绑定其他人员，批准未生效。", 409) from None

    def delete_members(self, member_ids: list[str], actor: str, note: str):
        if (not isinstance(member_ids, list) or not 1 <= len(member_ids) <= MAX_BULK_MEMBERS
                or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in member_ids)):
            raise AuthError(f"请选择 1 至 {MAX_BULK_MEMBERS} 名白名单人员。")
        if not 5 <= len(note.strip()) <= 500:
            raise AuthError("删除原因须为 5 至 500 个字符。")
        member_ids = list(dict.fromkeys(member_ids))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in member_ids)
            rows = c.execute(f"SELECT * FROM samlab_members WHERE id IN ({placeholders})", member_ids).fetchall()
            if len(rows) != len(member_ids):
                raise AuthError("部分白名单人员不存在，本次未删除任何人员，请刷新列表。", 404)
            now, deleted = time.time(), 0
            for row in rows:
                if row["deleted_at"] is not None:
                    continue
                c.execute("UPDATE samlab_members SET status='revoked',deleted_at=?,expires_at=NULL,"
                          "policy_version=policy_version+1,sync_state='pending_revoke',updated_at=? WHERE id=?",
                          (now, now, row["id"]))
                self._audit(c, actor, "whitelist.deleted", row["id"], note.strip())
                deleted += 1
            if deleted and len(member_ids) > 1:
                self._audit(c, actor, "whitelist.batch_deleted", str(uuid.uuid4()),
                            f"删除 {deleted} 人，已删除跳过 {len(rows) - deleted} 人；" + note.strip())
            return {"deleted": deleted, "already_deleted": len(rows) - deleted,
                    "message": f"已删除 {deleted} 人，已删除跳过 {len(rows) - deleted} 人；保留审计，关联账号停用以平台回执为准。"}

    def acknowledge(self, request_id: str, account_id: str, policy_version: int, state: str):
        if not 1 <= len(account_id) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in account_id):
            raise AuthError("平台账号编号须为 1 至 128 位可打印 ASCII 字符。")
        if state not in {"active", "disabled"}:
            raise AuthError("平台回执状态无效。")
        try:
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                current = self._status(c, request_id)
                if not current["member_id"] or current["policy_version"] != policy_version:
                    raise AuthError("准入策略已变化，请重新查询，不能使用旧版本回执。", 409)
                if state == "active" and not current["allowed"]:
                    raise AuthError("该人员当前没有有效准入授权。", 403)
                if state == "disabled" and current["allowed"]:
                    raise AuthError("有效准入不能回执为撤权完成。", 409)
                if current["account_id"] and current["account_id"] != account_id:
                    raise AuthError("平台账号编号与原映射不一致。", 409)
                if current["account_id"] == account_id and current["account_state"] == state:
                    return current
                c.execute("UPDATE samlab_members SET account_id=?,sync_state=?,synced_version=?,updated_at=? WHERE id=?",
                          (account_id, state, policy_version, time.time(), current["member_id"]))
                self._audit(c, "service:samlab", f"platform.{state}.acknowledged", current["member_id"])
                return self._status(c, request_id)
        except sqlite3.IntegrityError:
            raise AuthError("该平台账号已经绑定其他人员。", 409) from None

    def health(self):
        with self.connect() as c:
            row = c.execute("SELECT last_seen FROM samlab_service_health WHERE id=1").fetchone()
            seen = row["last_seen"] if row else None
            return {"last_seen": seen, "service_connected": bool(seen and time.time() - seen <= 30)}

    def dashboard(self):
        with self.connect() as c:
            legacy_tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            legacy_count = c.execute("SELECT COUNT(*) FROM lab_people").fetchone()[0] if "lab_people" in legacy_tables else 0
            members = []
            for row in c.execute("SELECT * FROM samlab_members WHERE deleted_at IS NULL "
                                 "ORDER BY account_id IS NULL,CAST(account_id AS INTEGER) DESC,updated_at DESC,id LIMIT 500"):
                item = dict(row)
                if item["source"] != "self":
                    item["username"] = item["display_username"] or ("未注册" if item["source"] in {"roster", "manual"} else item["username"])
                    if item["person_no"].startswith("account:"):
                        item["person_no"] = ""
                item["awaiting_registration"] = item["source"] in {"roster", "manual"} and not c.execute(
                    "SELECT 1 FROM samlab_requests WHERE member_id=?", (item["id"],)).fetchone()
                members.append(item)
            requests = []
            for row in c.execute("SELECT * FROM samlab_requests ORDER BY submitted_at DESC LIMIT 500"):
                item = dict(row)
                if item["source"] != "self":
                    item["username"] = item["display_username"] or item["username"]
                    if item["person_no"].startswith("account:"):
                        item["person_no"] = ""
                item["roster_match"] = bool(c.execute(
                    "SELECT 1 FROM samlab_members WHERE email=? AND source IN ('roster','manual') AND status='active' "
                    "AND deleted_at IS NULL", (item["email"],)).fetchone())
                requests.append(item)
            return {
                "platform": PLATFORM,
                "requests": requests,
                "members": members,
                "audits": [dict(r) for r in c.execute("SELECT * FROM samlab_audit ORDER BY id DESC LIMIT 100")],
                "legacy_people_count": legacy_count, "list_limit": 500,
            }
