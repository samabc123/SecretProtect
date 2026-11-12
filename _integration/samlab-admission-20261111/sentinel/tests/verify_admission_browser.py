"""Real browser acceptance against disposable synthetic databases only."""

import json
import csv
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth_store import AuthStore, password_hash
from app.samlab_admission import SamlabAdmissionStore
from app.roster_import import commit, preview
from app.monitoring_store import MonitoringStore
from app.event_store import EventStore


PASSWORD = "Browser-test-only-2026!"
OUTPUT = ROOT / ".test-results" / "samlab"

def choose_filter(page, name, option):
    admission_filter = page.get_by_role("button", name="筛选设置", exact=True)
    if admission_filter.count() and admission_filter.get_attribute("aria-expanded") != "true":
        admission_filter.click()
    page.locator(".el-select").filter(has=page.get_by_role("combobox", name=name, exact=True)).click()
    page.get_by_role("option", name=option, exact=True).click()
    expect(page.locator(".el-select-dropdown:visible")).to_have_count(0)
    if admission_filter.count():
        admission_filter.click()

def assert_layout(page, name):
    page.wait_for_function("document.documentElement.scrollWidth <= innerWidth", timeout=5000)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{name}: page overflow"
    clipped = page.locator(".record-identity, .record-fields dd, .preview-result, .admission-state").evaluate_all("""
        nodes => nodes.filter(node => node.getClientRects().length &&
          node.scrollWidth > node.clientWidth + 1).map(node => node.className)
    """)
    assert not clipped, f"{name}: clipped record fields {clipped}"


def assert_dialog_fit(page, dialog):
    expect(page.locator(".dialog-fade-enter-active, .dialog-fade-leave-active")).to_have_count(0)
    box = dialog.bounding_box()
    assert box and box["y"] >= 0 and box["y"] + box["height"] <= page.viewport_size["height"] + 1, f"{page.viewport_size}: {box}"
    footer = dialog.locator(".el-dialog__footer").bounding_box()
    assert footer and footer["y"] + footer["height"] <= page.viewport_size["height"], "Dialog footer not reachable"


