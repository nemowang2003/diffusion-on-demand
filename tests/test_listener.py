import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx


class ListenerTests(unittest.TestCase):
    def test_allocated_endpoint_readiness_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("auth_required = false\n")
            endpoint = root / "endpoint"
            command = [
                sys.executable,
                "-c",
                "from diffusion_on_demand import main; main()",
                "--config",
                str(config),
                "serve",
                "--endpoint-file",
                str(endpoint),
            ]
            process = subprocess.Popen(
                command,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(200):
                    if endpoint.exists():
                        break
                    self.assertIsNone(process.poll())
                    time.sleep(0.02)
                url = endpoint.read_text().strip()
                with httpx.Client(base_url=url, timeout=5) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertFalse(client.get("/status").json()["loaded"])
                self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
                other = subprocess.run(command, capture_output=True, timeout=10)
                self.assertNotEqual(other.returncode, 0)
                self.assertIn(b"Another instance", other.stderr)
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            self.assertFalse(endpoint.exists())
