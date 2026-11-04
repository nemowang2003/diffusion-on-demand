"""One FIFO queue and one disposable inference process. No GPU imports here."""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time

from .config import Config


class Busy(Exception):
    pass


class WorkerError(Exception):
    pass


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.lock = asyncio.Lock()
        self.process = None
        self.pending = 0
        self.active = False
        self.last_used = time.monotonic()
        self.closed = False

    async def stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        # Kill the group even if its leader has already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    async def _line(self):
        line = await self.process.stdout.readline()
        if not line:
            raise WorkerError("Inference process exited; see service logs")
        try:
            response = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise WorkerError("Invalid inference process response") from exc
        if not isinstance(response, dict):
            raise WorkerError("Invalid inference process response")
        if response.get("error"):
            raise WorkerError(response["error"])
        return response

    async def start(self):
        if self.process is not None and self.process.returncode is None:
            return
        await self.stop()
        command = self.config.worker_command or (sys.executable, "-m", "diffusion_on_demand.worker")
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                "--model",
                self.config.model,
                "--offload",
                self.config.offload,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr goes to journal; stdout contains only the private JSON protocol.
                start_new_session=True,
                limit=32 * 1024 * 1024,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            raise
        response = await asyncio.wait_for(self._line(), self.config.startup_timeout)
        if response != {"ready": True}:
            raise WorkerError("Inference process did not become ready")

    async def generate(self, request: dict):
        if self.closed:
            raise Busy("Service is shutting down")
        if self.pending >= self.config.max_queue:
            raise Busy("Generation queue is full")
        self.pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.lock.acquire(), self.config.queue_timeout)
                acquired = True
            except TimeoutError as exc:
                raise Busy("Timed out waiting for generation queue") from exc
            if self.closed:
                raise Busy("Service is shutting down")
            self.active = True
            try:
                await self.start()
                self.process.stdin.write(json.dumps(request).encode() + b"\n")
                await self.process.stdin.drain()
                return await asyncio.wait_for(self._line(), self.config.request_timeout)
            except BaseException:
                # Cancellation, timeout and OOM all destroy the worker before the
                # next request is allowed to enter. A failed GPU context is never reused.
                await self.stop()
                raise
            finally:
                self.active = False
                self.last_used = time.monotonic()
        finally:
            self.pending -= 1
            if acquired:
                self.lock.release()

    async def unload(self):
        async with self.lock:
            await self.stop()

    async def reap(self):
        while True:
            await asyncio.sleep(min(self.config.idle_seconds, 1))
            if self.pending or self.lock.locked():
                continue
            async with self.lock:
                if (
                    not self.pending
                    and time.monotonic() - self.last_used >= self.config.idle_seconds
                ):
                    await self.stop()

    async def close(self):
        self.closed = True
        async with self.lock:
            await self.stop()
