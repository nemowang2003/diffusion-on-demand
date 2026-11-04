import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from diffusion_on_demand import download as module


class DownloadTests(unittest.TestCase):
    def test_civitai_fallback_verifies_identical_artifact_without_leaking_token(self):
        artifact = b"test model"
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.host == "civitai.com":
                return httpx.Response(403)
            return httpx.Response(200, content=artifact)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.safetensors"
            with (
                patch.object(module.httpx, "Client", return_value=client),
                patch.object(module, "SHA256", hashlib.sha256(artifact).hexdigest()),
                patch.dict(module.os.environ, {"CIVITAI_TOKEN": "secret"}),
            ):
                module.download(str(target))
                self.assertEqual(target.read_bytes(), artifact)
                module.download(str(target))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].headers["Authorization"], "Bearer secret")
            self.assertNotIn("Authorization", calls[1].headers)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_corrupt_download_is_not_installed(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"bad"))
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.safetensors"
            with (
                patch.object(module.httpx, "Client", return_value=client),
                self.assertRaises(ValueError),
            ):
                module.download(str(target))
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])
