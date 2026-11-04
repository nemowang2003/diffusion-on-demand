import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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
    async def test_auth_validation_and_generation(self):
        with patch.dict(os.environ, {"DIFFUSION_ON_DEMAND_API_KEY": "test-key"}):
            app = create_app(Config(worker_command=FAKE))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertIsNone(app.state.runtime.process)
                self.assertEqual(
                    (await client.post("/generate", json={"prompt": "hello"})).status_code, 401
                )
                client.headers["Authorization"] = "Bearer test-key"
                for value in (
                    {"width": 833},
                    {"height": 1216, "width": 1216},
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