def verify_responsive_layout(page, admission, origin):
    long_email = "long-address-" + "x" * 46 + "@" + "lab-" + "a" * 40 + "." + "b" * 40 + ".example.edu"
    rows = [f"layout-{index:02d}@example.edu,布局测试人员{index:02d}" for index in range(44)]
    rows.append(f"{long_email}," + "长姓名验证" * 20)
    batch = preview(admission, ("邮箱,用户名\n" + "\n".join(rows) + "\n").encode("utf-8"), "synthetic-layout")
    assert batch["summary"]["error"] == 0
    commit(admission, batch["batch_id"], "synthetic-layout")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(origin + "/admission")
    page.get_by_role("tab", name="白名单", exact=True).click()
    expect(page.locator(".pagination-summary")).to_contain_text("共 45 条")
    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(20)
    with page.expect_download() as exported:
        page.get_by_role("button", name="导出名单", exact=True).click()
    with exported.value.path().open(encoding="utf-8-sig", newline="") as stream:
        exported_rows = list(csv.reader(stream))
    assert len(exported_rows) == 46, "Export must include all filtered records, not only the current page"
    assert exported_rows[0][0:2] == ["邮箱", "用户名"]
    columns_button = page.get_by_role("button", name="列设置", exact=True)
    columns_button.click()
    columns = page.get_by_role("group", name="显示的表格列", exact=True)
    columns.locator("label.el-checkbox").filter(has_text="平台账号编号").click()
    expect(page.get_by_role("columnheader", name="平台账号编号", exact=True)).to_have_count(0)
    columns_button.click()
    page.reload()
    page.get_by_role("tab", name="白名单", exact=True).click()
    expect(page.get_by_role("columnheader", name="平台账号编号", exact=True)).to_have_count(0)
    columns_button.click()
    page.get_by_role("button", name="恢复默认列", exact=True).click()
    columns_button.click()
    expect(page.get_by_role("columnheader", name="平台账号编号", exact=True)).to_be_visible()
    expect(columns).not_to_be_visible()
    page.locator("thead:visible label.el-checkbox").click()
    page.get_by_role("columnheader", name="用户名", exact=False).click()
    expect(page.get_by_role("button", name="批量删除", exact=True)).to_be_disabled()
    expect(page.locator(".el-table__body-wrapper:visible tbody tr").first).to_contain_text("layout-00@example.edu")
    page.set_viewport_size({"width": 1904, "height": 930})
    assert_layout(page, "Sub2API-style desktop 1904x930")
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1"), "Desktop must scroll inside the table"
    table = page.locator(".management-table:visible")
    assert table.bounding_box()["height"] >= 400
    footer = page.locator(".pagination-bar").bounding_box()
    assert footer["y"] + footer["height"] <= 930, "Pagination must remain inside the viewport"
    page.mouse.move(280, 80)
    page.screenshot(path=str(OUTPUT / "sub2api-whitelist-1904x930.png"))
    page.locator("thead:visible label.el-checkbox").click()
    page.screenshot(path=str(OUTPUT / "sub2api-selection-1904x930.png"))
    page.locator("thead:visible label.el-checkbox").click()
    columns_button.click()
    expect(columns).to_be_visible()
    page.screenshot(path=str(OUTPUT / "sub2api-columns-1904x930.png"))
    columns_button.click()
    expect(columns).not_to_be_visible()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("thead:visible label.el-checkbox").click()
    expect(page.get_by_role("button", name="批量删除", exact=True)).to_be_enabled()
    page.get_by_role("button", name="下一页").click()
    expect(page.locator(".pagination-summary")).to_contain_text("第 2 / 3 页")
    expect(page.get_by_role("button", name="批量删除", exact=True)).to_be_disabled()
    page.get_by_role("button", name="上一页").click()
    page.get_by_role("textbox", name="搜索用户名或邮箱").fill("no-such-person")
    expect(page.get_by_text("没有匹配的白名单人员", exact=True)).to_be_visible()
    page.get_by_role("textbox", name="搜索用户名或邮箱").fill("")
    matrix = [(1904, 930), (1440, 1000), (1024, 768), (768, 1024), (390, 844), (320, 568), (844, 390)]
    for width, height in matrix:
        page.set_viewport_size({"width": width, "height": height})
        search = page.get_by_role("textbox", name="搜索用户名或邮箱")
        search.fill(long_email)
        expect(page.get_by_text(long_email, exact=True)).to_be_visible()
        assert_layout(page, f"whitelist {width}x{height}")
        if width <= 900:
            expect(page.get_by_role("list", name="白名单人员列表").get_by_role("listitem")).to_have_count(1)
            expect(page.get_by_text("平台账号编号", exact=True)).to_be_visible()
            card = page.get_by_role("list", name="白名单人员列表").get_by_role("listitem")
            card.locator("label.el-checkbox").click()
            expect(page.get_by_role("button", name="批量删除", exact=True)).to_be_enabled()
            card.locator("label.el-checkbox").click()
        page.get_by_role("button", name="删除", exact=True).click()
        dialog = page.get_by_role("dialog", name="删除白名单人员", exact=True)
        expect(dialog).to_be_visible()
        assert_dialog_fit(page, dialog)
        page.screenshot(path=str(OUTPUT / f"dialog-fit-{width}x{height}.png"))
        dialog.get_by_role("textbox", name="处理原因").fill("仅验证界面滚动")
        dialog.locator("label.el-checkbox").click()
        expect(dialog.get_by_role("button", name="确认删除", exact=True)).to_be_enabled()
        assert_dialog_fit(page, dialog)
        dialog.get_by_role("button", name="取消", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.locator(".dialog-fade-leave-active")).to_have_count(0)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(OUTPUT / f"layout-{width}x{height}.png"), full_page=True)
        if width <= 760:
            page.get_by_role("button", name="打开导航", exact=True).click()
            drawer = page.get_by_role("dialog", name="功能导航", exact=True)
            expect(drawer.get_by_role("menuitem", name="运行审计", exact=True)).to_be_visible()
            page.wait_for_function("""() => {
              const node = document.querySelector('.mobile-navigation');
              if (!node) return false;
              const box = node.getBoundingClientRect();
              return box.x >= -0.5 && box.right <= innerWidth + 0.5;
            }""")
            page.screenshot(path=str(OUTPUT / f"navigation-{width}x{height}.png"))
            drawer.get_by_role("menuitem", name="管理员", exact=True).click()
            expect(drawer).not_to_be_visible()
            expect(page.get_by_role("heading", name="管理员", exact=True, level=2)).to_be_visible()
            assert_layout(page, f"admins {width}x{height}")
            page.goto(origin + "/admission")
            page.get_by_role("tab", name="白名单", exact=True).click()
    # Short desktop navigation must scroll to its final item.
    page.set_viewport_size({"width": 1024, "height": 600})
    last_item = page.locator(".app-sidebar").get_by_role("menuitem", name="运行审计", exact=True)
    last_item.scroll_into_view_if_needed()
    menu_box, item_box = page.locator(".app-sidebar .app-menu").bounding_box(), last_item.bounding_box()
    assert item_box["y"] >= menu_box["y"] and item_box["y"] + item_box["height"] <= menu_box["y"] + menu_box["height"] + 1
    # Verify common shell changes against existing views without triggering collection.
    for path in ("/", "/monitoring-tasks", "/events", "/findings", "/verification",
                 "/channels", "/reports", "/settings", "/connector-runs"):
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(origin + path)
        expect(page.locator(".app-header")).to_be_visible()
        expect(page.locator(".loading-state").first).not_to_be_visible(timeout=15000)
        assert_layout(page, path)
        if path == "/":
            expect(page.get_by_role("heading", name="安全工作台", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="近期事件", exact=True)).to_be_visible()
            expect(page.get_by_text("历史与测试来源不等同于生产平台实测", exact=True)).to_be_visible()
            assert page.locator("#overview-title").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
        if path == "/settings":
            expect(page.locator(".settings-tabs")).to_have_class("el-tabs el-tabs--top settings-tabs")
            assert page.locator(".settings-tabs .el-tabs__item").evaluate_all(
                "nodes => nodes.every(node => node.getBoundingClientRect().width >= 120 && node.getBoundingClientRect().height <= 90)"
            ), "Settings tabs squeezed into vertical text"
            audit_tab = page.get_by_role("tab", name="安全审计", exact=False)
            for _ in range(8):
                bounds = audit_tab.bounding_box()
                if bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390:
                    break
                page.locator(".settings-tabs .el-tabs__nav-next").click()
            audit_tab.click()
            expect(audit_tab).to_have_attribute("aria-selected", "true")
            account_tab = page.get_by_role("tab", name="个人账号", exact=False)
            for _ in range(8):
                bounds = account_tab.bounding_box()
                if bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390:
                    break
                page.locator(".settings-tabs .el-tabs__nav-prev").click()
            account_tab.click()
        page.screenshot(path=str(OUTPUT / f"route-{path.strip('/') or 'overview'}-mobile.png"))

