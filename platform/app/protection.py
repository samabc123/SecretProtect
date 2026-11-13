"""CrowdSec control-plane adapter. No shell execution or arbitrary API proxy."""
from __future__ import annotations

import json
import ipaddress
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from .auth_store import AuthError
from .config import ROOT

ENGINE_VERSION = "1.8.2"
MAX_RESPONSE = 4 * 1024 * 1024


def secret_file(variable: str) -> str:
    filename = os.getenv(variable, "")
    return Path(filename).read_text(encoding="utf-8").strip() if filename else ""


def configured() -> bool:
    return bool(os.getenv("CROWDSEC_CREDENTIALS_FILE") and os.getenv("CROWDSEC_BOUNCER_KEY_FILE"))


class CrowdSec:
    """A registered machine identity is required; a bouncer key is not enough."""

    def __init__(self, transport=None):
        path = os.getenv("CROWDSEC_CREDENTIALS_FILE", "")
        if not path:
            raise AuthError("尚未配置 CrowdSec 机器凭据文件。", 503)
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            self.url = str(data["url"]).rstrip("/")
            self.login, self.password = data["login"], data["password"]
            parts = urlsplit(self.url)
            allowed_http = {"127.0.0.1", "::1", "localhost", "crowdsec"}
            if (parts.scheme not in {"http", "https"} or parts.username or parts.password
                    or parts.query or parts.fragment or parts.path not in {"", "/"}
                    or not parts.hostname or not self.login or not self.password
                    or (parts.scheme == "http" and parts.hostname not in allowed_http)):
                raise ValueError("unsafe engine URL")
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            raise AuthError("CrowdSec 凭据文件无效；只允许本机、专用 crowdsec 容器或 HTTPS 服务。", 503) from None
        self.transport = transport
        self.token = None

    def call(self, method: str, path: str, payload=None, *, bouncer=False):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if bouncer:
            try:
                key = secret_file("CROWDSEC_BOUNCER_KEY_FILE")
            except OSError:
                raise AuthError("无法读取决策查询凭据文件。", 503) from None
            if not key:
                raise AuthError("尚未配置决策查询凭据文件。", 503)
            headers = {"X-Api-Key": key}
        verify = os.getenv("CROWDSEC_CA_FILE") or True
        if isinstance(verify, str):
            verify = ssl.create_default_context(cafile=verify)
        try:
            with httpx.Client(timeout=httpx.Timeout(8), verify=verify, trust_env=False,
                              follow_redirects=False, transport=self.transport) as client:
                with client.stream(method, self.url + path, json=payload, headers=headers) as response:
                    if response.status_code not in {200, 201, 204}:
                        raise AuthError(f"CrowdSec 请求失败（HTTP {response.status_code}）。", 502)
                    body = bytearray()
                    deadline = time.monotonic() + 15
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE or time.monotonic() > deadline:
                            raise AuthError("CrowdSec 返回数据超过安全上限。", 502)
                    return json.loads(body) if body else None
        except (httpx.HTTPError, ValueError, OSError):
            raise AuthError("无法连接 CrowdSec；请检查地址、证书及机器授权。", 502) from None

    def authenticate(self):
        value = self.call("POST", "/v1/watchers/login", {
            "machine_id": self.login, "password": self.password, "scenarios": [],
        })
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise AuthError("CrowdSec 登录响应无效。", 502)
        self.token = value["token"]

    def snapshot(self):
        self.authenticate()
        alerts = self.call("GET", "/v1/alerts?limit=100")
        decisions = self.call("GET", "/v1/decisions", bouncer=True)
        if alerts is None:
            alerts = []
        if decisions is None:
            decisions = []
        if not isinstance(alerts, list) or not isinstance(decisions, list):
            raise AuthError("CrowdSec 返回的数据结构无效。", 502)
        if len(decisions) > 10000:
            raise AuthError("决策数量超过单平台上限，未覆盖现有快照。", 502)
        return alerts, decisions

    def delete_decision(self, identity: int):
        self.authenticate()
        return self.call("DELETE", f"/v1/decisions/{identity}")


def identifier(value) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise AuthError("引擎记录编号无效。", 502)
    return int(value)


def text(value, limit=180):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or ""))[:limit]


def safe_source(scope, value):
    try:
        if scope == "Ip":
            return str(ipaddress.ip_address(value))
        if scope == "Range":
            return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        pass
    return "[已脱敏]"


def rule_name(value):
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z0-9_./:-]{1,180}", value) else "[非标准标识已脱敏]"


def sanitize_alert(raw):
    """Only structural fields cross the boundary; context/events/message never do."""
    if not isinstance(raw, dict):
        raise AuthError("引擎事件格式无效。", 502)
    source = raw.get("source") or {}
    if not isinstance(source, dict):
        raise AuthError("引擎事件来源格式无效。", 502)
    scope = text(source.get("scope"), 30)
    return {
        "id": identifier(raw.get("id")), "scenario": rule_name(raw.get("scenario")),
        "source": safe_source(scope, source.get("value")),
        "scope": scope if scope in {"Ip", "Range"} else "redacted",
        "country": text(source.get("cn"), 2),
        "events_count": max(0, min(int(raw.get("events_count") or 0), 10**9)),
        "created_at": text(raw.get("created_at"), 50),
        "start_at": text(raw.get("start_at"), 50), "stop_at": text(raw.get("stop_at"), 50),
        "simulated": bool(raw.get("simulated")),
    }


