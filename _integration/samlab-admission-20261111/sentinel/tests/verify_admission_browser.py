"""Real browser acceptance against disposable synthetic databases only."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth_store import AuthStore, password_hash
from app.samlab_admission import SamlabAdmissionStore


PASSWORD = "Browser-test-only-2026!"
OUTPUT = ROOT / ".test-results" / "samlab"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    with tempfile.TemporaryDirectory(prefix="samlab-browser-") as temporary:
        temp = Path(temporary)
        auth = AuthStore(temp / "auth.sqlite3")
        auth.initialize()
        with auth.connect() as c:
            c.execute("UPDATE auth_users SET password_hash=?,needs_password_change=0 WHERE username='admin'",
                      (password_hash(PASSWORD),))
            root_id = c.execute("SELECT id FROM auth_users WHERE username='admin'").fetchone()[0]
        operator = auth.create_admin(root_id, "operator01", PASSWORD)
        with auth.connect() as c:
            c.execute("UPDATE auth_users SET needs_password_change=0 WHERE id=?", (operator["id"],))
        admission = SamlabAdmissionStore(temp / "admission.sqlite3")
        admission.initialize()
        admission.submit("synthetic-request-01", "student01", "2026012345", "student01@example.edu")
        admission.submit("synthetic-request-02", "student02", "2026012346", "student02@example.edu")
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "AUTH_DB_PATH": str(temp / "auth.sqlite3"), "ADMISSION_DB_PATH": str(temp / "admission.sqlite3"),
               "BACKEND_DB_PATH": str(temp / "backend.sqlite3"), "EVENTS_DB_PATH": str(temp / "events.sqlite3"),
               "MONITORING_DB_PATH": str(temp / "monitoring.sqlite3"), "SETTINGS_DB_PATH": str(temp / "settings.sqlite3"),
               "SETTINGS_MASTER_KEY_PATH": str(temp / "settings.master.key"),
               "CONNECTOR_RUNS_DB_PATH": str(temp / "runs.sqlite3"), "SAMLAB_INTEGRATION_SECRET": "",
               "DATA_SOURCE": "sqlite", "CONNECTOR_NETWORK_ENABLED": "false", "VERIFICATION_NETWORK_ENABLED": "false"}
        with (temp / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Isolated server exited; inspect temporary log.")
                    try:
                        with urllib.request.urlopen(origin + "/login", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated server startup timeout.")
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="msedge", headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin + "/login")
                    expect(page.get_by_role("link", name="实验室准入申请")).to_have_count(0)
                    expect(page.get_by_text("注册并登录", exact=True)).to_have_count(0)
                    page.screenshot(path=str(OUTPUT / "admin-login.png"), full_page=True)
                    page.get_by_placeholder("请输入用户名").fill("admin")
                    page.get_by_placeholder("请输入密码", exact=True).fill(PASSWORD)
                    page.get_by_role("button", name="登录", exact=True).click()
                    expect(page.get_by_role("heading", name="Samlab 人员准入")).to_be_visible()
                    expect(page.get_by_text("student01@example.edu", exact=True)).to_be_visible()
                    page.screenshot(path=str(OUTPUT / "requests-desktop.png"), full_page=True)
                    results.append("admin login; no public signup or application entry")
                    expect(page.get_by_role("columnheader", name="学号", exact=True)).to_have_count(0)
                    page.get_by_role("button", name="导入名单", exact=True).click()
                    import_dialog = page.get_by_role("dialog", name="导入实验室名单", exact=True)
                    with page.expect_download() as download:
                        import_dialog.get_by_role("button", name="下载 CSV 模板", exact=True).click()
                    assert download.value.suggested_filename == "samlab-roster.csv"
                    import_dialog.get_by_label("名单文件", exact=True).set_input_files({
                        "name": "invalid.csv", "mimeType": "text/csv",
                        "buffer": "邮箱,用户名\ninvalid-email,错误数据\n".encode("utf-8-sig"),
                    })
                    expect(import_dialog.get_by_role("cell").filter(has_text="邮箱格式无效或超过 180 个字符。")).to_be_visible()
                    expect(import_dialog.get_by_role("button", name="确认导入", exact=True)).to_be_disabled()
                    import_dialog.get_by_label("名单文件", exact=True).set_input_files({
                        "name": "lab-roster.csv", "mimeType": "text/csv",
                        "buffer": "邮箱,用户名,有效期\nroster-browser@example.edu,导入人员,\nroster-browser@example.edu,重复行,\n".encode("utf-8-sig"),
                    })
                    expect(import_dialog.get_by_role("button", name="确认导入", exact=True)).to_be_enabled()
                    page.screenshot(path=str(OUTPUT / "roster-import-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "roster-import-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Import dialog mobile overflow"
                    import_dialog.get_by_role("button", name="确认导入", exact=True).click()
                    expect(import_dialog).not_to_be_visible()
                    expect(page.get_by_role("row").filter(has_text="roster-browser@example.edu")).to_be_visible()
                    expect(page.get_by_text("待注册或确认申请", exact=True)).to_be_visible()
                    results.append("CSV template download, invalid-row rejection, duplicate preview and actual import; desktop/mobile fit; no fake account opening")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("tab", name="注册申请", exact=False).click()
                    page.get_by_role("row").filter(has_text="student01@example.edu").get_by_role("button", name="审批").click()
                    dialog = page.get_by_role("dialog", name="注册准入审批")
                    expect(dialog.get_by_role("radio")).to_have_count(2)
                    expect(dialog.get_by_role("textbox")).to_have_count(0)
                    dialog.get_by_text("同意", exact=True).click()
                    dialog.get_by_role("button", name="确认审批").click()
                    expect(dialog).not_to_be_visible()
                    page.get_by_role("tab", name="白名单", exact=True).click()
                    expect(page.get_by_text("待平台开通", exact=True)).to_be_visible()
                    expect(page.get_by_text("平台已回执开通", exact=True)).to_have_count(0)
                    page.screenshot(path=str(OUTPUT / "whitelist-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "whitelist-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflow"
                    results.append("one approval adds whitelist; no fake platform account success")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("tab", name="注册申请", exact=False).click()
                    page.get_by_role("row").filter(has_text="student02@example.edu").get_by_role("button", name="审批").click()
                    dialog.get_by_text("不同意", exact=True).click()
                    dialog.get_by_role("button", name="确认审批").click()
                    expect(dialog).not_to_be_visible()
                    expect(page.get_by_role("row").filter(has_text="student02@example.edu").get_by_text("不同意", exact=True)).to_be_visible()
                    results.append("rejection without free-text note")
                    page.goto(origin + "/administrators")
                    expect(page.get_by_role("heading", name="管理员", exact=True, level=2)).to_be_visible()
                    page.get_by_role("textbox", name="新管理员用户名").fill("createdbyui")
                    page.get_by_label("新管理员初始密码", exact=True).fill(PASSWORD)
                    page.get_by_role("button", name="创建管理员", exact=True).click()
                    expect(page.get_by_role("row").filter(has_text="createdbyui")).to_be_visible()
                    expect(page.get_by_label("新管理员初始密码", exact=True)).to_have_value("")
                    page.screenshot(path=str(OUTPUT / "admins-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "admins-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Admin mobile page overflow"
                    results.append("superadmin creates ordinary admin; password cleared; desktop/mobile fit")
                    regular_context = browser.new_context()
                    regular = regular_context.new_page()
                    regular.on("pageerror", lambda error: errors.append(str(error)))
                    regular.goto(origin + "/login")
                    regular.get_by_placeholder("请输入用户名").fill("operator01")
                    regular.get_by_placeholder("请输入密码", exact=True).fill(PASSWORD)
                    regular.get_by_role("button", name="登录", exact=True).click()
                    expect(regular.get_by_role("heading", name="Samlab 人员准入")).to_be_visible()
                    expect(regular.get_by_role("menuitem", name="管理员", exact=True)).to_have_count(0)
                    regular.goto(origin + "/administrators")
                    expect(regular.get_by_role("heading", name="Samlab 人员准入")).to_be_visible()
                    results.append("ordinary admin has no administrator management view")
                    regular_context.close()
                    context.close()
                    browser.close()
                assert not errors, errors
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(json.dumps({"workflows": results, "page_errors": errors, "screenshots": str(OUTPUT),
                      "synthetic_only": True, "real_samlab_integrated": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
