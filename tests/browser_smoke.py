"""Optional real-browser checks, using the fake worker without loading a GPU model.

Run with Chromium on PATH:
    nix develop --command uv run --locked --with playwright python tests/browser_smoke.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


def main():
    chromium = os.environ.get("CHROMIUM") or shutil.which("chromium")
    if not chromium:
        raise SystemExit(
            "Provide Chromium with nix shell nixpkgs#chromium or CHROMIUM=/path/to/chromium"
        )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    source = """
import sys
import uvicorn
from diffusion_on_demand.config import Config
from diffusion_on_demand.service import create_app
app = create_app(Config(
    root_path='/diffusion', idle_seconds=0.15,
    worker_command=(sys.executable, sys.argv[2]),
))
async def stripped_proxy(scope, receive, send):
    if scope['type'] == 'http' and scope['path'].startswith('/diffusion'):
        path = scope['path'][len('/diffusion'):] or '/'
        scope = {**scope, 'path': path, 'raw_path': path.encode()}
    await app(scope, receive, send)
uvicorn.run(stripped_proxy, fd=int(sys.argv[1]), log_level='error')
"""
    env = dict(os.environ, DIFFUSION_ON_DEMAND_API_KEY="browser-test-key")
    env.pop("NOTIFY_SOCKET", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            str(listener.fileno()),
            str(Path(__file__).with_name("fake_worker.py").resolve()),
        ],
        pass_fds=(listener.fileno(),),
        env=env,
    )
    listener.close()
    base = f"http://127.0.0.1:{port}/diffusion/"
    try:
        for _ in range(100):
            try:
                with urlopen(base + "health", timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("Test server exited")
                time.sleep(0.05)
        else:
            raise RuntimeError("Test server did not start")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium, headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            payloads = []
            page.on(
                "request",
                lambda r: (
                    payloads.append(r.post_data_json) if r.url.endswith("/generate") else None
                ),
            )
            # Deliberately omit the trailing slash to exercise proxy-prefix URLs.
            page.goto(base.rstrip("/"))
            expect(page.locator("#prompt")).to_contain_text("score_9")
            page.locator("#api-key").fill("browser-test-key")
            page.locator("#api-key").dispatch_event("change")
            expect(page.locator("#model-status")).to_have_text("模型已释放 · 随时可用")
            page.screenshot(path="/tmp/diffusion-web-desktop.png", full_page=True)
            page.locator("#prompt").fill("a quiet forest")
            page.locator("#negative-prompt").fill("")
            page.locator("#seed").fill("42")
            page.locator("#generate").click()
            expect(page.locator("#history-count")).to_have_text("1")
            expect(page.locator("#preview")).to_be_visible()
            assert page.locator("#preview").evaluate("img => img.complete && img.naturalWidth > 0")
            assert payloads[-1]["prompt"] == "a quiet forest"
            assert payloads[-1]["negative_prompt"] == ""
            with page.expect_download() as download:
                page.locator("#download").click()
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "image.png"
                download.value.save_as(target)
                assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            page.reload()
            expect(page.locator("#history-count")).to_have_text("1")
            page.locator(".history-card").click()
            expect(page.locator("#prompt")).to_have_value("a quiet forest")
            expect(page.locator("#negative-prompt")).to_have_value("")
            expect(page.locator("#seed")).to_have_value("42")
            page.locator("#api-key").fill("browser-test-key")
            page.locator("#prompt").fill("error")
            page.locator("#generate").click()
            expect(page.locator("#error")).to_contain_text("simulated OOM")
            expect(page.locator("#generate")).to_be_enabled()
            # Authentication errors are actionable and never stored with images.
            page.locator("#api-key").fill("wrong-key")
            page.locator("#generate").click()
            expect(page.locator("#error")).to_contain_text("API key")
            saved = page.evaluate("""async () => {
                const db = await new Promise((resolve, reject) => {
                    const r = indexedDB.open('diffusion-history:/diffusion/', 1);
                    r.onsuccess = () => resolve(r.result); r.onerror = reject;
                });
                return await new Promise(resolve => {
                    const r = db.transaction('images').objectStore('images').getAll();
                    r.onsuccess = () => { db.close(); resolve(r.result.map(x => x.params)); };
                });
            }""")
            assert "browser-test-key" not in str(saved)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path="/tmp/diffusion-web-mobile.png", full_page=True)
            page.locator("#clear-history").click()
            page.locator("#cancel-clear").click()
            expect(page.locator("#history-count")).to_have_text("1")
            page.locator("#clear-history").click()
            page.locator("#confirm-clear").click()
            expect(page.locator("#history-count")).to_have_text("0")
            page.reload()
            expect(page.locator("#history-empty")).to_be_visible()
            # Storage denial must not prevent generation and download.
            other = browser.new_context()
            other.add_init_script(
                "Object.defineProperty(window, 'indexedDB', {get() {throw new Error('denied')}})"
            )
            no_storage = other.new_page()
            no_storage.goto(base)
            expect(no_storage.locator("#storage-notice")).to_be_visible()
            no_storage.locator("#api-key").fill("browser-test-key")
            no_storage.locator("#generate").click()
            expect(no_storage.locator("#download")).to_be_visible()
            assert not errors, errors
            browser.close()
        print(
            "Browser checks passed: proxy prefix, generation, exact prompts, PNG download, history, auth, errors, mobile layout and storage denial."
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