def sanitize_decision(raw):
    if not isinstance(raw, dict):
        raise AuthError("引擎决策格式无效。", 502)
    scope = text(raw.get("scope"), 30)
    return {
        "id": identifier(raw.get("id")), "scope": scope,
        "value": safe_source(scope, raw.get("value")),
        "type": rule_name(raw.get("type")), "scenario": rule_name(raw.get("scenario")),
        "origin": rule_name(raw.get("origin")), "duration": text(raw.get("duration"), 40),
    }


class ProtectionStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("PROTECTION_DB_PATH", ROOT / "data" / "protection.sqlite3"))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS engine_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_attempt REAL, last_success REAL,
                    error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT NOT NULL DEFAULT '',
                    worker_seen REAL, next_attempt REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO engine_state(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS attack_events (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_decisions (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protection_audit (
                    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                    subject TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, sent_at REAL
                );
            """)

    @staticmethod
    def audit(c, actor, action, subject="", note=""):
        c.execute("INSERT INTO protection_audit(actor,action,subject,note,created_at) VALUES(?,?,?,?,?)",
                  (actor, action, str(subject), note, time.time()))

    def status(self):
        with self.connect() as c:
            state = dict(c.execute("SELECT * FROM engine_state WHERE id=1").fetchone())
            state.pop("lease_owner")
            counts = {row["status"]: row["n"] for row in c.execute(
                "SELECT status,COUNT(*) n FROM attack_events GROUP BY status")}
            decisions = c.execute("SELECT COUNT(*) FROM engine_decisions").fetchone()[0]
            outbox = {row["status"]: row["n"] for row in c.execute(
                "SELECT status,COUNT(*) n FROM notification_outbox GROUP BY status")}
        interval = sync_interval()
        state.update({
            "configured": configured(), "version": ENGINE_VERSION,
            "machine_configured": bool(os.getenv("CROWDSEC_CREDENTIALS_FILE")),
            "decision_reader_configured": bool(os.getenv("CROWDSEC_BOUNCER_KEY_FILE")),
            "connection": "not_configured" if not configured() else (
                "error" if state["error"] else "connected" if state["last_success"] else "pending"),
            "stale": not state["last_success"] or time.time() - state["last_success"] > max(90, interval * 3),
            "worker_online": bool(state["worker_seen"] and time.time() - state["worker_seen"] < 90),
            "counts": counts, "decisions_count": decisions, "notifications": outbox,
            "notifications_configured": bool(os.getenv("SMTP_HOST")),
            "remediation_enabled": os.getenv("CROWDSEC_ALLOW_DELETE") == "true",
            "bouncer_verified": False, "alert_window": 100, "sync_interval": interval,
        })
        return state

    def events(self):
        with self.connect() as c:
            return [{**json.loads(r["payload"]), "status": r["status"],
                     "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                    for r in c.execute("SELECT * FROM attack_events ORDER BY id DESC LIMIT 1000")]

    def decisions(self):
        with self.connect() as c:
            return [json.loads(r["payload"]) for r in c.execute("SELECT * FROM engine_decisions ORDER BY id DESC")]

    def audits(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM protection_audit ORDER BY id DESC LIMIT 200")]

    def notifications(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT id,event_id,status,attempts,last_error,created_at,sent_at "
                "FROM notification_outbox ORDER BY id DESC LIMIT 200")]

    def review(self, event_id, status, actor):
        if status not in {"new", "confirmed", "false_positive", "resolved"}:
            raise AuthError("处置状态无效。")
        with self.connect() as c:
            cursor = c.execute("UPDATE attack_events SET status=? WHERE id=?", (status, event_id))
            if not cursor.rowcount:
                raise AuthError("事件不存在。", 404)
            self.audit(c, actor, "event.review", event_id, status)

    def claim(self, actor):
        now, owner = time.time(), secrets.token_hex(16)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM engine_state WHERE id=1").fetchone()
            if row["lease_until"] > now:
                raise AuthError("引擎同步正在进行，请稍后刷新。", 409)
            c.execute("UPDATE engine_state SET lease_until=?,lease_owner=?,last_attempt=? WHERE id=1",
                      (now + 120, owner, now))
            self.audit(c, actor, "engine.sync.started")
        return owner

    def sync(self, actor="worker", client=None):
        owner = self.claim(actor)
        try:
            raw_alerts, raw_decisions = (client or CrowdSec()).snapshot()
            alerts = [sanitize_alert(a) for a in raw_alerts]
            decisions = [sanitize_decision(d) for d in raw_decisions]
            now = time.time()
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                if c.execute("SELECT lease_owner FROM engine_state WHERE id=1").fetchone()[0] != owner:
                    raise AuthError("同步租约已失效，结果未写入。", 409)
                for alert in alerts:
                    payload = json.dumps(alert, ensure_ascii=False)
                    c.execute("INSERT INTO attack_events(id,payload,first_seen,last_seen) VALUES(?,?,?,?) "
                              "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen",
                              (alert["id"], payload, now, now))
                    c.execute("INSERT OR IGNORE INTO notification_outbox(event_id,payload,created_at) VALUES(?,?,?)",
                              (alert["id"], payload, now))
                c.execute("DELETE FROM engine_decisions")
                c.executemany("INSERT INTO engine_decisions(id,payload) VALUES(?,?)",
                              [(d["id"], json.dumps(d, ensure_ascii=False)) for d in decisions])
                c.execute("UPDATE engine_state SET last_success=?,error='',failures=0,lease_until=0,"
                          "lease_owner='',next_attempt=? WHERE id=1", (now, now + sync_interval()))
                self.audit(c, actor, "engine.sync.succeeded", note=f"alerts={len(alerts)},decisions={len(decisions)}")
        except Exception as error:
            safe = str(error) if isinstance(error, AuthError) else "引擎响应处理失败，原始数据未记录。"
            with self.connect() as c:
                failures = c.execute("SELECT failures FROM engine_state WHERE id=1").fetchone()[0] + 1
                c.execute("UPDATE engine_state SET error=?,failures=?,lease_until=0,lease_owner='',"
                          "next_attempt=? WHERE id=1 AND lease_owner=?",
                          (safe, failures, time.time() + min(900, 15 * 2**min(failures, 6)), owner))
                self.audit(c, actor, "engine.sync.failed", note=safe)
            raise AuthError(safe, error.status_code if isinstance(error, AuthError) else 502) from None
        return self.status()

    def delete_decision(self, decision_id, actor, note, client=None):
        if os.getenv("CROWDSEC_ALLOW_DELETE") != "true":
            raise AuthError("引擎处置默认只读；需在服务器显式启用解封权限。", 403)
        with self.connect() as c:
            if not c.execute("SELECT 1 FROM engine_decisions WHERE id=?", (decision_id,)).fetchone():
                raise AuthError("决策不存在，请先同步。", 404)
            self.audit(c, actor, "decision.delete.requested", decision_id, note)
        try:
            (client or CrowdSec()).delete_decision(decision_id)
        except AuthError:
            with self.connect() as c:
                self.audit(c, actor, "decision.delete.failed", decision_id)
            raise
        with self.connect() as c:
            c.execute("DELETE FROM engine_decisions WHERE id=?", (decision_id,))
            self.audit(c, actor, "decision.delete.accepted", decision_id, note)
        return {"message": "引擎已接受删除；执行组件撤销拦截仍需现场确认。"}


def sync_interval():
    return max(15, min(3600, int(os.getenv("CROWDSEC_SYNC_INTERVAL", "30"))))


def deliver_notifications(store):
    if not os.getenv("SMTP_HOST"):
        return
    # A transaction claims one row; expired sending rows can recover after a crash.
    for _ in range(10):
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE notification_outbox SET status='failed',last_error=? "
                      "WHERE status='sending' AND attempts>=5 AND next_attempt<=?",
                      ("发送中断且已达到重试上限，请人工核查。", time.time()))
            row = c.execute("SELECT * FROM notification_outbox WHERE status IN ('pending','retry','sending') "
                            "AND next_attempt<=? AND attempts<5 ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return
            c.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,next_attempt=? WHERE id=?",
                      (time.time() + 120, row["id"]))
        try:
            message = EmailMessage()
            message["From"] = os.environ["SMTP_FROM"]
            message["To"] = os.environ["SMTP_TO"]
            message["Subject"] = f"SecertProtect 安全事件 #{row['event_id']}"
            message["Message-ID"] = f"<secertprotect-{row['event_id']}@notifications.local>"
            message.set_content(row["payload"])
            with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "465")),
                                  timeout=15, context=ssl.create_default_context()) as smtp:
                if os.getenv("SMTP_USERNAME"):
                    smtp.login(os.environ["SMTP_USERNAME"], secret_file("SMTP_PASSWORD_FILE"))
                refused = smtp.send_message(message)
                if refused:
                    raise RuntimeError("recipient refused")
            with store.connect() as c:
                c.execute("UPDATE notification_outbox SET status='sent',sent_at=?,last_error='' WHERE id=?",
                          (time.time(), row["id"]))
        except Exception:
            attempts = row["attempts"] + 1
            with store.connect() as c:
                c.execute("UPDATE notification_outbox SET status=?,next_attempt=?,last_error=? WHERE id=?",
                          ("failed" if attempts >= 5 else "retry", time.time() + min(3600, 30 * 2**attempts),
                           "邮件发送失败，请检查服务器 SMTP 配置。", row["id"]))
