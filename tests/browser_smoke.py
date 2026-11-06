"""Optional real-browser checks, using the fake worker without loading a GPU model.

Run with Chromium on PATH:
    nix develop --command uv run --locked --with playwright python tests/browser_smoke.py
"""

import json
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
    model_directory = tempfile.TemporaryDirectory(prefix="diffusion-browser-models-")
    for name in (
        "NoobAI-XL-Vpred-v1.0.safetensors",
        "WAI-NSFW-Illustrious-v17.0.safetensors",
        "NoobAI-XL-v1.1.safetensors",
    ):
        (Path(model_directory.name) / name).touch()
    source = """
import sys
import uvicorn
from diffusion_on_demand.config import Config
from diffusion_on_demand.service import create_app
app = create_app(Config(
    root_path='/diffusion', idle_seconds=0.15,
    worker_command=(sys.executable, sys.argv[2]),
    model=sys.argv[3]+'/WAI-NSFW-Illustrious-v17.0.safetensors',
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
            model_directory.name,
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
            expect(page.locator("#prompt")).to_contain_text("masterpiece")
            page.locator("#api-key").fill("browser-test-key")
            page.locator("#api-key").dispatch_event("change")
            expect(page.locator("#model-status")).to_have_text("模型已释放 · 随时可用")
            expect(page.locator("#options-state")).to_contain_text("所有参数")
            page.locator("#advanced-parameters").evaluate("node => node.open = false")
            expect(page.locator("#option-model")).to_be_visible()
            expect(page.locator("#apply-model-preset")).to_be_visible()
            expect(page.locator("#negative-prompt")).to_be_hidden()
            expect(page.locator("#steps")).to_be_hidden()
            assert page.locator("#option-model").evaluate(
                "node => Boolean(node.compareDocumentPosition(document.getElementById('prompt')) & Node.DOCUMENT_POSITION_FOLLOWING)"
            )
            page.screenshot(path="/tmp/diffusion-web-desktop.png", full_page=True)
            page.locator("#advanced-parameters > summary").click()
            for summary in page.locator(".option-group summary").all():
                summary.click()
            page.locator("#option-precision").select_option("float32")
            page.locator("#option-offload").select_option("sequential")
            page.locator("#option-vae_tiling").uncheck()
            page.locator("#option-sampler").select_option("dpmpp_2m")
            page.locator("#option-scheduler_options").fill('{"use_karras_sigmas":true}')
            page.locator("#option-prompt_2").fill('""')
            page.locator("#option-original_size").fill("[1024,1536]")
            page.locator("#width").fill("1536")
            page.locator("#height").fill("1024")
            page.locator("#steps").fill("80")
            expect(page.locator("#size")).to_have_value("custom")
            page.locator("#prompt").fill("a quiet forest")
            page.locator("#negative-prompt").fill("")
            page.locator("#seed").fill("42")
            assert not payloads, payloads
            page.locator("#generate").click()
            expect(page.locator("#history-count")).to_have_text("1")
            expect(page.locator("#preview")).to_be_visible()
            assert page.locator("#preview").evaluate("img => img.complete && img.naturalWidth > 0")
            assert payloads[-1]["prompt"] == "a quiet forest", payloads[-1]
            assert payloads[-1]["negative_prompt"] == ""
            assert payloads[-1]["precision"] == "float32"
            assert payloads[-1]["offload"] == "sequential"
            assert payloads[-1]["vae_tiling"] is False
            assert payloads[-1]["scheduler_options"] == {"use_karras_sigmas": True}
            assert payloads[-1]["prompt_2"] == ""
            assert payloads[-1]["original_size"] == [1024, 1536]
            assert payloads[-1]["width"] == 1536
            with page.expect_download() as download:
                page.locator("#download").click()
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "image.png"
                download.value.save_as(target)
                assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            page.reload()
            expect(page.locator("#history-count")).to_have_text("1")
            page.locator(".history-open").click()
            expect(page.locator("#prompt")).to_have_value("a quiet forest")
            expect(page.locator("#negative-prompt")).to_have_value("")
            expect(page.locator("#seed")).to_have_value("42")
            page.locator("#api-key").fill("browser-test-key")
            page.locator("#api-key").dispatch_event("change")
            expect(page.locator("#options-state")).to_contain_text("所有参数")
            expect(page.locator("#option-offload")).to_have_value("sequential")
            expect(page.locator("#option-precision")).to_have_value("float32")
            expect(page.locator("#option-vae_tiling")).not_to_be_checked()
            expect(page.locator("#option-original_size")).to_have_value("[1024,1536]")
            # Invalid JSON must be rejected locally, without starting a request.
            page.locator(".option-group summary").nth(1).click()
            page.locator("#option-scheduler_options").fill("{")
            previous_requests = len(payloads)
            page.locator("#generate").click()
            expect(page.locator("#error")).to_contain_text("无效 JSON")
            assert len(payloads) == previous_requests
            page.locator("#option-scheduler_options").fill('{"use_karras_sigmas":true}')
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
                    const r = indexedDB.open('diffusion-history:/diffusion/');
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
            page.locator("#api-key").fill("browser-test-key")
            page.locator("#api-key").dispatch_event("change")
            expect(page.locator("#options-state")).to_contain_text("所有参数")
            for summary in page.locator(".option-group summary").all():
                summary.click()
            page.locator("#prompt").fill("my existing scene")
            page.locator("#seed").fill("123")
            page.locator("#option-precision").select_option("float32")
            page.locator("#option-timesteps").fill("[999,0]")
            page.locator("#option-model").select_option("NoobAI-XL-v1.1.safetensors")
            expect(page.locator("#prompt")).to_have_value("my existing scene")
            expect(page.locator("#apply-model-preset")).to_be_enabled()
            page.locator("#apply-model-preset").click()
            expect(page.locator("#cfg")).to_have_value("5.5")
            expect(page.locator("#steps")).to_have_value("28")
            expect(page.locator("#prompt")).to_have_value(
                "masterpiece, best quality, newest, highres, safe"
            )
            expect(page.locator("#option-timesteps")).to_have_value("")
            expect(page.locator("#option-precision")).to_have_value("float32")
            expect(page.locator("#seed")).to_have_value("123")
            page.locator("#option-model").select_option("NoobAI-XL-Vpred-v1.0.safetensors")
            expect(page.locator("#option-sampler")).to_have_value("euler")
            assert (
                json.loads(page.locator("#option-scheduler_options").input_value())[
                    "prediction_type"
                ]
                == "v_prediction"
            )
            assert (
                json.loads(page.locator("#option-scheduler_options").input_value())[
                    "rescale_betas_zero_snr"
                ]
                is True
            )
            page.locator("#apply-model-preset").click()
            page.locator("#option-model").select_option("WAI-NSFW-Illustrious-v17.0.safetensors")
            expect(page.locator("#cfg")).to_have_value("6")
            page.locator("#steps").fill("42")
            page.locator("#apply-model-preset").click()
            expect(page.locator("#steps")).to_have_value("42")
            page.locator("#steps").fill("25")
            expect(page.locator("#cfg")).to_have_value("6")
            expect(page.locator("#steps")).to_have_value("25")
            expect(page.locator("#width")).to_have_value("1024")
            page.locator("#generate").click()
            expect(page.locator("#history-count")).to_have_text("1")
            assert payloads[-1]["model"] == "WAI-NSFW-Illustrious-v17.0.safetensors"
            assert payloads[-1]["scheduler_options"]["prediction_type"] == "epsilon"
            # Upgrade an existing v1 database with >20 records, then paginate
            # and delete records without discarding any older images.
            archive = browser.new_context()
            archive_page = archive.new_page()
            archive_page.on("pageerror", lambda error: errors.append(str(error)))
            archive_page.route(
                "**/assets/app.js",
                lambda route: route.fulfill(body="", content_type="text/javascript"),
            )
            archive_page.goto(base)
            archive_page.evaluate("""async () => {
                const db = await new Promise((resolve, reject) => {
                    const r = indexedDB.open('diffusion-history:/diffusion/', 1);
                    r.onupgradeneeded = () => r.result.createObjectStore('images', {keyPath:'id'});
                    r.onsuccess = () => resolve(r.result); r.onerror = reject;
                });
                await new Promise((resolve, reject) => {
                    const tx = db.transaction('images', 'readwrite');
                    for (let i = 0; i < 61; i++) tx.objectStore('images').put({
                        id: `old-${String(i).padStart(3,'0')}`, created: i + 1,
                        params: {prompt:`old image ${i}`, negative_prompt:'', width:832,
                            height:1216, steps:30, cfg_scale:6.5, seed:i}, seed:i,
                        blob:new Blob(['old-image'], {type:'image/png'})
                    });
                    tx.oncomplete = resolve; tx.onerror = reject;
                });
                db.close();
            }""")
            archive_page.unroute("**/assets/app.js")
            archive_page.reload()
            expect(archive_page.locator("#history-count")).to_have_text("61")
            expect(archive_page.locator(".history-card")).to_have_count(24)
            archive_page.locator("#load-history").click()
            expect(archive_page.locator(".history-card")).to_have_count(48)
            archive_page.locator("#load-history").click()
            expect(archive_page.locator(".history-card")).to_have_count(61)
            expect(archive_page.locator("#load-history")).to_be_hidden()
            # Cancel a single deletion, then confirm it; the selected preview
            # and download must not continue to reference the deleted image.
            archive_page.locator(".history-open").first.click()
            archive_page.locator(".history-delete").first.click()
            archive_page.locator("#cancel-clear").click()
            expect(archive_page.locator("#history-count")).to_have_text("61")
            deleted_url = archive_page.locator("#preview").get_attribute("src")
            deleted_thumbnail = archive_page.locator(".history-card img").first.get_attribute("src")
            archive_page.locator(".history-delete").first.click()
            archive_page.locator("#confirm-clear").click()
            expect(archive_page.locator("#history-count")).to_have_text("60")
            expect(archive_page.locator("#download")).to_be_hidden()
            for url in (deleted_url, deleted_thumbnail):
                assert archive_page.evaluate(
                    "async url => {try {await fetch(url); return false} catch {return true}}", url
                )
            assert archive_page.evaluate("""async () => {
                const db = await new Promise(resolve => {
                    const r = indexedDB.open('diffusion-history:/diffusion/');
                    r.onsuccess = () => resolve(r.result);
                });
                return await new Promise(resolve => {
                    const r = db.transaction('images').objectStore('images').get('old-060');
                    r.onsuccess = () => { db.close(); resolve(r.result === undefined); };
                });
            }""")
            archive_page.locator("#api-key").fill("browser-test-key")
            archive_page.locator("#prompt").fill("new image after old history")
            archive_page.locator("#generate").click()
            expect(archive_page.locator("#history-count")).to_have_text("61")
            archive_page.reload()
            expect(archive_page.locator("#history-count")).to_have_text("61")
            # Other images survive reload, and deleting one does not hide the
            # current preview when a different image is selected.
            archive_page.locator(".history-delete").nth(1).click()
            archive_page.locator("#confirm-clear").click()
            expect(archive_page.locator("#history-count")).to_have_text("60")
            expect(archive_page.locator("#download")).to_be_visible()
            archive.close()
            # Storage denial must not prevent generation and download.
            other = browser.new_context()
            other.add_init_script(
                "Object.defineProperty(window, 'indexedDB', {get() {throw new Error('denied')}})"
            )
            other.add_init_script("""
                const originalFetch = window.fetch.bind(window);
                window.fetch = (url, init) => String(url).endsWith('/options') && !init?.headers?.Authorization
                    ? new Promise(resolve => setTimeout(() => resolve(originalFetch(url, init)), 300))
                    : originalFetch(url, init);
            """)
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
        model_directory.cleanup()


if __name__ == "__main__":
    main()
