"""Samlab contract and administrator boundaries, using disposable databases."""

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.testclient import TestClient
from starlette.requests import Request

from app.admission_api import PREFIX, install_admission, signing_message
from app.admission_store import AdmissionStore
from app.auth import install_auth
from app.auth_store import AuthError, AuthStore, password_hash, password_matches
from app.config import ROOT
from app.samlab_admission import SamlabAdmissionStore


PASSWORD = "Samlab-test-only-2026!"
SECRET = "Synthetic-test-signing-secret-not-for-production"


class AdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "AUTH_DB_PATH": str(Path(cls.temp.name) / "auth.sqlite3"),
            "ADMISSION_DB_PATH": str(Path(cls.temp.name) / "admission.sqlite3"),
            "BACKEND_DB_PATH": str(Path(cls.temp.name) / "backend.sqlite3"),
            "SAMLAB_INTEGRATION_SECRET": SECRET,
        })
        cls.env.start()
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
        install_auth(app, Jinja2Templates(directory=str(ROOT / "templates")))
        install_admission(app)
        cls.remote = False

        async def local_app(scope, receive, send):
            scope = dict(scope)
            if scope["type"] == "http":
                scope["client"] = ("203.0.113.1" if cls.remote else "127.0.0.1", 10000)
            await app(scope, receive, send)

        cls.asgi = staticmethod(local_app)
        cls.app = app
        with cls.client():
            pass
        cls.auth = app.state.auth_store
        cls.store = app.state.admission_store
        cls.encoded = password_hash(PASSWORD)
        cls.root_id = 1

    @classmethod
    def client(cls):
        return TestClient(cls.asgi, base_url="http://127.0.0.1")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        type(self).remote = False
        with self.store.connect() as c:
            for table in ("samlab_requests", "samlab_members", "samlab_service_nonces", "samlab_audit", "samlab_import_batches"):
                c.execute(f"DELETE FROM {table}")
        with self.auth.connect() as c:
            c.execute("DELETE FROM auth_sessions")
            c.execute("DELETE FROM auth_attempts")
            c.execute("DELETE FROM auth_users WHERE username<>'admin'")
            c.execute("UPDATE auth_users SET password_hash=?,role='admin',is_superadmin=1,is_active=1,"
                      "needs_password_change=0 WHERE username='admin'", (self.encoded,))
            type(self).root_id = c.execute("SELECT id FROM auth_users WHERE username='admin'").fetchone()[0]
        self.admin = self.client()
        self.service_client = self.client()
        self.login(self.admin)

    def tearDown(self):
        type(self).remote = False
        self.admin.close()
        self.service_client.close()

    def login(self, client, username="admin", password=PASSWORD):
        client.cookies.clear()
        client.get("/login")
        response = client.post("/api/auth/login", json={"username": username, "password": password},
                               headers={"X-CSRF-Token": client.cookies.get("sentinel_preflight", "")})
        return response

    def write(self, path, body, client=None):
        client = client or self.admin
        me = client.get("/api/auth/me")
        token = me.json().get("csrf_token", "")
        return client.post(path, json=body, headers={"X-CSRF-Token": token})

    def signed(self, method, path, body=None, nonce=None, timestamp=None, signature=None, raw=None):
        raw = (json.dumps(body, separators=(",", ":")).encode() if body is not None else b"") if raw is None else raw
        timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
        nonce = nonce or secrets.token_urlsafe(24)
        signature = signature or hmac.new(
            SECRET.encode(), signing_message(timestamp, nonce, method, path, raw), hashlib.sha256,
        ).hexdigest()
        return self.service_client.request(method, path, content=raw, headers={
            "X-Samlab-Timestamp": timestamp, "X-Samlab-Nonce": nonce, "X-Samlab-Signature": signature,
            "Content-Type": "application/json",
        })

    def application(self, request_id="req-01", **changes):
        return {"request_id": request_id, "username": "student01",
                "email": "student01@example.edu", **changes}

    def submit(self, body=None):
        response = self.signed("POST", PREFIX + "/requests", body or self.application())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve(self, request_id="req-01"):
        response = self.write(f"/api/lab/admin/requests/{request_id}/review", {"decision": "approved"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_anonymous_cannot_view_admin_data(self):
        with self.client() as c:
            for path in ("/api/lab/admin/dashboard", "/api/auth/admins"):
                self.assertEqual(c.get(path).status_code, 401)

    def test_login_fallback_works_while_frontend_assets_are_unavailable(self):
        with self.client() as client, patch("app.auth.render_vue_app", return_value=None):
            response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def direct_account(self, **changes):
        return {"request_id": "admin-account-123", "username": "samlab_u123", "person_no": "account:123",
                "email": "admin-created@example.edu", "account_id": "123", "actor_id": 7,
                "source": "admin_direct", "display_username": "管理员创建用户", **changes}

    def test_admin_creation_automatically_grants_whitelist_once_with_provenance(self):
        payload = self.direct_account()
        first = self.signed("POST", PREFIX + "/admin-provision", payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["allowed"])
        self.assertEqual(first.json()["account_id"], "123")
        self.assertEqual(self.signed("POST", PREFIX + "/admin-provision", payload).status_code, 200)
        dashboard = self.store.dashboard()
        self.assertEqual(len(dashboard["members"]), 1)
        self.assertEqual(dashboard["requests"][0]["status"], "approved")
        self.assertEqual(dashboard["requests"][0]["reviewed_by"], "samlab-admin:7")
        self.assertEqual(dashboard["members"][0]["source"], "admin_direct")
        self.assertEqual(dashboard["members"][0]["username"], "管理员创建用户")
        self.assertEqual(dashboard["members"][0]["person_no"], "")
        self.assertEqual(sum(a["action"] == "whitelist.admin_provisioned" for a in dashboard["audits"]), 1)

    def test_admin_delivery_retry_cannot_restore_revoked_whitelist(self):
        payload = self.direct_account()
        result = self.signed("POST", PREFIX + "/admin-provision", payload).json()
        self.store.revoke(result["member_id"], "user:admin", "管理员撤销该账号", delete=True)
        response = self.signed("POST", PREFIX + "/admin-provision", payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["allowed"])
        self.assertEqual(response.json()["policy_version"], 2)

    def test_admin_creation_requires_signed_service_and_rejects_self_elevation(self):
        self.assertEqual(self.admin.post(PREFIX + "/admin-provision", json=self.direct_account()).status_code, 401)
        self.assertEqual(self.signed("POST", PREFIX + "/requests",
                                    self.application(source="admin_direct", actor_id=7)).status_code, 422)
        self.assertEqual(self.signed("POST", PREFIX + "/admin-provision",
                                    self.direct_account(actor_id=0)).status_code, 422)
        self.assertEqual(self.signed("POST", PREFIX + "/admin-provision",
                                    self.direct_account(account_id="124")).status_code, 400)
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_admin_invitation_autoapproved_without_public_review(self):
        payload = self.direct_account(request_id="admin-invitation-123", username="invited_user",
                                      person_no="2026012345", source="admin_invite")
        response = self.signed("POST", PREFIX + "/admin-provision", payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["allowed"])
        self.assertEqual(self.store.dashboard()["requests"][0]["source"], "admin_invite")

    def test_admin_creation_rolls_back_when_audit_storage_fails(self):
        with patch.object(SamlabAdmissionStore, "_audit", side_effect=sqlite3.OperationalError("synthetic failure")):
            response = self.signed("POST", PREFIX + "/admin-provision", self.direct_account())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.dashboard()["requests"], [])
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_disconnected_signed_body_is_denied_without_consuming_nonce(self):
        path = PREFIX + "/requests"
        raw = json.dumps(self.application()).encode()
        timestamp, nonce = str(int(time.time())), secrets.token_urlsafe(24)
        signature = hmac.new(
            SECRET.encode(), signing_message(timestamp, nonce, "POST", path, raw), hashlib.sha256,
        ).hexdigest()
        headers = [(key.lower().encode(), value.encode()) for key, value in {
            "X-Samlab-Timestamp": timestamp, "X-Samlab-Nonce": nonce,
            "X-Samlab-Signature": signature, "Content-Length": str(len(raw)),
        }.items()]
        messages = iter([
            {"type": "http.request", "body": raw[:8], "more_body": True},
            {"type": "http.disconnect"},
        ])

        async def receive():
            return next(messages)

        request = Request({"type": "http", "method": "POST", "path": path,
                           "query_string": b"", "headers": headers,
                           "server": ("127.0.0.1", 80), "scheme": "http"}, receive)
        route = next(route for route in self.app.routes if getattr(route, "path", None) == path)
        with self.assertRaises(AuthError) as error:
            asyncio.run(route.dependant.dependencies[0].call(request))
        self.assertEqual(error.exception.status_code, 400)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM samlab_requests").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM samlab_service_nonces").fetchone()[0], 0)
        self.assertEqual(self.signed("POST", path, nonce=nonce, raw=raw).status_code, 200)

    def test_expiry_advances_policy_once_and_cannot_renew_expired_member(self):
        self.submit()
        approved = self.approve()
        with self.store.connect() as c:
            c.execute("UPDATE samlab_members SET expires_at=? WHERE id=?",
                      (time.time() - 1, approved["member_id"]))
        first = self.store.status("req-01")
        second = self.store.status("req-01")
        self.assertFalse(first["allowed"])
        self.assertEqual(first["valid_until"], 0)
        self.assertEqual(first["policy_version"], approved["policy_version"] + 1)
        self.assertEqual(second["policy_version"], first["policy_version"])
        with self.assertRaises(AuthError):
            self.store.renew(approved["member_id"], "admin", time.time() + 100)

    def test_renewal_updates_policy_and_bounds_lease_by_expiry(self):
        self.submit()
        approved = self.approve()
        expiry = time.time() + 5
        self.store.renew(approved["member_id"], "admin", expiry)
        result = self.store.status("req-01")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["policy_version"], approved["policy_version"] + 1)
        self.assertLessEqual(result["valid_until"], expiry)

    def test_public_application_routes_are_gone_even_with_old_cookies(self):
        for path in ("/lab", "/lab/activate", "/lab/workspace", "/api/lab/portal/signup",
                     "/api/lab/portal/login", "/api/lab/portal/apply", "/api/lab/platform/register"):
            response = self.service_client.get(path) if path.startswith("/lab") else self.service_client.post(path, json={})
            self.assertEqual(response.status_code, 410, path)
        response = self.write("/api/auth/register", {"username": "outsider", "password": PASSWORD, "confirm_password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_superadmin_creates_regular_admin_not_superadmin(self):
        response = self.write("/api/auth/admins", {"username": "operator01", "password": "123456"})
        self.assertEqual(response.status_code, 201, response.text)
        with self.auth.connect() as c:
            user = c.execute("SELECT * FROM auth_users WHERE username='operator01'").fetchone()
            self.assertEqual(user["role"], "admin")
            self.assertEqual(user["is_superadmin"], 0)
            self.assertEqual(user["needs_password_change"], 1)
            self.assertTrue(password_matches("123456", user["password_hash"]))
        self.assertNotIn("password", response.json())
        self.assertNotIn("password_hash", response.json())

    def test_ordinary_admin_cannot_create_or_list_admins(self):
        user = self.auth.create_admin(self.root_id, "operator01", PASSWORD)
        with self.auth.connect() as c:
            c.execute("UPDATE auth_users SET needs_password_change=0 WHERE id=?", (user["id"],))
        with self.client() as c:
            self.assertEqual(self.login(c, "operator01").status_code, 200)
            self.assertEqual(c.get("/api/auth/admins").status_code, 403)
            result = self.write("/api/auth/admins", {"username": "elevated", "password": PASSWORD}, c)
            self.assertEqual(result.status_code, 403)
            self.assertEqual(c.get("/api/lab/admin/dashboard").status_code, 200)

    def test_forged_superadmin_field_rejected_without_echo(self):
        response = self.write("/api/auth/admins", {"username": "fakeadmin", "password": PASSWORD, "is_superadmin": True})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(PASSWORD, response.text)

    def test_admin_creation_requires_csrf(self):
        self.assertEqual(self.admin.post("/api/auth/admins", json={"username": "fakeadmin", "password": PASSWORD}).status_code, 403)

    def test_disabled_admin_loses_session_and_cannot_login(self):
        user = self.auth.create_admin(self.root_id, "operator01", PASSWORD)
        with self.auth.connect() as c:
            c.execute("UPDATE auth_users SET needs_password_change=0 WHERE id=?", (user["id"],))
        with self.client() as c:
            self.assertEqual(self.login(c, "operator01").status_code, 200)
            self.assertEqual(self.write(f"/api/auth/admins/{user['id']}/status", {"is_active": False}).status_code, 200)
            self.assertEqual(c.get("/api/auth/me").status_code, 401)
            self.assertEqual(self.login(c, "operator01").status_code, 401)

    def test_superadmin_cannot_be_disabled(self):
        response = self.write(f"/api/auth/admins/{self.root_id}/status", {"is_active": False})
        self.assertEqual(response.status_code, 403)

    def test_nonadmin_legacy_user_cannot_login(self):
        with self.auth.connect() as c:
            c.execute("INSERT INTO auth_users(username,password_hash,created_at,role) VALUES (?,?,?,'viewer')",
                      ("legacyviewer", self.encoded, time.time()))
        with self.client() as c:
            self.assertEqual(self.login(c, "legacyviewer").status_code, 401)

    def test_initial_password_blocks_sensitive_reads_and_writes(self):
        user = self.auth.create_admin(self.root_id, "operator01", PASSWORD)
        with self.client() as c:
            self.assertEqual(self.login(c, "operator01").status_code, 200)
            self.assertEqual(c.get("/api/lab/admin/dashboard").status_code, 403)
            result = self.write("/api/auth/password", {"current_password": PASSWORD, "new_password": "New-password-2026!", "confirm_password": "New-password-2026!"}, c)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(c.get("/api/lab/admin/dashboard").status_code, 200)
        self.assertFalse(self.auth.list_admins(self.root_id)[-1]["needs_password_change"])

    def test_unsigned_service_call_rejected_even_for_admin(self):
        self.assertEqual(self.admin.post(PREFIX + "/requests", json=self.application()).status_code, 401)

    def test_unconfigured_service_fails_closed(self):
        with patch.dict(os.environ, {"SAMLAB_INTEGRATION_SECRET": ""}):
            self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application()).status_code, 503)

    def test_signed_request_received_once(self):
        first = self.submit()
        self.assertEqual(first["decision"], "pending")
        self.assertFalse(first["allowed"])
        self.assertEqual(self.submit(), first)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samlab_requests").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samlab_audit").fetchone()[0], 1)

    def test_payload_conflict_and_duplicate_pending_identity_rejected(self):
        self.submit()
        self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application(email="other@example.edu")).status_code, 409)
        self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application("req-02")).status_code, 409)

    def test_email_mandatory_without_student_number(self):
        for change in ({"email": ""}, {"email": "invalid"}, {"email": "a@@example.edu"}):
            result = self.signed("POST", PREFIX + "/requests", self.application(**change))
            self.assertIn(result.status_code, (400, 422), result.text)
        body = self.application()
        del body["email"]
        self.assertEqual(self.signed("POST", PREFIX + "/requests", body).status_code, 422)
        self.assertEqual(self.submit()["decision"], "pending")

    def test_passwords_not_accepted_or_echoed(self):
        body = self.application(password=PASSWORD)
        response = self.signed("POST", PREFIX + "/requests", body)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(PASSWORD, response.text)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samlab_requests").fetchone()[0], 0)
            self.assertNotIn("password", " ".join(r["name"] for r in c.execute("PRAGMA table_info(samlab_requests)")))

    def test_nonce_replay_and_old_timestamp_rejected(self):
        nonce = secrets.token_urlsafe(24)
        self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application(), nonce=nonce).status_code, 200)
        self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application(), nonce=nonce).status_code, 409)
        self.assertEqual(self.signed("GET", PREFIX + "/requests/req-01", timestamp=int(time.time())-301).status_code, 401)

    def test_tampered_signature_and_oversized_request_rejected(self):
        self.assertEqual(self.signed("POST", PREFIX + "/requests", self.application(), signature="0"*64).status_code, 401)
        self.assertEqual(self.signed("POST", PREFIX + "/requests", raw=b"x"*16385).status_code, 413)
        self.assertEqual(self.signed("POST", PREFIX + "/requests", raw=b"{invalid").status_code, 400)

    def test_approval_adds_whitelist_without_fake_account_creation(self):
        self.submit()
        status = self.approve()
        self.assertTrue(status["allowed"])
        self.assertEqual(status["account_state"], "pending_create")
        self.assertIsNone(status["account_id"])
        self.assertEqual(len(self.store.dashboard()["members"]), 1)
        refreshed = self.signed("GET", PREFIX + "/requests/req-01").json()
        self.assertGreaterEqual(refreshed.pop("valid_until"), status.pop("valid_until"))
        self.assertEqual(refreshed, status)

    def test_rejection_never_creates_whitelist(self):
        self.submit()
        result = self.write("/api/lab/admin/requests/req-01/review", {"decision": "rejected"})
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["allowed"])
        self.assertEqual(self.store.dashboard()["members"], [])
        self.assertEqual(self.write("/api/lab/admin/requests/req-01/review", {"decision": "approved"}).status_code, 409)

    def test_concurrent_approval_has_one_member_and_one_audit(self):
        self.submit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.store.review("req-01", "approved", "user:admin"), range(2)))
        for result in results:
            self.assertLessEqual(result.pop("valid_until") - time.time(), 15)
        self.assertEqual(results[0], results[1])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samlab_members").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samlab_audit WHERE action='whitelist.approved'").fetchone()[0], 1)

    def test_approval_and_whitelist_roll_back_when_audit_fails(self):
        self.submit()
        with patch.object(self.store, "_audit", side_effect=sqlite3.OperationalError("synthetic failure")):
            result = self.write("/api/lab/admin/requests/req-01/review", {"decision": "approved"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.store.status("req-01")["decision"], "pending")
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_only_signed_platform_ack_can_mark_account_active(self):
        self.submit()
        self.approve()
        ack = {"account_id": "external-user-1", "policy_version": 1, "state": "active"}
        path = PREFIX + "/requests/req-01/acknowledgement"
        self.assertEqual(self.admin.post(path, json=ack).status_code, 401)
        response = self.signed("POST", path, ack)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["account_state"], "active")
        repeated = self.signed("POST", path, ack).json()
        original = response.json()
        self.assertGreaterEqual(repeated.pop("valid_until"), original.pop("valid_until"))
        self.assertEqual(repeated, original)

    def test_revocation_denies_access_and_stale_ack(self):
        self.submit()
        approved = self.approve()
        self.assertEqual(self.write(f"/api/lab/admin/members/{approved['member_id']}/revoke", {"confirmed": True, "note": "人员离组停止准入"}).status_code, 200)
        result = self.signed("GET", PREFIX + "/requests/req-01").json()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["account_state"], "pending_revoke")
        ack = {"account_id": "external-user-1", "policy_version": 1, "state": "active"}
        self.assertEqual(self.signed("POST", PREFIX + "/requests/req-01/acknowledgement", ack).status_code, 409)
        ack.update(policy_version=2, state="disabled")
        self.assertEqual(self.signed("POST", PREFIX + "/requests/req-01/acknowledgement", ack).status_code, 200)

    def test_delete_keeps_history_and_requires_confirmation(self):
        self.submit()
        approved = self.approve()
        path = f"/api/lab/admin/members/{approved['member_id']}/delete"
        self.assertEqual(self.write(path, {"confirmed": False, "note": "人员离组停止准入"}).status_code, 400)
        self.assertEqual(self.write(path, {"confirmed": "yes", "note": "人员离组停止准入"}).status_code, 422)
        self.assertEqual(self.write(path, {"confirmed": True, "note": "人员离组停止准入"}).status_code, 200)
        self.assertEqual(self.store.dashboard()["members"], [])
        self.assertEqual(len(self.store.dashboard()["requests"]), 1)
        self.assertFalse(self.store.status("req-01")["allowed"])

    def test_expiry_is_checked_on_every_status_query(self):
        self.submit()
        approved = self.approve()
        with self.store.connect() as c:
            c.execute("UPDATE samlab_members SET expires_at=? WHERE id=?", (time.time()-1, approved["member_id"]))
        self.assertFalse(self.store.status("req-01")["allowed"])
        self.assertTrue(self.store.dashboard()["members"][0]["expired"])

    def test_host_and_remote_browser_boundary_preserved(self):
        self.assertEqual(self.admin.get("/api/lab/admin/dashboard", headers={"host": "evil.example"}).status_code, 400)
        type(self).remote = True
        self.assertEqual(self.admin.get("/api/lab/admin/dashboard").status_code, 403)

    def test_existing_rows_preserved_and_not_autoapproved(self):
        path = Path(self.temp.name) / ("legacy-" + secrets.token_hex(8) + ".sqlite3")
        legacy = AdmissionStore(path)
        legacy.initialize()
        with legacy.connect() as c:
            c.execute("INSERT INTO lab_people(username,real_name,person_no,created_at) VALUES ('legacy','测试','2026000001',?)", (time.time(),))
            before = [tuple(r) for r in c.execute("SELECT * FROM lab_people")]
        current = SamlabAdmissionStore(path)
        current.initialize()
        current.initialize()
        with current.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM lab_people")], before)
        self.assertEqual(current.dashboard()["legacy_people_count"], 1)
        self.assertEqual(current.dashboard()["members"], [])

    def test_superadmin_migration_is_idempotent_and_preserves_credentials(self):
        path = Path(self.temp.name) / ("auth-" + secrets.token_hex(8) + ".sqlite3")
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE auth_users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,"
                      "needs_password_change INTEGER DEFAULT 0,password_version INTEGER DEFAULT 0,created_at REAL,role TEXT)")
            c.executemany("INSERT INTO auth_users(username,password_hash,created_at,role) VALUES (?,?,?,?)",
                          [("admin", self.encoded, time.time(), "admin"), ("operator", self.encoded, time.time(), "admin"),
                           ("viewer", self.encoded, time.time(), "viewer")])
        store = AuthStore(path)
        store.initialize()
        store.initialize()
        with store.connect() as c:
            rows = list(c.execute("SELECT * FROM auth_users ORDER BY id"))
            self.assertEqual([r["is_superadmin"] for r in rows], [1, 0, 0])
            self.assertTrue(all(r["password_hash"] == self.encoded for r in rows))

    def test_reapproval_does_not_silently_restore_revoked_member(self):
        self.submit()
        status = self.approve()
        self.store.revoke(status["member_id"], "user:admin", "人员离组停止准入", delete=True)
        self.approve()
        self.assertFalse(self.store.status("req-01")["allowed"])
        self.submit(self.application("new-explicit-request"))
        restored = self.approve("new-explicit-request")
        self.assertTrue(restored["allowed"])
        self.assertEqual(restored["member_id"], status["member_id"])
        self.assertEqual(restored["policy_version"], 3)

    def test_other_identity_cannot_take_existing_username(self):
        self.submit()
        self.approve()
        self.submit(self.application("req-other", person_no="2026000002", email="other@example.edu"))
        response = self.write("/api/lab/admin/requests/req-other/review", {"decision": "approved"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.status("req-other")["decision"], "pending")

    def test_nonfinite_expiry_cannot_be_saved(self):
        self.submit()
        with self.assertRaises(AuthError):
            self.store.review("req-01", "approved", "user:admin", float("inf"))
        with self.assertRaises(AuthError):
            self.store.review("req-01", "approved", "user:admin", float("nan"))

    def test_service_query_string_and_invalid_decision_rejected(self):
        self.assertEqual(self.signed("GET", PREFIX + "/requests/req-01?extra=1").status_code, 401)
        self.submit()
        self.assertEqual(self.write("/api/lab/admin/requests/req-01/review", {"decision": "anything"}).status_code, 422)

    def preview_roster(self, text, client=None):
        client = client or self.admin
        csrf = client.get("/api/auth/me").json().get("csrf_token", "")
        return client.post("/api/lab/admin/roster/preview",
                           content=text.encode("utf-8-sig") if isinstance(text, str) else text,
                           headers={"X-CSRF-Token": csrf, "Content-Type": "text/csv"})

    def import_roster(self, text):
        preview = self.preview_roster(text)
        self.assertEqual(preview.status_code, 200, preview.text)
        result = self.write("/api/lab/admin/roster/commit", {"batch_id": preview.json()["batch_id"]})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_roster_preview_commit_deduplicates_and_is_idempotent(self):
        preview = self.preview_roster("邮箱,用户名,有效期\nA@example.edu,张三,\na@example.edu,重复,\nb@example.edu,,\n")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["summary"], {"add": 2, "skip": 1, "error": 0})
        self.assertEqual(self.store.dashboard()["members"], [])
        body = {"batch_id": preview.json()["batch_id"]}
        first = self.write("/api/lab/admin/roster/commit", body)
        second = self.write("/api/lab/admin/roster/commit", body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        data = self.store.dashboard()
        self.assertEqual(len(data["members"]), 2)
        self.assertTrue(all(m["account_id"] is None and m["awaiting_registration"] for m in data["members"]))
        self.assertEqual(sum(a["action"] == "roster.import.committed" for a in data["audits"]), 1)

    def test_roster_invalid_row_blocks_entire_commit(self):
        result = self.preview_roster("email,username\nok@example.edu,ok\nbad-mail,bad\n")
        self.assertEqual(result.json()["summary"]["error"], 1)
        commit = self.write("/api/lab/admin/roster/commit", {"batch_id": result.json()["batch_id"]})
        self.assertEqual(commit.status_code, 409)
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_roster_rejects_extra_secret_columns_malformed_and_large_files(self):
        for raw in ("email,password\na@example.edu,SyntheticSecret\n", b"\x00\x01", "email\n\"unterminated"):
            response = self.preview_roster(raw)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("SyntheticSecret", response.text)
        self.assertEqual(self.preview_roster(b"x" * (1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.preview_roster("email\n" + "a@example.edu\n" * 2001).status_code, 400)

    def test_roster_requires_admin_session_csrf_and_batch_owner(self):
        with self.client() as anon:
            self.assertEqual(anon.post("/api/lab/admin/roster/preview", content=b"email\n").status_code, 401)
        self.assertEqual(self.admin.post("/api/lab/admin/roster/preview", content=b"email\n").status_code, 403)
        batch_id = self.preview_roster("email\na@example.edu\n").json()["batch_id"]
        self.auth.create_admin(self.root_id, "import_operator", PASSWORD)
        with self.auth.connect() as c:
            c.execute("UPDATE auth_users SET needs_password_change=0 WHERE username='import_operator'")
        with self.client() as other:
            self.login(other, "import_operator")
            self.assertEqual(self.write("/api/lab/admin/roster/commit", {"batch_id": batch_id}, other).status_code, 404)

    def test_roster_reimport_cannot_restore_revoked_or_deleted_people(self):
        self.import_roster("email\na@example.edu\n")
        member = self.store.dashboard()["members"][0]
        self.store.revoke(member["id"], "user:admin", "已确认人员离组", delete=True)
        result = self.import_roster("email\na@example.edu\n")
        self.assertEqual(result["summary"], {"add": 0, "skip": 1, "error": 0})
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_roster_matches_by_email_but_does_not_auto_approve_unverified_applicant(self):
        self.submit()
        self.import_roster("邮箱,用户名,有效期\nstudent01@example.edu,名单姓名,2027-01-01T00:00:00+08:00\n")
        member = self.store.dashboard()["members"][0]
        self.assertTrue(self.store.dashboard()["requests"][0]["roster_match"])
        self.assertFalse(self.store.status("req-01")["allowed"])
        approved = self.approve()
        self.assertTrue(approved["allowed"])
        self.assertEqual(approved["member_id"], member["id"])
        self.assertEqual(len(self.store.dashboard()["members"]), 1)
        self.assertEqual(self.store.dashboard()["members"][0]["expires_at"], member["expires_at"])

    def test_roster_admin_direct_creation_binds_imported_person_without_review(self):
        self.import_roster("email,username\nadmin-created@example.edu,实验室人员\n")
        member_id = self.store.dashboard()["members"][0]["id"]
        response = self.signed("POST", PREFIX + "/admin-provision", self.direct_account())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["allowed"])
        self.assertEqual(response.json()["member_id"], member_id)
        self.assertEqual(response.json()["account_id"], "123")
        self.assertEqual(len(self.store.dashboard()["members"]), 1)

    def test_roster_audit_failure_rolls_back_entire_import(self):
        batch_id = self.preview_roster("email\na@example.edu\nb@example.edu\n").json()["batch_id"]
        with patch.object(SamlabAdmissionStore, "_audit", side_effect=sqlite3.OperationalError("synthetic")):
            response = self.write("/api/lab/admin/roster/commit", {"batch_id": batch_id})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.dashboard()["members"], [])

    def test_roster_expired_batch_and_formula_name_are_rejected(self):
        preview = self.preview_roster("email,username\na@example.edu,=HYPERLINK(\"bad\")\n")
        self.assertEqual(preview.json()["summary"]["error"], 1)
        batch_id = self.preview_roster("email\na@example.edu\n").json()["batch_id"]
        with self.store.connect() as c:
            c.execute("UPDATE samlab_import_batches SET expires_at=? WHERE id=?", (time.time() - 1, batch_id))
        self.assertEqual(self.write("/api/lab/admin/roster/commit", {"batch_id": batch_id}).status_code, 409)

    def test_roster_gb18030_and_concurrent_commit(self):
        from app.roster_import import commit
        preview = self.preview_roster("邮箱,用户名\na@example.edu,实验室人员\n".encode("gb18030"))
        self.assertEqual(preview.status_code, 200)
        batch_id = preview.json()["batch_id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: commit(self.store, batch_id, "user:admin"), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.dashboard()["members"]), 1)
        self.assertEqual(sum(a["action"] == "whitelist.imported" for a in self.store.dashboard()["audits"]), 1)


if __name__ == "__main__":
    unittest.main()