def verify_security_workspace(page, origin):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(origin + "/monitoring-tasks")
    explorer = page.get_by_role("complementary", name="检测目标列表")
    expect(explorer).to_be_visible()
    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(3)
    explorer.get_by_role("button").filter(has_text="https://ui-fixture.example.invalid/site-a").click()
    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(2)
    expect(page.locator(".pagination-summary")).to_contain_text("共 2 条")
    page.screenshot(path=str(OUTPUT / "scan-workspace-desktop.png"), full_page=True)
    page.locator(".el-table__body-wrapper:visible tbody tr").first.get_by_role("button", name="详情", exact=True).click()
    detail = page.get_by_role("dialog", name="监控任务详情", exact=True)
    expect(detail.get_by_text("https://ui-fixture.example.invalid/site-a", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(detail).not_to_be_visible()
    task_row = page.locator(".el-table__body-wrapper:visible tbody tr").first
    task_row.get_by_role("button", name="更多操作", exact=True).click()
    page.get_by_role("menuitem", name="修改", exact=True).click()
    edit_dialog = page.get_by_role("dialog", name="修改监控任务", exact=True)
    expect(edit_dialog).to_be_visible()
    edit_dialog.get_by_role("button", name="取消", exact=True).click()
    expect(edit_dialog).not_to_be_visible()
    task_row.get_by_role("button", name="更多操作", exact=True).click()
    page.get_by_role("menuitem", name="暂停", exact=True).click()
    expect(task_row.get_by_text("已暂停", exact=True)).to_be_visible()
    explorer.get_by_role("button").filter(has_text="全部目标").click()
    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(3)
    page.set_viewport_size({"width": 390, "height": 844})
    assert_layout(page, "scan workspace mobile")
    page.screenshot(path=str(OUTPUT / "scan-workspace-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(origin + "/")
    expect(page.get_by_role("heading", name="安全工作台", exact=True)).to_be_visible()
    expect(page.locator(".console-section .el-table__body-wrapper tbody tr")).to_have_count(3)
    expect(page.locator(".console-section").get_by_text("合成样例", exact=False)).to_have_count(3)
    page.screenshot(path=str(OUTPUT / "security-console-desktop.png"), full_page=True)
    page.goto(origin + "/events?source_kind=" + quote("合成样例"))
    expect(page.locator(".event-table .el-table__body-wrapper tbody tr")).to_have_count(3)
    page.screenshot(path=str(OUTPUT / "security-events-desktop.png"), full_page=True)


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
        monitoring = MonitoringStore(temp / "monitoring.sqlite3")
        for index, target in enumerate(["site-a", "site-a", "site-b"]):
            monitoring.create_task({
                "task_id": f"synthetic-task-{index}", "task_name": f"合成界面任务 {index}",
                "channel": "controlled_test", "platform": "合成测试载体",
                "monitor_object": f"https://ui-fixture.example.invalid/{target}",
                "collection_method": "controlled_fixtures", "schedule": "手动",
            })
        event_store = EventStore(temp / "events.sqlite3")
        event_store.import_synthetic([{
            "event_id": f"synthetic-ui-event-{index}", "source_kind": "合成样例",
            "channel": "controlled_test", "platform": "合成测试载体",
            "credential_type": "synthetic_token", "redacted_value": "synthetic_****_only",
            "evidence_level": "L0", "risk_level": risk, "disposition": "待确认",
            "first_observed_at": "2026-11-11T12:00:00Z", "detected_at": "2026-11-11T12:00:00Z",
        } for index, risk in enumerate(["high", "medium", "low"])])
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
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
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
                    page.get_by_role("button", name="收起侧栏", exact=True).click()
                    expect(page.locator(".app-sidebar")).to_have_class("el-aside app-sidebar is-collapsed")
                    page.reload()
                    expect(page.get_by_role("button", name="展开侧栏", exact=True)).to_be_visible()
                    expect(page.get_by_role("heading", name="Samlab 人员准入")).to_be_visible()
                    page.screenshot(path=str(OUTPUT / "sidebar-collapsed.png"))
                    page.get_by_role("button", name="展开侧栏", exact=True).click()
                    choose_filter(page, "审批状态", "已同意")
                    expect(page.get_by_text("没有匹配的注册申请", exact=True)).to_be_visible()
                    choose_filter(page, "审批状态", "全部审批状态")
                    expect(page.get_by_text("student01@example.edu", exact=True)).to_be_visible()
                    results.append("sidebar collapse persists on reload; approval-state filter uses actual records")
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
                        "buffer": "邮箱,用户名\nroster-browser@example.edu,导入人员\nroster-browser@example.edu,重复行\n".encode("utf-8-sig"),
                    })
                    expect(import_dialog.get_by_role("button", name="确认导入", exact=True)).to_be_enabled()
                    page.screenshot(path=str(OUTPUT / "roster-import-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(import_dialog.get_by_text("新增", exact=True)).to_be_visible()
                    expect(import_dialog.get_by_text("跳过", exact=True)).to_be_visible()
                    assert_dialog_fit(page, import_dialog)
                    page.screenshot(path=str(OUTPUT / "roster-import-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Import dialog mobile overflow"
                    import_dialog.get_by_role("button", name="确认导入", exact=True).click()
                    expect(import_dialog).not_to_be_visible()
                    expect(page.get_by_role("tabpanel", name="白名单").get_by_text("roster-browser@example.edu", exact=True)).to_be_visible()
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
                    expect(page.get_by_role("columnheader", name="有效期", exact=True)).to_have_count(0)
                    expect(page.get_by_role("button", name="续期", exact=True)).to_have_count(0)
                    expect(page.get_by_role("button", name="撤销", exact=True)).to_have_count(0)
                    expect(page.get_by_text("永久有效", exact=True)).to_have_count(2)
                    expect(page.get_by_text("待平台开通", exact=True)).to_be_visible()
                    expect(page.get_by_text("平台已回执开通", exact=True)).to_have_count(0)
                    expect(page.locator(".el-message")).to_have_count(0)
                    page.screenshot(path=str(OUTPUT / "whitelist-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "whitelist-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflow"
                    results.append("one approval adds whitelist; no fake platform account success")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    batch_button = page.get_by_role("button", name="批量删除", exact=True)
                    expect(batch_button).to_be_disabled()
                    roster_row = page.get_by_role("row").filter(has_text="roster-browser@example.edu")
                    roster_row.get_by_role("button", name="删除", exact=True).click()
                    single_dialog = page.get_by_role("dialog", name="删除白名单人员", exact=True)
                    expect(single_dialog.get_by_label("待删除人员")).to_contain_text("roster-browser@example.edu")
                    single_dialog.get_by_role("button", name="取消", exact=True).click()
                    roster_row.locator("label.el-checkbox").click()
                    expect(roster_row.get_by_role("checkbox")).to_be_checked()
                    expect(batch_button).to_be_enabled()
                    choose_filter(page, "人员来源", "文件导入")
                    expect(batch_button).to_be_disabled()
                    expect(page.get_by_role("tabpanel", name="白名单").get_by_text("student01@example.edu", exact=True)).to_have_count(0)
                    expect(roster_row).to_be_visible()
                    choose_filter(page, "人员来源", "全部来源")
                    page.get_by_role("textbox", name="搜索用户名或邮箱").fill("student01")
                    expect(batch_button).to_be_disabled()
                    page.get_by_role("textbox", name="搜索用户名或邮箱").fill("")
                    page.get_by_role("tabpanel", name="白名单").locator("thead label.el-checkbox").first.click()
                    batch_button.click()
                    removal = page.get_by_role("dialog", name="批量删除白名单人员", exact=True)
                    expect(removal.get_by_label("待删除人员").get_by_role("listitem")).to_have_count(2)
                    expect(removal.get_by_role("button", name="确认删除", exact=True)).to_be_disabled()
                    removal.get_by_role("button", name="取消", exact=True).click()
                    expect(roster_row).to_be_visible()
                    batch_button.click()
                    removal.get_by_role("textbox", name="处理原因").fill("实验室人员批量离组")
                    removal.locator("label.el-checkbox").click()
                    page.screenshot(path=str(OUTPUT / "batch-delete-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "batch-delete-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Batch delete mobile overflow"
                    removal.get_by_role("button", name="确认删除", exact=True).click()
                    expect(removal).not_to_be_visible()
                    expect(page.get_by_role("tabpanel", name="白名单").get_by_text("暂无已批准的 Samlab 人员")).to_be_visible()
                    assert not admission.status("synthetic-request-01")["allowed"]
                    with admission.connect() as c:
                        assert c.execute("SELECT count(*) FROM samlab_members WHERE deleted_at IS NOT NULL").fetchone()[0] == 2
                    results.append("permanent roster; single-delete confirmation; filtered selections clear; batch cancel and atomic delete; desktop/mobile fit")
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
                    page.get_by_role("button", name="创建管理员", exact=True).click()
                    create_dialog = page.get_by_role("dialog", name="创建管理员", exact=True)
                    create_dialog.get_by_role("textbox", name="新管理员用户名").fill("x")
                    create_dialog.get_by_label("新管理员初始密码", exact=True).fill("short")
                    create_dialog.get_by_role("button", name="创建管理员", exact=True).click()
                    expect(create_dialog.get_by_text("请输入 3—32 位合法用户名。", exact=True)).to_be_visible()
                    expect(create_dialog.get_by_text("初始密码须为 6—128 位。", exact=True)).to_be_visible()
                    create_dialog.get_by_role("button", name="取消", exact=True).click()
                    expect(create_dialog).not_to_be_visible()
                    expect(page.locator(".dialog-fade-leave-active")).to_have_count(0)
                    page.get_by_role("button", name="创建管理员", exact=True).click()
                    expect(create_dialog.get_by_label("新管理员初始密码", exact=True)).to_have_value("")
                    create_dialog.get_by_role("textbox", name="新管理员用户名").fill("createdbyui")
                    create_dialog.get_by_label("新管理员初始密码", exact=True).fill(PASSWORD)
                    create_dialog.get_by_role("button", name="创建管理员", exact=True).click()
                    expect(create_dialog).not_to_be_visible()
                    expect(page.get_by_role("row").filter(has_text="createdbyui")).to_be_visible()
                    page.get_by_role("textbox", name="搜索管理员").fill("createdbyui")
                    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(1)
                    page.get_by_role("textbox", name="搜索管理员").fill("")
                    created_row = page.get_by_role("row").filter(has_text="createdbyui")
                    created_row.get_by_role("button", name="停用", exact=True).click()
                    page.get_by_role("button", name="确认", exact=True).click()
                    expect(created_row.get_by_text("停用", exact=True)).to_be_visible()
                    choose_filter(page, "管理员状态", "停用")
                    expect(page.locator(".el-table__body-wrapper:visible tbody tr")).to_have_count(1)
                    created_row.get_by_role("button", name="启用", exact=True).click()
                    page.get_by_role("button", name="确认", exact=True).click()
                    expect(page.get_by_text("没有匹配的管理员", exact=True)).to_be_visible()
                    choose_filter(page, "管理员状态", "全部状态")
                    expect(created_row).to_be_visible()
                    expect(created_row.locator(".el-tag").get_by_text("启用", exact=True)).to_be_visible()
                    expect(page.get_by_role("row").filter(has_text="受保护").locator(".el-tag").get_by_text("超级管理员", exact=True)).to_be_visible()
                    expect(page.locator(".el-message")).to_have_count(0)
                    page.screenshot(path=str(OUTPUT / "admins-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(OUTPUT / "admins-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Admin mobile page overflow"
                    page.get_by_role("button", name="创建管理员", exact=True).click()
                    expect(create_dialog.get_by_label("新管理员初始密码", exact=True)).to_have_value("")
                    assert_dialog_fit(page, create_dialog)
                    page.screenshot(path=str(OUTPUT / "create-admin-mobile.png"))
                    create_dialog.get_by_role("button", name="取消", exact=True).click()
                    expect(create_dialog).not_to_be_visible()
                    results.append("admin modal validates fields, clears password on cancel/success, creates ordinary admin; search and desktop/mobile fit")
                    verify_security_workspace(page, origin)
                    results.append("security console shows persisted synthetic events with provenance; target explorer filters tasks and opens details; no scan triggered")
                    verify_responsive_layout(page, admission, origin)
                    results.append("Sub2API-style viewport-filling table, fixed pagination, persisted column settings, full-set sorting clears selection and CSV export includes 45 records")
                    results.append("45 synthetic members: pagination, page-selection reset, empty search, long names/emails, desktop/tablet/mobile/landscape, dialog footer, drawer and all existing route widths")
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
    report = {"run_completed": True, "workflows": results, "page_errors": errors, "screenshots": str(OUTPUT),
              "browser": "Microsoft Edge / Chromium, emulated viewport sizes",
              "layout_viewports": ["1904x930", "1440x1000", "1024x768", "768x1024", "390x844", "320x568", "844x390"],
              "synthetic_only": True, "real_samlab_integrated": False, "production_changed": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    (OUTPUT / "browser-results.json").write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
