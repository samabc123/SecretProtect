"""Inspect candidate registration without touching production or submitting credentials."""

import json
import subprocess

from playwright.sync_api import sync_playwright

from verify_isolated import ROOT, SSH, BUSINESS, COMPOSE, remote, wait_for


def main():
    output = ROOT / "test-results"
    remote(COMPOSE + " up -d main")
    tunnel = subprocess.Popen(SSH[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes",
                                         "-L", "127.0.0.1:18080:127.0.0.1:18080", SSH[-1]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import httpx
        def ready():
            try:
                return httpx.get(BUSINESS + "/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(ready)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, requests, responses = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: requests.append({"url": request.url.split("?")[0], "failure": request.failure}))
            page.on("response", lambda response: responses.append({"url": response.url.split("?")[0], "status": response.status}))
            try:
                page.goto(BUSINESS + "/register", wait_until="commit", timeout=60000)
                page.locator("#lab-username").wait_for(timeout=90000)
            finally:
                page.screenshot(path=str(output / "samlab-register-diagnostic.png"), full_page=True, timeout=15000)
                result = {"url": page.url, "text": page.locator("body").inner_text()[:3000],
                          "page_errors": errors, "failed_requests": requests, "responses": responses}
                (output / "browser-diagnostic.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False, indent=2))
            page.screenshot(path=str(output / "samlab-register-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "samlab-register-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            browser.close()
    finally:
        remote(COMPOSE + " stop")
        tunnel.terminate()
        tunnel.wait(timeout=10)


if __name__ == "__main__":
    main()
