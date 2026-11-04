import asyncio
import base64
import contextlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Config
from .runtime import Busy, Runtime, WorkerError


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    width: int = Field(default=832, ge=512, le=1216, strict=True)
    height: int = Field(default=1216, ge=512, le=1216, strict=True)
    steps: int = Field(default=30, ge=1, le=50, strict=True)
    cfg_scale: float = Field(default=6.5, ge=1, le=15, allow_inf_nan=False)
    seed: int = Field(default=-1, ge=-1, le=2**32 - 1, strict=True)

    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 64 or self.height % 64 or self.width * self.height > 1024 * 1024:
            raise ValueError(
                "Dimensions must be multiples of 64 and total at most 1024x1024 pixels"
            )
        if not self.prompt.strip():
            raise ValueError("Prompt must not be blank")
        return self


def create_app(config: Config, *, endpoint_file: Path | None = None, endpoint: str = ""):
    key = os.environ.get("DIFFUSION_ON_DEMAND_API_KEY", "")
    if config.auth_required and not key:
        raise ValueError("Set DIFFUSION_ON_DEMAND_API_KEY or explicitly set auth_required=false")
    runtime = Runtime(config)

    @asynccontextmanager
    async def lifespan(app):
        reaper = asyncio.create_task(runtime.reap())
        if endpoint_file:
            temporary = endpoint_file.with_suffix(".tmp")
            temporary.write_text(endpoint + "\n")
            temporary.chmod(0o600)
            temporary.replace(endpoint_file)
        notify = os.environ.get("NOTIFY_SOCKET")
        if notify:
            import socket

            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect("\0" + notify[1:] if notify.startswith("@") else notify)
                sock.sendall(b"READY=1")
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            await runtime.close()
            if endpoint_file:
                endpoint_file.unlink(missing_ok=True)

    app = FastAPI(title="diffusion-on-demand", lifespan=lifespan, root_path=config.root_path)
    app.state.runtime = runtime

    async def authorize(request: Request):
        if config.auth_required and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {key}".encode()
        ):
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return {
            "loaded": runtime.process is not None and runtime.process.returncode is None,
            "active": runtime.active,
            "pending": runtime.pending,
            "idle_seconds": config.idle_seconds,
        }

    @app.post("/unload", dependencies=[Depends(authorize)])
    async def unload():
        await runtime.unload()
        return {"loaded": False}

    @app.post(
        "/generate",
        dependencies=[Depends(authorize)],
        responses={200: {"content": {"image/png": {}}}},
    )
    async def generate(body: Generate):
        payload = body.model_dump()
        payload["seed"] = secrets.randbits(32) if body.seed == -1 else body.seed
        payload["prompt"] = ", ".join(filter(None, (config.prompt_prefix, body.prompt)))
        payload["negative_prompt"] = config.negative_prompt
        try:
            result = await runtime.generate(payload)
        except Busy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "Inference timed out; worker stopped") from exc
        except (WorkerError, OSError) as exc:
            raise HTTPException(503, str(exc)) from exc
        return Response(
            base64.b64decode(result["image"], validate=True),
            media_type="image/png",
            headers={"X-Seed": str(payload["seed"]), "Cache-Control": "no-store"},
        )

    return app
