import argparse
import fcntl
import os
import socket
from pathlib import Path

from .config import Config


def endpoint_path():
    root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/diffusion-on-demand-{os.getuid()}"))
    return root / "diffusion-on-demand/endpoint"


def main():
    parser = argparse.ArgumentParser(description="On-demand local SDXL generation API")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--endpoint-file", type=Path, default=endpoint_path())
    endpoint = sub.add_parser("endpoint")
    endpoint.add_argument("--endpoint-file", type=Path, default=endpoint_path())
    sub.add_parser("download-model")
    sub.add_parser("verify-model")
    args = parser.parse_args()
    if args.command == "endpoint":
        print(args.endpoint_file.read_text().strip())
        return
    config = Config.load(args.config)
    if args.command in ("download-model", "verify-model"):
        from .download import download, verify

        if args.command == "download-model":
            download(config.model)
        elif not verify(Path(config.model).expanduser()):
            parser.exit(1, "Model SHA-256 mismatch\n")
        else:
            print("Model SHA-256 verified")
        return
    import uvicorn

    from .service import create_app

    os.umask(0o077)
    args.endpoint_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.endpoint_file.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another instance owns this endpoint file\n")
        args.endpoint_file.unlink(missing_ok=True)
        # Bind once; no probe-and-release race. The actual port is discoverable.
        family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.host, args.port))
            listener.listen(128)
            host = (
                "127.0.0.1"
                if args.host == "0.0.0.0"
                else ("::1" if args.host == "::" else args.host)
            )
            host = f"[{host}]" if ":" in host else host
            url = f"http://{host}:{listener.getsockname()[1]}"
            app = create_app(config, endpoint_file=args.endpoint_file, endpoint=url)
            uvicorn.Server(uvicorn.Config(app, log_level="info", timeout_graceful_shutdown=15)).run(
                sockets=[listener]
            )
