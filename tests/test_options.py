import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import ValidationError
from test_service import FAKE

from diffusion_on_demand.config import Config
from diffusion_on_demand.options import Generate, load_options
from diffusion_on_demand.runtime import Runtime
from diffusion_on_demand.service import create_app
from diffusion_on_demand.worker import make_scheduler, pipeline_options


class OptionTests(unittest.TestCase):
    def test_constraints_and_optional_conditioning(self):
        request = Generate(
            prompt="forest",
            width=1536,
            height=1024,
            steps=80,
            cfg_scale=0,
            prompt_2="",
            negative_prompt_2="",
            clip_skip=1,
            original_size=[1024, 1536],
            timesteps=[999, 500, 0],
        )
        forwarded = pipeline_options(json.loads(request.model_dump_json()))
        self.assertEqual(forwarded["num_inference_steps"], 80)
        self.assertEqual(forwarded["prompt_2"], [""])
        self.assertEqual(forwarded["negative_prompt_2"], [""])
        self.assertEqual(forwarded["original_size"], (1024, 1536))
        self.assertEqual(forwarded["clip_skip"], 1)
        self.assertIsNone(pipeline_options(Generate(prompt="forest").model_dump())["clip_skip"])
        self.assertNotIn("prompt_2", pipeline_options(Generate(prompt="forest").model_dump()))
        for invalid in (
            {"precision": "int8"},
            {"vae_tiling": "false"},
            {"gpu_id": True},
            {"timesteps": [0, 999]},
            {"sigmas": [float("nan"), 0]},
            {"timesteps": [999, 0], "sigmas": [1, 0]},
            {"original_size": [0, 1024]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                Generate(prompt="forest", **invalid)

    def test_scheduler_options_are_checked_and_reset_from_model_config(self):
        class Scheduler:
            def __init__(self, use_karras_sigmas=False):
                pass

            @classmethod
            def from_config(cls, config, **kwargs):
                result = cls()
                result.settings = {**config, **kwargs}
                return result

            def set_timesteps(self, steps, timesteps=None):
                pass

        library = SimpleNamespace(EulerDiscreteScheduler=Scheduler)
        request = Generate(
            prompt="forest", sampler="euler", scheduler_options={"use_karras_sigmas": True}
        ).model_dump()
        first = make_scheduler(library, {"use_karras_sigmas": False}, request)
        self.assertTrue(first.settings["use_karras_sigmas"])
        request["scheduler_options"] = {}
        self.assertFalse(
            make_scheduler(library, {"use_karras_sigmas": False}, request).settings[
                "use_karras_sigmas"
            ]
        )
        request["scheduler_options"] = {"unknown": True}
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            make_scheduler(library, {}, request)
        request["scheduler_options"] = {}
        request["sigmas"] = [1, 0]
        with self.assertRaisesRegex(ValueError, "does not support"):
            make_scheduler(library, {}, request)


class OptionAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_validation_and_forwarding_without_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default = root / "pony.safetensors"
            default.touch()
            other = root / "other.safetensors"
            other.touch()
            app = create_app(
                Config(
                    model=str(default),
                    offload="sequential",
                    auth_required=False,
                    worker_command=FAKE,
                )
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                options = (await client.get("/options")).json()
                self.assertEqual(options["models"], ["other.safetensors", "pony.safetensors"])
                self.assertEqual(options["defaults"]["offload"], "sequential")
                self.assertIsNone(app.state.runtime.process)
                bad = await client.post(
                    "/generate", json={"prompt": "forest", "model": "../other.safetensors"}
                )
                self.assertEqual(bad.status_code, 422)
                self.assertIsNone(app.state.runtime.process)
                request = {
                    "prompt": "forest",
                    "model": "other.safetensors",
                    "precision": "float32",
                    "vae_tiling": False,
                    "sampler": "dpmpp_2m",
                    "scheduler_options": {"use_karras_sigmas": True},
                }
                response = await client.post("/generate", json=request)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(app.state.runtime.loaded_options["model"], str(other))
                self.assertEqual(app.state.runtime.loaded_options["precision"], "float32")
                self.assertFalse(app.state.runtime.loaded_options["vae_tiling"])

    async def test_load_changes_restart_only_when_each_queued_request_runs(self):
        runtime = Runtime(Config(auth_required=False, worker_command=FAKE))
        try:
            base = Generate(prompt="forest").model_dump()
            first = await runtime.generate(base)
            same = await runtime.generate({**base, "sampler": "heun", "steps": 42})
            self.assertEqual(first["pid"], same["pid"])
            results = await asyncio.gather(
                runtime.generate({**base, "precision": "float32"}),
                runtime.generate({**base, "offload": "sequential"}),
                runtime.generate(base),
            )
            self.assertEqual(len({item["pid"] for item in results} | {first["pid"]}), 4)
            self.assertEqual(results[0]["load_options"]["precision"], "float32")
            self.assertEqual(results[1]["load_options"]["offload"], "sequential")
            self.assertEqual(results[2]["load_options"], load_options(base, runtime.config))
        finally:
            await runtime.close()
