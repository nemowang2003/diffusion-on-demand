"""Download a pinned Civitai artifact; never install an unverified checkpoint."""

import hashlib
import os
import tempfile
from contextlib import closing
from pathlib import Path

import httpx

MODEL_VERSION = 290640
FILE_ID = 228616
SHA256 = "67ab2fd8ec439a89b3fedb15cc65f54336af163c7eb5e4f2acc98f090a29b0b3"
URL = f"https://civitai.com/api/download/models/{MODEL_VERSION}?fileId={FILE_ID}"
MIRROR = "https://huggingface.co/AstraliteHeart/pony-diffusion-v6/resolve/5ec9c05863255568f1b59753e3838107befaa712/v6.safetensors"


def verify(path: Path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == SHA256


def download(destination: str):
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if verify(target):
            print(f"Already verified: {target}")
            return
        raise ValueError(f"Existing file fails SHA-256 verification: {target}")
    headers = {}
    if token := os.environ.get("CIVITAI_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    # Secrets are sent only to Civitai. httpx drops Authorization on redirects
    # to the artifact CDN, and credentials never appear in query strings.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(output.name, 0o600)
            with httpx.Client(follow_redirects=True, timeout=120) as client:
                response = client.send(
                    client.build_request("GET", URL, headers=headers), stream=True
                )
                if response.status_code in (401, 403):
                    response.close()
                    print("Civitai denied download; trying the author mirror (identical SHA-256).")
                    response = client.send(client.build_request("GET", MIRROR), stream=True)
                with closing(response):
                    if response.status_code in (401, 403):
                        raise ValueError(
                            "Civitai denied download. Set CIVITAI_TOKEN, or download manually and run verify-model."
                        )
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
        if not verify(temporary):
            raise ValueError("Downloaded model SHA-256 mismatch; file discarded")
        # Do not overwrite a model downloaded concurrently.
        os.link(temporary, target)
        print(f"Downloaded and verified: {target}")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
