import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    model: str = str(
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "diffusion-on-demand/models/pony-v6-xl.safetensors"
    )
    idle_seconds: float = 30
    startup_timeout: float = 600
    request_timeout: float = 600
    queue_timeout: float = 3600
    max_queue: int = 16
    auth_required: bool = True
    offload: str = "model"
    root_path: str = ""
    # SDXL already selects the penultimate CLIP layer. WebUI Clip skip 2 maps
    # to Diffusers clip_skip=None, NOT clip_skip=2 (which skips two extra layers).
    prompt_prefix: str = "score_9, score_8_up, score_7_up, score_6_up, source_anime, rating_safe, 1girl, solo, long hair, detailed eyes, beautiful face"
    negative_prompt: str = "score_4, score_5, score_6, low quality, worst quality, bad anatomy, bad hands, extra fingers, deformed, blurry, watermark, text"
    worker_command: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("idle_seconds", "startup_timeout", "request_timeout", "queue_timeout"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value < float("inf")
            ):
                raise ValueError(f"{name} must be finite and positive")
        if type(self.max_queue) is not int or self.max_queue < 1:
            raise ValueError("max_queue must be a positive integer")
        if type(self.auth_required) is not bool:
            raise ValueError("auth_required must be boolean")
        if self.offload not in ("model", "sequential", "none"):
            raise ValueError("offload must be model, sequential or none")
        if not isinstance(self.root_path, str) or (
            self.root_path and (not self.root_path.startswith("/") or self.root_path.endswith("/"))
        ):
            raise ValueError(
                "root_path must be empty or an absolute URL path without trailing slash"
            )
        for name in ("model", "prompt_prefix", "negative_prompt"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if not isinstance(self.worker_command, (list, tuple)) or not all(
            isinstance(x, str) and x for x in self.worker_command
        ):
            raise ValueError("worker_command must be an array of nonempty strings")

    @classmethod
    def load(cls, path: str | None):
        explicit = path is not None
        if path is None:
            path = str(
                Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                / "diffusion-on-demand/config.toml"
            )
        source = Path(path)
        if explicit or source.exists():
            return cls(**tomllib.loads(source.read_text()))
        return cls()
