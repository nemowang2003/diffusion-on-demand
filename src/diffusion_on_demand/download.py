"""Download a pinned Civitai artifact; never install an unverified checkpoint."""

import hashlib
import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

import httpx


def artifact_for(path: Path):
    presets = json.loads(Path(__file__).with_name("web").joinpath("model-presets.json").read_text())
    if path.name not in presets:
        raise ValueError(
            f"No pinned download for {path.name}; choose a filename from model-presets.json"
        )
    return presets[path.name]["artifact"]


def verify(path: Path, sha256: str | None = None):
    expected = sha256 or artifact_for(path)["sha256"]
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == expected


def download(destination: str):
    target = Path(destination).expanduser()
    artifact = artifact_for(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if verify(target):
            print(f"Already verified: {target}")
            return
        raise ValueError(f"Existing file fails SHA-256 verification: {target}")
    headers = {}
    if httpx.URL(artifact["url"]).host == "civitai.com" and (
        token := os.environ.get("CIVITAI_TOKEN")
    ):
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
                    client.build_request("GET", artifact["url"], headers=headers), stream=True
                )
                with closing(response):
                    if response.status_code in (401, 403):
                        raise ValueError(
                            "Download denied. Set the appropriate credentials, or download manually and run verify-model."
                        )
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
        if not verify(temporary, artifact["sha256"]):
            raise ValueError("Downloaded model SHA-256 mismatch; file discarded")
        # Do not overwrite a model downloaded concurrently.
        os.link(temporary, target)
        print(f"Downloaded and verified: {target}")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
