import json
import unittest
from pathlib import Path

from diffusion_on_demand import service
from diffusion_on_demand.options import Generate


class PresetTests(unittest.TestCase):
    def test_curated_presets_are_valid_requests_with_fixed_artifacts(self):
        presets = json.loads(
            Path(service.__file__).with_name("web").joinpath("model-presets.json").read_text()
        )
        self.assertEqual(len(presets), 3)
        for name, preset in presets.items():
            with self.subTest(model=name):
                request = Generate(model=name, **preset["parameters"])
                self.assertEqual(request.sampler, "euler" if "Vpred" in name else "euler_a")
                if "Vpred" in name:
                    self.assertEqual(request.scheduler_options["prediction_type"], "v_prediction")
                    self.assertTrue(request.scheduler_options["rescale_betas_zero_snr"])
                self.assertRegex(preset["artifact"]["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(preset["artifact"]["url"].startswith("https://"))
                self.assertNotIn("/resolve/main/", preset["artifact"]["url"])
