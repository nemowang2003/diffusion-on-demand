import asyncio
import base64
import contextlib
import hmac
import html
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from .config import Config
from .options import Generate, model_catalog
from .runtime import Busy, Runtime, WorkerError


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
    assets = Path(__file__).with_name("web")

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        # Explicit assets avoid nested mount root-path ambiguity when Serve
        # strips the prefix before proxying the request.
        if name not in {"app.js", "zip.js", "favicon.svg", "style.css", "model-presets.json"}:
            raise HTTPException(404, "Asset not found")
        return FileResponse(assets / name, headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        # An absolute base also works when Serve receives /diffusion without a
        # trailing slash. Never derive it from forwarded Host headers.
        base = request.scope.get("root_path", "").rstrip("/") + "/"
        settings = json.dumps(
            {
                "auth_required": config.auth_required,
                "idle_seconds": config.idle_seconds,
            }
        ).replace("<", "\\u003c")
        page = (assets / "index.html").read_text()
        return HTMLResponse(
            page.replace("__BASE_PATH__", html.escape(base, quote=True)).replace(
                "__UI_SETTINGS__", settings
            ),
            headers={"Cache-Control": "no-cache"},
        )

    async def authorize(request: Request):
        if config.auth_required and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {key}".encode()
        ):
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/options", dependencies=[Depends(authorize)])
    async def options():
        return {
            "schema": Generate.model_json_schema(),
            "models": list(model_catalog(config)),
            "defaults": {"model": Path(config.model).name, "offload": config.offload},
        }

    @app.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return {
            "loaded": runtime.process is not None and runtime.process.returncode is None,
            "active": runtime.active,
            "pending": runtime.pending,
            "idle_seconds": config.idle_seconds,
            "load_options": {
                **runtime.loaded_options,
                "model": Path(runtime.loaded_options["model"]).name,
            }
            if runtime.loaded_options
            else None,
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
        models = model_catalog(config)
        selected = body.model or Path(config.model).name
        if selected not in models:
            raise HTTPException(422, "Unknown model; choose an installed SDXL model from /options")
        payload["model"] = models[selected]
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
