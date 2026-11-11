"""Exercise real candidate HTTP services with disposable, synthetic identities."""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
SENTINEL = ROOT / "sentinel"
sys.path.insert(0, str(SENTINEL))
from app.auth_store import AuthStore, password_hash

REMOTE = "/opt/sub2api/work/samlab-admission-20261111T071145Z"
SSH = ["ssh", "-i", "C:/Users/samlyx/.ssh/id_ed25519_conversation_research",
       "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "ubuntu@43.162.124.184"]
COMPOSE = f"sudo docker compose --env-file {REMOTE}/runtime.env -f {REMOTE}/isolated.compose.yml"
BUSINESS = "http://127.0.0.1:18080"
SECURITY = "http://127.0.0.1:18011"


def remote(command):
    result = subprocess.run(SSH + [command], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("isolated remote operation failed: " + result.stderr[-1500:])
    return result.stdout


def require(response, status=200):
    if response.status_code != status:
        # Never include a successful token or activation response in a failure report.
        raise AssertionError(f"{response.request.method} {response.request.url.path}: "
                             f"expected {status}, got {response.status_code}; {response.text[:400]}")
    return response.json()


def wait_for(predicate, seconds=40):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError("isolated workflow timed out")


def main():
    started_at = datetime.now(timezone.utc).isoformat()
    OUTPUT = ROOT / "test-results"
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "isolated-http-results.json").write_text(json.dumps({
        "started_at": started_at, "run_completed": False, "synthetic_only": True,
        "production_changed": False, "real_production_acceptance": False,
        "checks": [],
    }, indent=2), encoding="utf-8")
    (OUTPUT / "isolated-http-progress.json").write_text(json.dumps({
        "started_at": started_at, "run_completed": False, "synthetic_only": True,
        "production_changed": False, "checks_completed": [], "failure_type": None,
    }, indent=2), encoding="utf-8")
    config = dict(line.split("=", 1) for line in remote(f"cat {REMOTE}/runtime.env").splitlines() if "=" in line)
    security_password = secrets.token_hex(24)
    member_password = secrets.token_hex(12)
    suffix = secrets.token_hex(4)
    results = []
    try:
        remote(COMPOSE + " up -d main worker")
    except Exception:
        remote(COMPOSE + " stop")
        raise
    image_id = remote("sudo docker inspect --format '{{.Image}}' samlab-admission-isolated-main-1").strip()
    processes = []
    with tempfile.TemporaryDirectory(prefix="samlab-isolated-") as directory:
        temp = Path(directory)
        auth = AuthStore(temp / "auth.sqlite3")
        auth.initialize()
        with auth.connect() as connection:
            connection.execute("UPDATE auth_users SET password_hash=?,needs_password_change=0 WHERE username='admin'",
                               (password_hash(security_password),))
        env = {**os.environ, "PYTHONUTF8": "1", "DATA_SOURCE": "sqlite",
               "AUTH_DB_PATH": str(temp / "auth.sqlite3"), "ADMISSION_DB_PATH": str(temp / "admission.sqlite3"),
               "BACKEND_DB_PATH": str(temp / "backend.sqlite3"), "EVENTS_DB_PATH": str(temp / "events.sqlite3"),
               "MONITORING_DB_PATH": str(temp / "monitoring.sqlite3"), "SETTINGS_DB_PATH": str(temp / "settings.sqlite3"),
               "SETTINGS_MASTER_KEY_PATH": str(temp / "settings.key"),
               "CONNECTOR_RUNS_DB_PATH": str(temp / "runs.sqlite3"),
               "SAMLAB_INTEGRATION_SECRET": config["SAMLAB_INTEGRATION_SECRET"],
               "CONNECTOR_NETWORK_ENABLED": "false", "VERIFICATION_NETWORK_ENABLED": "false"}
        with (OUTPUT / "sentinel-isolated.log").open("w", encoding="utf-8") as log:
            try:
                processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                                                   "--host", "127.0.0.1", "--port", "18011"],
                                                  cwd=SENTINEL, env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen(
                    SSH[:-1] + ["-C", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                                "-N", "-R", "127.0.0.1:18011:127.0.0.1:18011",
                                "-L", "127.0.0.1:18080:127.0.0.1:18080", SSH[-1]],
                    stdout=log, stderr=log))
                # Fresh connections avoid racing the server's idle keepalive timeout during SSH operations.
                with httpx.Client(base_url=SECURITY, timeout=10, limits=httpx.Limits(max_keepalive_connections=0)) as security, \
                        httpx.Client(base_url=BUSINESS, timeout=10, limits=httpx.Limits(max_keepalive_connections=0)) as business:
                    def ready():
                        try:
                            return security.get("/login").status_code in (200, 303) and business.get("/health").status_code == 200
                        except httpx.HTTPError:
                            return False
                    wait_for(ready)
                    require(security.post("/api/auth/login", json={"username": "admin", "password": security_password},
                                          headers={"X-CSRF-Token": security.cookies.get("sentinel_preflight", "")}))
                    csrf = require(security.get("/api/auth/me"))["csrf_token"]

                    def review(request_id, decision="approved", **extra):
                        return require(security.post(f"/api/lab/admin/requests/{request_id}/review",
                                                     json={"decision": decision, **extra}, headers={"X-CSRF-Token": csrf}))

                    def dashboard():
                        return require(security.get("/api/lab/admin/dashboard"))

                    def await_received(request_id):
                        wait_for(lambda: any(r["request_id"] == request_id for r in dashboard()["requests"]))

                    def application_status(payload):
                        return require(business.post("/api/v1/auth/lab-application/status",
                                                     json={"request_id": payload["request_id"], "email": payload["email"],
                                                           "password": member_password}))["data"]

                    admin = require(business.post("/api/v1/auth/login",
                                                   json={"email": config["ADMIN_EMAIL"], "password": config["ADMIN_PASSWORD"]}))["data"]
                    admin_headers = {"Authorization": "Bearer " + admin["access_token"]}
                    assert not require(business.get("/api/v1/auth/me", headers=admin_headers))["data"]["lab_business_access"]
                    assert business.get("/api/v1/keys", headers=admin_headers).status_code == 403
                    results.append("unmapped management administrator cannot bypass business admission")
                    compliance = require(business.get("/api/v1/admin/compliance", headers=admin_headers))["data"]
                    require(business.post("/api/v1/admin/compliance/accept", headers=admin_headers,
                                          json={"phrase": compliance["ack_phrase_en"], "language": "en"}))
                    group = require(business.post("/api/v1/admin/groups", headers=admin_headers, json={
                        "name": "isolated-" + suffix, "platform": "openai", "rate_multiplier": 1,
                        "is_exclusive": False, "subscription_type": "standard",
                    }))["data"]
                    # Test setup uses only this fresh test database, not production settings.
                    remote(f"sudo docker compose --env-file {REMOTE}/runtime.env -f {REMOTE}/isolated.compose.yml exec -T postgres "
                           "psql -U labtest -d labtest -c \"INSERT INTO settings(key,value) VALUES ('registration_enabled','true') "
                           "ON CONFLICT(key) DO UPDATE SET value='true';\"")
                    # Restart only candidate main to discard its settings cache.
                    remote(COMPOSE + " restart main")
                    wait_for(ready)
                    invalid = business.post("/api/v1/auth/register", json={
                        "request_id": "invalid-fields-request-0001", "username": "x",
                        "email": "", "password": "123"})
                    invalid_fields = require(invalid, 422)["field_errors"]
                    assert set(invalid_fields) >= {"username", "email", "password"}
                    assert "person_no" not in invalid_fields
                    results.append("invalid registration returns explicit errors for each field")
                    payload = {"request_id": "isolated-request-" + secrets.token_hex(8),
                               "username": "member_" + suffix,
                               "email": f"member-{suffix}@example.edu", "password": member_password}
                    roster = require(security.post("/api/lab/admin/roster/preview",
                        content=f"邮箱,用户名\n{payload['email']},实验室成员\n".encode("utf-8-sig"),
                        headers={"X-CSRF-Token": csrf, "Content-Type": "text/csv"}))
                    require(security.post("/api/lab/admin/roster/commit", json={"batch_id": roster["batch_id"]},
                                          headers={"X-CSRF-Token": csrf}))
                    imported_member = next(m for m in dashboard()["members"] if m["email"] == payload["email"])
                    assert imported_member["account_id"] is None
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        first_codes = list(pool.map(lambda _: business.post("/api/v1/auth/register", json=payload).status_code, range(2)))
                    assert first_codes == [202, 202], first_codes
                    count = remote(COMPOSE + " exec -T postgres psql -U labtest -d labtest -At -c "
                                   f"\"SELECT count(*) FROM lab_registration_requests WHERE request_id='{payload['request_id']}';\"")
                    assert count.strip() == "1"
                    await_received(payload["request_id"])
                    assert application_status(payload)["decision"] == "pending"
                    assert next(r for r in dashboard()["requests"] if r["request_id"] == payload["request_id"])["roster_match"]
                    assert business.post("/api/v1/auth/login", json={"email": payload["email"], "password": member_password}).status_code == 403
                    results.append("pending applicant cannot log in")
                    approved = review(payload["request_id"])
                    assert approved["member_id"] == imported_member["id"]
                    results.append("CSV-imported email matches registration without a student number; import alone never activates an unverified applicant")
                    wait_for(lambda: any(m["id"] == approved["member_id"] and m["sync_state"] == "active"
                                         for m in dashboard()["members"]))
                    assert application_status(payload)["ready"]
                    member = require(business.post("/api/v1/auth/login",
                                                    json={"email": payload["email"], "password": member_password}))["data"]
                    member_headers = {"Authorization": "Bearer " + member["access_token"]}
                    assert require(business.get("/api/v1/auth/me", headers=member_headers))["data"]["concurrency"] == 5
                    results.append("self-registered laboratory account has concurrency fixed at five")
                    key = require(business.post("/api/v1/keys", headers=member_headers,
                                                json={"name": "synthetic-key-" + suffix, "group_id": group["id"]}))["data"]
                    key_headers = {"Authorization": "Bearer " + key["key"]}
                    require(business.get("/v1/sub2api/billing", headers=key_headers))
                    wait_for(lambda: any(m["id"] == approved["member_id"] and m["sync_state"] == "active"
                                         for m in dashboard()["members"]))
                    results.append("one approval grants whitelist and activates original account without resubmission")
                    require(business.post("/api/v1/auth/register", json=payload), 202)
                    results.append("concurrent duplicate registration returns original account")
                    assert business.post("/api/v1/auth/lab-application/status",
                                         json={"request_id": payload["request_id"], "email": config["ADMIN_EMAIL"],
                                               "password": config["ADMIN_PASSWORD"]}).status_code == 401
                    assert business.get("/api/v1/auth/oauth/github/start").status_code == 403
                    results.append("own-progress isolation and alternate OAuth registration denied")
                    remote(COMPOSE + " stop worker")
                    time.sleep(17)
                    assert business.get("/api/v1/auth/me", headers=member_headers).status_code in (401, 403)
                    results.append("worker outage denies cached approval after the 15-second lease")
                    remote(COMPOSE + " start worker")
                    wait_for(lambda: business.get("/api/v1/auth/me", headers=member_headers).status_code == 200,
                             seconds=80)
                    results.append("worker restart resumes durable synchronization without resubmission")
                    require(security.post(f"/api/lab/admin/members/{approved['member_id']}/revoke",
                                          json={"note": "isolated revocation test", "confirmed": True},
                                          headers={"X-CSRF-Token": csrf}))
                    wait_for(lambda: business.get("/api/v1/auth/me", headers=member_headers).status_code in (401, 403))
                    assert business.post("/api/v1/auth/refresh", json={"refresh_token": member["refresh_token"]}).status_code >= 400
                    assert business.get("/v1/sub2api/billing", headers=key_headers).status_code in (401, 403)
                    results.append("revocation invalidates old access and refresh sessions")
                    results.append("revoked account cannot use its existing API key at the gateway")
                    # A second synthetic identity validates rejection without granting access.
                    rejected = {**payload, "request_id": "isolated-rejected-" + secrets.token_hex(8),
                                "username": "rejected_" + suffix,
                                "email": f"rejected-{suffix}@example.edu"}
                    require(business.post("/api/v1/auth/register", json=rejected), 202)
                    await_received(rejected["request_id"])
                    review(rejected["request_id"], "rejected")
                    wait_for(lambda: application_status(rejected)["decision"] == "rejected")
                    assert not application_status(rejected)["ready"]
                    results.append("rejected application cannot activate")
                    # The original administrator workflow creates directly, without a review action.
                    direct_email = f"direct-{suffix}@example.edu"
                    direct_roster = require(security.post("/api/lab/admin/roster/preview",
                        content=f"email\n{direct_email}\n".encode(),
                        headers={"X-CSRF-Token": csrf, "Content-Type": "text/csv"}))
                    require(security.post("/api/lab/admin/roster/commit", json={"batch_id": direct_roster["batch_id"]},
                                          headers={"X-CSRF-Token": csrf}))
                    direct = require(business.post("/api/v1/admin/users", headers=admin_headers, json={
                        "email": direct_email, "password": member_password, "username": "直接创建用户",
                        "role": "user", "balance": 0, "concurrency": 999,
                    }))["data"]
                    assert direct["concurrency"] == 5 and direct["lab_concurrency_fixed"]
                    invalid_limit = require(business.put(f"/api/v1/admin/users/{direct['id']}", headers=admin_headers,
                                                        json={"concurrency": 9}), 400)
                    assert "5" in invalid_limit["message"]
                    require(business.post("/api/v1/admin/users/batch-limits", headers=admin_headers,
                                          json={"user_ids": [direct["id"]], "concurrency": 9}), 400)
                    assert require(business.get(f"/api/v1/admin/users/{direct['id']}", headers=admin_headers))["data"]["concurrency"] == 5
                    results.append("administrator creation clamps requested 999 concurrency to five; individual and batch overrides are rejected")
                    direct_request_id = f"admin-account-{direct['id']}"
                    await_received(direct_request_id)
                    wait_for(lambda: any(m["account_id"] == str(direct["id"]) and m["sync_state"] == "active"
                                         for m in dashboard()["members"]))
                    direct_row = next(r for r in dashboard()["requests"] if r["request_id"] == direct_request_id)
                    assert direct_row["status"] == "approved" and direct_row["source"] == "admin_direct"
                    direct_login = require(business.post("/api/v1/auth/login", json={
                        "email": direct_email, "password": member_password,
                    }))["data"]
                    direct_headers = {"Authorization": "Bearer " + direct_login["access_token"]}
                    require(business.get("/api/v1/auth/me", headers=direct_headers))
                    assert business.post("/api/v1/admin/users", headers=direct_headers, json={
                        "email": f"forbidden-{suffix}@example.edu", "password": member_password,
                    }).status_code == 403
                    results.append("original admin creation automatically grants whitelist without any review; ordinary user cannot create users")
                    reset_password = secrets.token_hex(14)
                    require(business.put(f"/api/v1/admin/users/{direct['id']}", headers=admin_headers,
                                         json={"password": reset_password}))
                    assert business.post("/api/v1/auth/login", json={
                        "email": direct_email, "password": member_password,
                    }).status_code == 401
                    require(business.post("/api/v1/auth/login", json={
                        "email": direct_email, "password": reset_password,
                    }))
                    assert business.get("/api/v1/auth/me", headers=direct_headers).status_code == 401
                    results.append("original administrator password reset works; old password and old access token are rejected")
                    require(business.put(f"/api/v1/admin/users/{direct['id']}", headers=admin_headers,
                                         json={"status": "disabled"}))
                    time.sleep(4)
                    assert require(business.get(f"/api/v1/admin/users/{direct['id']}",
                                                headers=admin_headers))["data"]["status"] == "disabled"
                    assert business.post("/api/v1/auth/login", json={
                        "email": direct_email, "password": reset_password,
                    }).status_code == 403
                    require(business.put(f"/api/v1/admin/users/{direct['id']}", headers=admin_headers,
                                         json={"status": "active"}))
                    require(business.post("/api/v1/auth/login", json={
                        "email": direct_email, "password": reset_password,
                    }))
                    results.append("original admin disable and enable are preserved; synchronization cannot silently reenable a disabled account")
                    delegated = {"request_id": "isolated-delegated-" + secrets.token_hex(8),
                                 "username": "delegated_" + suffix,
                                 "email": f"delegated-{suffix}@example.edu"}
                    require(business.post("/api/v1/admin/lab-applications", headers=admin_headers, json=delegated))
                    require(business.post("/api/v1/admin/lab-applications", headers=admin_headers, json=delegated))
                    await_received(delegated["request_id"])
                    assert next(r for r in dashboard()["requests"] if r["request_id"] == delegated["request_id"])["status"] == "approved"
                    def invite_ready():
                        data = require(business.get("/api/v1/admin/lab-applications", headers=admin_headers))["data"]
                        return any(row["request_id"] == delegated["request_id"] and row["approved"] for row in data["items"])
                    wait_for(invite_ready)
                    issued = require(business.post(f"/api/v1/admin/lab-applications/{delegated['request_id']}/invitation",
                                                    headers=admin_headers))["data"]
                    token = issued["activation_path"].split("#token=", 1)[1]
                    require(business.post("/api/v1/auth/lab-activate", json={"token": token, "password": member_password}))
                    assert business.post("/api/v1/auth/lab-activate", json={"token": token, "password": member_password}).status_code == 403
                    delegated_login = require(business.post("/api/v1/auth/login",
                                                           json={"email": delegated["email"], "password": member_password}))["data"]
                    require(business.post("/api/v1/admin/lab-applications", headers=admin_headers, json=delegated))
                    require(business.post("/api/v1/admin/lab-applications", headers=admin_headers, json={
                        key: payload[key] for key in ("request_id", "username", "email")}))
                    assert business.post(f"/api/v1/admin/lab-applications/{payload['request_id']}/invitation",
                                         headers=admin_headers).status_code == 409
                    results.append("optional admin invitation is idempotent, autoapproved, and activates only once")
                    delegated_request = next(r for r in dashboard()["requests"] if r["request_id"] == delegated["request_id"])
                    require(security.post(f"/api/lab/admin/members/{delegated_request['member_id']}/renew",
                                          json={"expires_at": time.time() + 10}, headers={"X-CSRF-Token": csrf}))
                    time.sleep(12)
                    assert business.get("/api/v1/auth/me", headers={
                        "Authorization": "Bearer " + delegated_login["access_token"]}).status_code in (401, 403)
                    results.append("whitelist expiry denies an already-issued session without a new application")
                    (OUTPUT / "isolated-http-results.json").write_text(json.dumps({
                        "started_at": started_at, "run_completed": False, "candidate_image_id": image_id,
                        "synthetic_only": True, "production_changed": False, "real_production_acceptance": False,
                        "checks": results, "browser_verified": False,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    # Browser registration and progress are tested on the same real candidate.
                    from playwright.sync_api import expect, sync_playwright
                    with sync_playwright() as p:
                        browser = p.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors, payment_requests = [], []
                        page.on("pageerror", lambda e: errors.append(str(e)))
                        page.on("request", lambda request: payment_requests.append(request.url.split("?")[0])
                                if "airwallex.com/" in request.url else None)
                        page.goto(BUSINESS + "/register", wait_until="commit", timeout=60000)
                        try:
                            page.locator("#lab-username").wait_for(timeout=90000)
                        finally:
                            page.screenshot(path=str(OUTPUT / "samlab-register-diagnostic.png"), full_page=True)
                            (OUTPUT / "browser-diagnostic.json").write_text(json.dumps({
                                "url": page.url, "text": page.locator("body").inner_text()[:3000],
                                "page_errors": errors,
                            }, ensure_ascii=False, indent=2), encoding="utf-8")
                        page.screenshot(path=str(OUTPUT / "samlab-register-desktop.png"), full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.screenshot(path=str(OUTPUT / "samlab-register-mobile.png"), full_page=True)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        # Wait for public policy, not only the form's initial render.
                        expect(page.get_by_role("button", name="提交入组申请", exact=True)).to_be_enabled(timeout=30000)
                        assert page.locator("#lab-username").evaluate(
                            "el => { new RegExp(el.pattern, 'v'); el.value = '-invalid'; return !el.checkValidity() }")
                        page.get_by_label("用户名", exact=True).fill("browser_" + suffix)
                        expect(page.get_by_label("学号", exact=True)).to_have_count(0)
                        page.get_by_label("邮箱", exact=True).fill(f"browser-{suffix}@example.edu")
                        page.locator("#lab-password").fill(member_password)
                        # Registration limiter is deliberate: previous assertions consumed its five-request budget.
                        time.sleep(61)
                        page.get_by_role("button", name="提交入组申请", exact=True).click()
                        expect(page.get_by_text("申请已保存，等待管理员审批。", exact=True)).to_be_visible(timeout=15000)
                        reference = json.loads(page.evaluate("localStorage.getItem('samlab_application_reference')"))
                        assert not page.evaluate("(password) => Object.values(localStorage).some(v => v.includes(password))", member_password)
                        await_received(reference["request_id"])
                        review(reference["request_id"])
                        wait_for(lambda: any(r["request_id"] == reference["request_id"] and
                                             any(m["id"] == r["member_id"] and m["sync_state"] == "active"
                                                 for m in dashboard()["members"]) for r in dashboard()["requests"]))
                        page.get_by_role("button", name="查询进度", exact=True).click()
                        expect(page.get_by_text("账号已开通", exact=True)).to_be_visible(timeout=15000)
                        page.screenshot(path=str(OUTPUT / "samlab-approved-mobile.png"), full_page=True)
                        assert not errors, errors
                        assert not payment_requests, "payment SDK loaded in credential registration page"
                        from verify_admin_browser import check_admin_forms
                        check_admin_forms(browser, config, direct_email)
                        browser.close()
                    results.append("real browser submits once, shows pending, then approved account without password storage")
                    results.append("registration UI renders at desktop and mobile without page errors or overflow")
                    results.append("credential registration page does not load Airwallex third-party payment scripts")
                    results.append("original administrator create and password-edit forms remain accessible; optional invitation UI no longer requests approval")
                    main_log = remote("sudo docker logs samlab-admission-isolated-main-1 2>&1")
                    sentinel_log = (OUTPUT / "sentinel-isolated.log").read_text(encoding="utf-8")
                    for secret in (config["SAMLAB_INTEGRATION_SECRET"], config["ADMIN_PASSWORD"], member_password, reset_password, token):
                        assert secret not in main_log and secret not in sentinel_log, "raw secret found in application log"
                    results.append("test passwords, signing secret and invitation token are absent from application logs")
            finally:
                (OUTPUT / "isolated-http-progress.json").write_text(json.dumps({
                    "started_at": started_at, "candidate_image_id": image_id,
                    "synthetic_only": True, "production_changed": False, "checks_completed": results,
                    "run_completed": sys.exc_info()[0] is None,
                    "failure_type": sys.exc_info()[0].__name__ if sys.exc_info()[0] else None,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                # Only the dedicated candidate services are stopped; named test volumes remain recoverable.
                try:
                    remote(COMPOSE + " stop")
                finally:
                    for process in reversed(processes):
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
    evidence = {"synthetic_only": True, "production_changed": False, "real_production_acceptance": False,
                "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(), "run_completed": True,
                "candidate_image_id": image_id,
                "transport": "real HTTP between candidate Go server, independent Go worker and Python Sentinel over SSH",
                "checks": results}
    (OUTPUT / "isolated-http-results.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Stop only the isolated candidate compose project.")
    arguments = parser.parse_args()
    if arguments.stop:
        remote(COMPOSE + " stop")
        print("Isolated candidate services stopped; production was not changed.")
    else:
        main()
