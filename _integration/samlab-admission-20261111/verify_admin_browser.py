"""Check retained administrator forms on the isolated candidate, without submitting real data."""

import json
import subprocess

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright

from verify_isolated import BUSINESS, COMPOSE, REMOTE, ROOT, SSH, remote, wait_for


def check_admin_forms(browser, config, direct_email):
    output = ROOT / "test-results"
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors, failed_requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append({
        "url": request.url.split("?")[0], "failure": request.failure,
    }))
    try:
        page.goto(BUSINESS + "/login?redirect=/admin/users", wait_until="commit", timeout=60000)
        try:
            page.locator("#email").wait_for(timeout=90000)
        finally:
            page.screenshot(path=str(output / "admin-login-diagnostic.png"), full_page=True)
            (output / "admin-browser-diagnostic.json").write_text(json.dumps({
                "url": page.url, "text": page.locator("body").inner_text()[:3000],
                "page_errors": errors, "failed_requests": failed_requests,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        page.locator("#email").fill(config["ADMIN_EMAIL"])
        page.locator("#password").fill(config["ADMIN_PASSWORD"])
        page.locator("form button[type=submit]").click()
        page.wait_for_url("**/admin/users", wait_until="commit", timeout=30000)
        try:
            page.locator(".driver-popover").wait_for(timeout=10000)
        except PlaywrightTimeoutError:
            pass
        else:
            page.keyboard.press("Escape")
            expect(page.locator(".driver-overlay")).to_have_count(0)
        page.get_by_role("button", name="创建用户", exact=True).first.click()
        expect(page.locator("#create-user-form")).to_be_visible()
        expect(page.locator("#create-user-form input[type=email]")).to_be_visible()
        expect(page.locator("#create-user-form input[type=number]").nth(1)).to_have_value("5")
        expect(page.locator("#create-user-form input[type=number]").nth(1)).to_have_attribute("readonly", "")
        page.screenshot(path=str(output / "original-admin-create-retained.png"), full_page=True)
        page.get_by_role("button", name="取消", exact=True).click()
        row = page.get_by_role("row").filter(has_text=direct_email)
        row.get_by_role("button", name="编辑", exact=True).click()
        expect(page.locator("#edit-user-form")).to_be_visible()
        expect(page.locator("#edit-user-form input").nth(1)).to_be_visible()
        expect(page.locator('[data-test="concurrency-input"]')).to_have_value("5")
        expect(page.locator('[data-test="concurrency-input"]')).to_have_attribute("readonly", "")
        page.screenshot(path=str(output / "original-admin-password-edit-retained.png"), full_page=True)
        page.get_by_role("button", name="取消", exact=True).click()
        page.goto(BUSINESS + "/admin/lab-invitations", wait_until="commit")
        expect(page.get_by_role("button", name="创建邀请账号", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="提交审批", exact=True)).to_have_count(0)
        page.screenshot(path=str(output / "admin-invitation-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("""() => {
            const sidebar = document.querySelector('.sidebar');
            const content = document.querySelector('main')?.parentElement;
            return sidebar && content &&
                sidebar.getBoundingClientRect().right <= 1 &&
                content.getBoundingClientRect().left <= 1 &&
                document.documentElement.scrollWidth <= innerWidth;
        }""", timeout=5000)
        page.screenshot(path=str(output / "admin-invitation-mobile.png"), full_page=True)
        assert not errors, errors
    finally:
        if page.url.startswith(BUSINESS) and "/login" not in page.url:
            page.screenshot(path=str(output / "admin-browser-final-diagnostic.png"), full_page=True)
            (output / "admin-browser-final-diagnostic.json").write_text(json.dumps({
                "url": page.url, "text": page.locator("body").inner_text()[:4000], "page_errors": errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        context.close()


def main():
    config = dict(line.split("=", 1) for line in remote(f"cat {REMOTE}/runtime.env").splitlines() if "=" in line)
    remote(COMPOSE + " up -d main")
    tunnel = subprocess.Popen(SSH[:-1] + ["-N", "-C", "-o", "ExitOnForwardFailure=yes",
                                         "-L", "127.0.0.1:18080:127.0.0.1:18080", SSH[-1]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def ready():
            try:
                return httpx.get(BUSINESS + "/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(ready)
        direct_email = remote(COMPOSE + " exec -T postgres psql -U labtest -d labtest -At -c "
                              "\"SELECT email FROM lab_registration_requests WHERE provisioning_source='admin_direct' "
                              "ORDER BY created_at DESC LIMIT 1;\"").strip()
        assert direct_email.endswith("@example.edu"), "only a synthetic administrator-created account may be inspected"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                check_admin_forms(browser, config, direct_email)
            finally:
                browser.close()
        result = {"synthetic_only": True, "production_changed": False, "browser_forms_verified": True}
        (ROOT / "test-results" / "admin-browser-results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        remote(COMPOSE + " stop")
        tunnel.terminate()
        tunnel.wait(timeout=10)


if __name__ == "__main__":
    main()
