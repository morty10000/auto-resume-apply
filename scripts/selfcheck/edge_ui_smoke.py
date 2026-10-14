"""Headless Microsoft Edge smoke test for the web UI.

Starts a disposable server on a temporary port/data directory, drives the UI
with system Edge, checks every tab and theme, verifies one configuration edit
round-trips to the API, and asserts that no console error or layout overflow
appears at desktop and mobile widths.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services.browser import find_edge
from playwright.async_api import async_playwright

PORT = 18081
BASE_URL = f"http://127.0.0.1:{PORT}"
TABS = ["home", "collect-setup", "match-setup", "apply-setup", "monitor",
        "collect", "matchlog", "results", "resume"]


def wait_server(timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"temporary server did not start: {last_error}")


async def run_browser() -> None:
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=find_edge(), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda msg: errors.append(f"console {msg.type}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

        response = await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        assert response and response.status == 200
        assert "全自动投递简历系统" in await page.title()
        assert await page.locator("#platformList .platform-card").count() == 4
        assert await page.locator(".nav-bar .tab").count() == len(TABS)

        for tab in TABS:
            await page.locator(f'.nav-bar .tab[data-tab="{tab}"]').click()
            panel = page.locator(f"#tab-{tab}")
            assert await panel.is_visible(), f"tab panel not visible: {tab}"
            assert await panel.evaluate("el => el.classList.contains('active')")

        await page.locator('#btnTheme').click()
        assert await page.locator('#themeMenu .theme-opt').count() == 6
        for slug in ["neumorphism", "brutalist", "editorial", "bento", "dark", "corporate"]:
            if await page.locator('#themeMenu').evaluate("el => el.classList.contains('hidden')"):
                await page.locator('#btnTheme').click()
            await page.locator(f'#themeMenu .theme-opt[data-slug="{slug}"]').click()
            assert await page.locator("html").get_attribute("data-theme") == slug

        await page.locator('.nav-bar .tab[data-tab="collect-setup"]').click()
        keyword_input = page.locator("#keywordInput")
        await keyword_input.fill("回归测试关键词")
        await keyword_input.press("Enter")
        saved = False
        for _ in range(20):
            payload = await page.evaluate("fetch('/api/userconfig').then(r => r.json())")
            if "回归测试关键词" in ((payload.get("config") or {}).get("keywords") or []):
                saved = True
                break
            await page.wait_for_timeout(250)
        assert saved, "keyword edit was not persisted to /api/userconfig"

        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator('.nav-bar .tab[data-tab="collect-setup"]').click()
        assert not await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")

        # Stop with no task running must be a harmless idempotent operation.
        result = await page.evaluate("fetch('/api/task/stop',{method:'POST'}).then(r => r.json())")
        assert result.get("ok") is True
        assert not errors, json.dumps(errors, ensure_ascii=False)
        await browser.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="auto_resume_edge_smoke_", ignore_cleanup_errors=True) as data_dir:
        env = dict(os.environ)
        env.update({
            "AUTO_RESUME_DATA_DIR": data_dir,
            "AUTO_RESUME_API_PORT": str(PORT),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        })
        server = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "server_host.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            wait_server()
            asyncio.run(run_browser())
            print("edge ui smoke: PASS")
            return 0
        finally:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
