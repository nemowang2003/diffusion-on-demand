import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from diffusion_on_demand.config import Config
from diffusion_on_demand.runtime import Busy, Runtime, WorkerError
from diffusion_on_demand.service import create_app

FAKE = (sys.executable, str(Path(__file__).with_name("fake_worker.py")))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime(Config(worker_command=FAKE, idle_seconds=0.05))

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_reuse_and_idle_reap(self):
        task = asyncio.create_task(self.runtime.reap())
        try:
            await self.runtime.generate({"prompt": "one"})
            pid = self.runtime.process.pid
            await self.runtime.generate({"prompt": "two"})
            self.assertEqual(pid, self.runtime.process.pid)
            for _ in range(40):
                await asyncio.sleep(0.02)
                if self.runtime.process is None:
                    break
            self.assertIsNone(self.runtime.process)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_error_kills_worker_and_recovers(self):
        with self.assertRaises(WorkerError):
            await self.runtime.generate({"prompt": "error"})
        self.assertIsNone(self.runtime.process)
        await self.runtime.generate({"prompt": "fine"})
        self.assertIsNotNone(self.runtime.process)

    async def test_timeout_kills_worker(self):
        self.runtime.config = replace(self.runtime.config, request_timeout=0.05)
        with self.assertRaises(TimeoutError):
            await self.runtime.generate({"prompt": "timeout"})
        self.assertIsNone(self.runtime.process)
        self.assertEqual(self.runtime.pending, 0)

    async def test_cancellation_and_queue_limit(self):
        self.runtime.config = replace(self.runtime.config, max_queue=1)
        first = asyncio.create_task(self.runtime.generate({"prompt": "timeout"}))
        while not self.runtime.active:
            await asyncio.sleep(0.01)
        with self.assertRaises(Busy):
            await self.runtime.generate({"prompt": "queued"})
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIsNone(self.runtime.process)
        self.assertFalse(self.runtime.lock.locked())

    async def test_fifo(self):
        order = []

        async def job(index):
            await self.runtime.generate({"prompt": str(index)})
            order.append(index)

        await asyncio.gather(*(job(i) for i in range(6)))
        self.assertEqual(order, list(range(6)))


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_web_assets_and_status_do_not_load_or_keep_model_alive(self):
        app = create_app(Config(auth_required=False, worker_command=FAKE, idle_seconds=0.05))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertRegex(page.text, r'<base\s+href="/"')
                for path in ("/assets/app.js", "/assets/style.css", "/status"):
                    self.assertEqual((await client.get(path)).status_code, 200)
                self.assertIsNone(app.state.runtime.process)
                await client.post("/generate", json={"prompt": "one"})
                last_used = app.state.runtime.last_used
                for _ in range(40):
                    await client.get("/")
                    await client.get("/status")
                    await asyncio.sleep(0.02)
                    if app.state.runtime.process is None:
                        break
                self.assertIsNone(app.state.runtime.process)
                self.assertEqual(app.state.runtime.last_used, last_used)

    async def test_web_under_proxy_prefix_and_auth(self):
        with patch.dict(os.environ, {"DIFFUSION_ON_DEMAND_API_KEY": "secret-key"}):
            app = create_app(Config(root_path="/diffusion"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Both paths occur with reverse proxies: stripped or retained prefix.
            for path in ("/", "/diffusion/", "/diffusion"):
                page = await client.get(path, follow_redirects=True)
                self.assertEqual(page.status_code, 200, path)
                self.assertRegex(page.text, r'<base\s+href="/diffusion/"')
                self.assertIn('"auth_required": true', page.text)
                self.assertNotIn("secret-key", page.text)
            for path in ("/assets/app.js", "/diffusion/assets/app.js"):
                self.assertEqual((await client.get(path)).status_code, 200)
            self.assertEqual((await client.get("/diffusion/status")).status_code, 401)
            self.assertEqual(
                (
                    await client.get(
                        "/diffusion/status", headers={"Authorization": "Bearer secret-key"}
                    )
                ).status_code,
                200,
            )
            self.assertIsNone(app.state.runtime.process)

    async def test_prompt_text_is_only_supplied_by_request(self):
        app = create_app(Config(auth_required=False))
        generate = AsyncMock(return_value={"image": "iVBORw0KGgo="})
        app.state.runtime.generate = generate
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for negative in (None, "", "avoid text"):
                payload = {"prompt": "a quiet forest", "seed": 42}
                if negative is not None:
                    payload["negative_prompt"] = negative
                response = await client.post("/generate", json=payload)
                self.assertEqual(response.status_code, 200)
                sent = generate.call_args.args[0]
                self.assertEqual(sent["prompt"], payload["prompt"])
                self.assertEqual(sent["negative_prompt"], negative or "")
            invalid = await client.post(
                "/generate", json={"prompt": "hello", "negative_prompt": "x" * 4001}
            )
            self.assertEqual(invalid.status_code, 422)

    async def test_auth_validation_and_generation(self):
        with patch.dict(os.environ, {"DIFFUSION_ON_DEMAND_API_KEY": "test-key"}):
            app = create_app(Config(worker_command=FAKE))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.get("/options")).status_code, 401)
                self.assertIsNone(app.state.runtime.process)
                self.assertEqual(
                    (await client.post("/generate", json={"prompt": "hello"})).status_code, 401
                )
                client.headers["Authorization"] = "Bearer test-key"
                for value in (
                    {"width": 833},
                    {"height": 1217, "width": 1216},
                    {"prompt": " "},
                    {"steps": 0},
                    {"unknown": True},
                ):
                    response = await client.post("/generate", json={"prompt": "hello", **value})
                    self.assertEqual(response.status_code, 422, response.text)
                response = await client.post("/generate", json={"prompt": "landscape", "seed": 42})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["x-seed"], "42")
                self.assertTrue((await client.get("/status")).json()["loaded"])
                await client.post("/unload")
                self.assertFalse((await client.get("/status")).json()["loaded"])

    def test_config_validation(self):
        for values in (
            {"idle_seconds": 0},
            {"auth_required": "false"},
            {"offload": "x"},
            {"max_queue": True},
        ):
            with self.assertRaises(ValueError):
                Config(**values)

    def test_key_required(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            create_app(Config())
