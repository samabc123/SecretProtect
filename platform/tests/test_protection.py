import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.templating import Jinja2Templates

from app.auth import install_auth
from app.auth_store import AuthError, AuthStore
from app.protection import CrowdSec, ProtectionStore, deliver_notifications, sanitize_alert
from app.protection_api import install_protection


ALERT = {"id": 7, "scenario": "crowdsecurity/ssh-bf", "source": {"scope": "Ip", "value": "192.0.2.1", "cn": "ZZ"},
         "events_count": 8, "created_at": "2026-11-13T00:00:00Z",
         "message": "SYNTHETIC-DO-NOT-STORE", "events": [{"password": "SYNTHETIC-DO-NOT-STORE"}]}
DECISION = {"id": 9, "scope": "Ip", "value": "192.0.2.1", "type": "ban", "scenario": "crowdsecurity/ssh-bf",
            "origin": "crowdsec", "duration": "1h"}


def local_client(app, origin="http://127.0.0.1", peer="127.0.0.1"):
    async def wrapper(scope, receive, send):
        scope = dict(scope)
        if scope["type"] == "http":
            scope["client"] = (peer, 50000)
        await app(scope, receive, send)
    return TestClient(wrapper, base_url=origin)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {"CROWDSEC_CREDENTIALS_FILE": "", "CROWDSEC_BOUNCER_KEY_FILE": "",
                                         "CROWDSEC_ALLOW_DELETE": "", "SMTP_HOST": "", "SECERTPROTECT_PUBLIC_ORIGIN": ""})
        self.env.start()
        self.store = ProtectionStore(self.root / "protection.sqlite3")
        self.store.initialize()
        self.client = Mock()
        self.client.snapshot.return_value = ([ALERT], [DECISION])

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_unconfigured_is_not_protected(self):
        state = self.store.status()
        self.assertEqual(state["connection"], "not_configured")
        self.assertFalse(state["bouncer_verified"])
        self.assertTrue(state["stale"])

    def test_sync_idempotence_and_no_raw_evidence(self):
        self.store.sync(client=self.client)
        self.store.review(7, "confirmed", "tester")
        self.store.sync(client=self.client)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.events()[0]["status"], "confirmed")
        self.assertEqual(len(self.store.notifications()), 1)
        self.assertNotIn(b"SYNTHETIC-DO-NOT-STORE", self.store.path.read_bytes())

    def test_failed_sync_preserves_previous_snapshot(self):
        self.store.sync(client=self.client)
        self.client.snapshot.side_effect = RuntimeError("SECRET-not-for-log")
        with self.assertRaises(AuthError):
            self.store.sync(client=self.client)
        self.assertEqual(len(self.store.decisions()), 1)
        self.assertNotIn("SECRET-not-for-log", json.dumps(self.store.audits()))
        self.assertGreater(self.store.status()["next_attempt"], time.time())

    def test_lease_excludes_parallel_sync(self):
        self.store.claim("first")
        with self.assertRaises(AuthError) as error:
            self.store.sync(client=self.client)
        self.assertEqual(error.exception.status_code, 409)
        self.client.snapshot.assert_not_called()

    def test_expired_lease_recovers(self):
        self.store.claim("interrupted")
        with self.store.connect() as c:
            c.execute("UPDATE engine_state SET lease_until=0")
        self.store.sync(client=self.client)
        self.assertEqual(len(self.store.events()), 1)

    def test_delete_requires_explicit_server_opt_in(self):
        self.store.sync(client=self.client)
        with self.assertRaises(AuthError) as error:
            self.store.delete_decision(9, "tester", "synthetic reason", self.client)
        self.assertEqual(error.exception.status_code, 403)
        self.client.delete_decision.assert_not_called()

    def test_failed_delete_keeps_decision(self):
        self.store.sync(client=self.client)
        self.client.delete_decision.side_effect = AuthError("unavailable", 502)
        with patch.dict(os.environ, {"CROWDSEC_ALLOW_DELETE": "true"}):
            with self.assertRaises(AuthError):
                self.store.delete_decision(9, "tester", "synthetic reason", self.client)
        self.assertEqual(len(self.store.decisions()), 1)
        self.assertEqual(self.store.audits()[0]["action"], "decision.delete.failed")

    def test_successful_delete_is_audited(self):
        self.store.sync(client=self.client)
        with patch.dict(os.environ, {"CROWDSEC_ALLOW_DELETE": "true"}):
            self.store.delete_decision(9, "tester", "synthetic reason", self.client)
        self.assertEqual(self.store.decisions(), [])
        self.assertEqual(self.store.audits()[0]["action"], "decision.delete.accepted")

    def test_non_ip_source_and_context_redacted(self):
        row = sanitize_alert({**ALERT, "source": {"scope": "username", "value": "private-user"}})
        self.assertEqual(row["source"], "[已脱敏]")
        self.assertNotIn("private-user", json.dumps(row))

    def test_invalid_ip_value_redacted(self):
        row = sanitize_alert({**ALERT, "source": {"scope": "Ip", "value": "password=sensitive"}})
        self.assertEqual(row["source"], "[已脱敏]")

    def test_missing_smtp_is_not_delivery(self):
        self.store.sync(client=self.client)
        deliver_notifications(self.store)
        self.assertEqual(self.store.notifications()[0]["status"], "pending")

    def test_mail_success_and_retry(self):
        self.store.sync(client=self.client)
        env = {"SMTP_HOST": "smtp.example.invalid", "SMTP_FROM": "sender@example.invalid", "SMTP_TO": "to@example.invalid"}
        with patch.dict(os.environ, env), patch("app.protection.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            deliver_notifications(self.store)
        self.assertEqual(self.store.notifications()[0]["status"], "sent")
        with self.store.connect() as c:
            c.execute("UPDATE notification_outbox SET status='pending',next_attempt=0")
        with patch.dict(os.environ, env), patch("app.protection.smtplib.SMTP_SSL", side_effect=RuntimeError("SECRET")):
            deliver_notifications(self.store)
        self.assertEqual(self.store.notifications()[0]["status"], "retry")
        self.assertNotIn("SECRET", self.store.notifications()[0]["last_error"])

    def test_fifth_attempt_interruption_becomes_failed(self):
        self.store.sync(client=self.client)
        with self.store.connect() as c:
            c.execute("UPDATE notification_outbox SET status='sending',attempts=5,next_attempt=0")
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.invalid"}):
            deliver_notifications(self.store)
        self.assertEqual(self.store.notifications()[0]["status"], "failed")

    def credentials(self, url="http://127.0.0.1:8080"):
        path = self.root / "machine.yaml"
        path.write_text(yaml.safe_dump({"url": url, "login": "test-machine", "password": "synthetic-only"}))
        key = self.root / "reader.secret"
        key.write_text("synthetic-reader")
        return {"CROWDSEC_CREDENTIALS_FILE": str(path), "CROWDSEC_BOUNCER_KEY_FILE": str(key)}

    def test_api_uses_two_distinct_identity_types(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/v1/watchers/login":
                return httpx.Response(200, json={"token": "synthetic-jwt"})
            if request.url.path == "/v1/alerts":
                self.assertEqual(request.headers["Authorization"], "Bearer synthetic-jwt")
                return httpx.Response(200, json=[ALERT])
            self.assertEqual(request.headers["X-Api-Key"], "synthetic-reader")
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, json=[DECISION])
        with patch.dict(os.environ, self.credentials()):
            self.store.sync(client=CrowdSec(httpx.MockTransport(handler)))
        self.assertEqual(calls, ["/v1/watchers/login", "/v1/alerts", "/v1/decisions"])

    def test_redirect_not_followed(self):
        with patch.dict(os.environ, self.credentials()):
            client = CrowdSec(httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "https://evil.invalid"})))
            with self.assertRaises(AuthError):
                client.snapshot()

    def test_remote_plaintext_forbidden(self):
        with patch.dict(os.environ, self.credentials("http://example.com:8080")):
            with self.assertRaises(AuthError):
                CrowdSec()

    def test_response_body_limit(self):
        with patch.dict(os.environ, self.credentials()):
            client = CrowdSec(httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1))))
            with self.assertRaises(AuthError):
                client.authenticate()

    def test_unknown_event_rejected(self):
        with self.assertRaises(AuthError) as error:
            self.store.review(999, "resolved", "tester")
        self.assertEqual(error.exception.status_code, 404)

    def test_fresh_install_has_no_default_password(self):
        with patch.dict(os.environ, {"SECERTPROTECT_BOOTSTRAP_PASSWORD": "", "SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE": ""}):
            with self.assertRaises(AuthError):
                AuthStore(self.root / "new-auth.db").initialize()

    def test_api_auth_csrf_and_admin_enforcement(self):
        env = {"AUTH_DB_PATH": str(self.root / "auth.db"), "PROTECTION_DB_PATH": str(self.store.path),
               "SECERTPROTECT_BOOTSTRAP_PASSWORD": "Synthetic-password-only!", "SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE": ""}
        with patch.dict(os.environ, env):
            app = FastAPI()
            install_auth(app, Jinja2Templates(directory="templates"))
            install_protection(app)
            with local_client(app) as client:
                self.assertEqual(client.get("/api/protection/status").status_code, 401)
                auth = app.state.auth_store
                with auth.connect() as c:
                    c.execute("UPDATE auth_users SET needs_password_change=0")
                user = auth.authenticate("admin", env["SECERTPROTECT_BOOTSTRAP_PASSWORD"], "test")
                token, csrf = auth.create_session(user["id"])
                client.cookies.set("sentinel_session", token)
                self.assertEqual(client.get("/api/protection/status").status_code, 200)
                self.assertEqual(client.post("/api/protection/sync").status_code, 403)
                response = client.post("/api/protection/sync", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.invalid"})
                self.assertEqual(response.status_code, 403)
                response = client.post("/api/protection/sync", headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("password", response.text)

    def test_proxy_origin_secure_cookie_and_remote_peer_rejection(self):
        env = {"AUTH_DB_PATH": str(self.root / "auth-proxy.db"),
               "SECERTPROTECT_BOOTSTRAP_PASSWORD": "Synthetic-password-only!",
               "SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE": "",
               "SECERTPROTECT_PUBLIC_ORIGIN": "https://security.example.test"}
        with patch.dict(os.environ, env):
            app = FastAPI()
            install_auth(app, Jinja2Templates(directory="templates"))
            with local_client(app, "https://security.example.test") as client:
                response = client.get("/login")
                self.assertIn("Secure", response.headers["set-cookie"])
                csrf = client.cookies.get("sentinel_preflight")
                payload = {"username": "admin", "password": env["SECERTPROTECT_BOOTSTRAP_PASSWORD"]}
                response = client.post("/api/auth/login", json=payload,
                                       headers={"X-CSRF-Token": csrf, "Origin": "https://wrong.example.test"})
                self.assertEqual(response.status_code, 403)
                response = client.post("/api/auth/login", json=payload,
                                       headers={"X-CSRF-Token": csrf, "Origin": "https://security.example.test"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Secure", response.headers["set-cookie"])
            with local_client(app, "https://security.example.test", "192.0.2.1") as remote:
                response = remote.get("/login", headers={"X-Forwarded-For": "127.0.0.1"})
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
