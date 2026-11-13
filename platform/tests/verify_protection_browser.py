"""UI acceptance with disposable synthetic stores, never production records."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.auth_store import AuthStore
from app.protection import ProtectionStore

OUTPUT = ROOT / ".test-results" / "secertprotect"
PASSWORD = "Synthetic-browser-only-2026!"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    errors, results = [], []
    with tempfile.TemporaryDirectory(prefix="secertprotect-browser-") as directory:
        temp = Path(directory)
        with patch.dict(os.environ, {"SECERTPROTECT_BOOTSTRAP_PASSWORD": PASSWORD, "SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE": ""}):
            auth = AuthStore(temp / "auth.db")
            auth.initialize()
        with auth.connect() as c:
            c.execute("UPDATE auth_users SET needs_password_change=0")
        store = ProtectionStore(temp / "protection.db")
        store.initialize()
        env = {**os.environ, "PYTHONUTF8": "1", "AUTH_DB_PATH": str(auth.path),
               "PROTECTION_DB_PATH": str(store.path), "ADMISSION_DB_PATH": str(temp / "admission.db"),
               "BACKEND_DB_PATH": str(temp / "backend.db"), "EVENTS_DB_PATH": str(temp / "events.db"),
               "MONITORING_DB_PATH": str(temp / "monitoring.db"), "SETTINGS_DB_PATH": str(temp / "settings.db"),
               "SETTINGS_MASTER_KEY_PATH": str(temp / "settings.key"), "CONNECTOR_RUNS_DB_PATH": str(temp / "runs.db"),
               "CROWDSEC_CREDENTIALS_FILE": "", "CROWDSEC_BOUNCER_KEY_FILE": "", "SMTP_HOST": "",
               "SECERTPROTECT_PUBLIC_ORIGIN": "", "DATA_SOURCE": "sqlite",
               "CONNECTOR_NETWORK_ENABLED": "false", "VERIFICATION_NETWORK_ENABLED": "false"}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (temp / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                        "--port", str(port), "--no-proxy-headers"],
                                       cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(origin + "/login", timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="msedge", headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce", accept_downloads=True)
                    page = context.new_page()
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(origin + "/login")
                    expect(page.get_by_text("SecertProtect", exact=True)).to_be_visible()
                    for width, height in [(1440, 1000), (1024, 768), (768, 1024), (390, 844), (320, 720)]:
                        page.set_viewport_size({"width": width, "height": height})
                        expect(page.get_by_role("heading", name="Samlab 安全管理")).to_be_visible()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), ("login", width)
                        assert page.locator(".visual-brand img").evaluate("(img) => img.complete && img.naturalWidth > 0")
                        assert page.locator(".auth-heading").evaluate(
                            "(heading) => heading.getBoundingClientRect().bottom <= document.querySelector('.auth-form').getBoundingClientRect().top")
                        button = page.get_by_role("button", name="登录", exact=True)
                        button.scroll_into_view_if_needed()
                        expect(button).to_be_in_viewport()
                        page.screenshot(path=str(OUTPUT / f"login-{width}.png"), full_page=True)
                    results.append("legacy split login at five viewports: no overflow, form reachable, logo loaded")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.screenshot(path=str(OUTPUT / "login-desktop.png"), full_page=True)
                    page.get_by_role("button", name="登录", exact=True).click()
                    expect(page.get_by_text("请输入用户名", exact=True)).to_be_visible()
                    expect(page.get_by_text("请输入密码", exact=True)).to_be_visible()
                    assert page.url == origin + "/login"
                    page.get_by_placeholder("请输入用户名").fill("admin")
                    page.get_by_placeholder("请输入密码", exact=True).fill("Synthetic-wrong-password")
                    page.get_by_role("button", name="登录", exact=True).click()
                    expect(page.locator(".auth-error")).to_be_visible()
                    expect(page.get_by_role("button", name="登录", exact=True)).to_be_enabled()
                    assert page.url == origin + "/login"
                    results.append("login required fields and invalid credentials remain on login with inline errors")
                    page.get_by_placeholder("请输入用户名").fill("admin")
                    page.get_by_placeholder("请输入密码", exact=True).fill(PASSWORD)
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.wait_for_url(origin + "/")
                    expect(page.get_by_role("button", name="同步引擎", exact=True)).to_be_disabled()
                    expect(page.get_by_text("引擎未配置").first).to_be_visible()
                    expect(page.get_by_text("暂无引擎事件")).to_be_visible()
                    assert page.locator(".protect-metrics strong").all_text_contents() == ["—", "—", "—", "0"]
                    results.append("unconfigured engine does not invent counters")
                    for width, height in [(1440, 1000), (1024, 768), (768, 1024), (390, 844), (320, 720)]:
                        page.set_viewport_size({"width": width, "height": height})
                        for route in ("/", "/attacks", "/decisions", "/engine", "/security-audit", "/admission"):
                            page.goto(origin + route)
                            page.wait_for_load_state("networkidle")
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                            images = page.locator("img").evaluate_all("xs => xs.filter(x => x.getClientRects().length && (!x.complete || !x.naturalWidth)).map(x => x.src)")
                            assert not images, images
                            if route in {"/", "/engine"}:
                                page.screenshot(path=str(OUTPUT / f"{route.strip('/') or 'overview'}-{width}.png"), full_page=True)
                        results.append(f"six routes at {width}x{height}: no overflow, assets loaded")
                    fixture = Mock()
                    fixture.snapshot.return_value = ([{"id": 101, "scenario": "synthetic/ssh-bf", "events_count": 8,
                                                       "source": {"scope": "Ip", "value": "192.0.2.22"}, "simulated": True,
                                                       "message": "PRIVATE_SYNTHETIC_VALUE"}],
                                                     [{"id": 102, "scope": "Ip", "value": "192.0.2.22", "type": "ban", "duration": "1h", "scenario": "synthetic/ssh-bf"}])
                    store.sync("synthetic-test", fixture)
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.goto(origin + "/attacks")
                    expect(page.get_by_role("cell", name="synthetic/ssh-bf", exact=True)).to_be_visible()
                    assert "PRIVATE_SYNTHETIC_VALUE" not in page.content()
                    page.get_by_role("button", name="详情", exact=True).click()
                    drawer = page.get_by_role("dialog", name="攻击事件详情")
                    drawer.locator(".el-select").filter(has=page.get_by_role("combobox", name="更新处置状态")).click()
                    page.get_by_role("option", name="已确认", exact=True).click()
                    drawer.get_by_role("button", name="保存处置").click()
                    expect(drawer).not_to_be_visible()
                    expect(page.get_by_role("cell", name="已确认", exact=True)).to_be_visible()
                    assert store.events()[0]["status"] == "confirmed"
                    with page.expect_download() as downloaded:
                        page.get_by_role("button", name="导出脱敏记录").click()
                    downloaded.value.save_as(OUTPUT / "synthetic-events.json")
                    exported = json.loads((OUTPUT / "synthetic-events.json").read_text())
                    assert len(exported["items"]) == 1 and "PRIVATE_SYNTHETIC_VALUE" not in str(exported)
                    page.get_by_role("textbox", name="搜索事件").fill("not-present")
                    expect(page.get_by_text("暂无记录", exact=True)).to_be_visible()
                    results.append("synthetic event detail, persisted review, filtering and safe export")
                    page.goto(origin + "/decisions")
                    expect(page.get_by_role("button", name="申请解封")).to_be_disabled()
                    page.goto(origin + "/security-audit")
                    expect(page.get_by_role("cell", name="更新事件处置", exact=True)).to_be_visible()
                    page.get_by_role("tab", name="邮件投递").click()
                    expect(page.get_by_role("cell", name="待发送", exact=True)).to_be_visible()
                    results.append("read-only decisions and unsent notification states")
                    assert not errors, errors
                    browser.close()
            finally:
                if os.name == "nt" and process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, check=False)
                elif process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    report = {"synthetic_only": True, "real_crowdsec_integrated": False, "production_changed": False,
              "results": results, "page_errors": errors}
    (OUTPUT / "browser-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
