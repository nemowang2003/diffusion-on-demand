"""Public single-image SDXL options; importing this module never imports GPU libraries."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, model_validator

from .config import Config

Precision = Literal["float16", "bfloat16", "float32"]
Offload = Literal["model", "sequential", "none"]
Sampler = Literal["euler_a", "euler", "ddim", "dpmpp_2m", "dpmpp_2m_sde", "unipc", "heun", "lms"]
Dimension = Annotated[int, Field(strict=True, ge=1, le=16384)]
Coordinate = Annotated[int, Field(strict=True, ge=0, le=16384)]
Timestep = Annotated[int, Field(strict=True, ge=0, le=9999)]
Sigma = Annotated[FiniteFloat, Field(ge=0)]


def option(default, title, group, description="", **kwargs):
    return Field(
        default=default,
        title=title,
        description=description,
        json_schema_extra={"group": group},
        **kwargs,
    )


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=4000)
    width: int = Field(default=832, ge=64, le=8192, strict=True)
    height: int = Field(default=1216, ge=64, le=8192, strict=True)
    steps: int = Field(default=30, ge=1, le=1000, strict=True)
    cfg_scale: FiniteFloat = Field(default=6.5, ge=0, le=100)
    seed: int = Field(default=-1, ge=-1, le=2**32 - 1, strict=True)
    model: str | None = option(
        None, "模型权重", "模型与显存", "选择服务器已安装的 SDXL 单文件模型。"
    )
    offload: Offload | None = option(
        None,
        "显存卸载策略",
        "模型与显存",
        "model：模块卸载到 CPU；sequential：更省显存但更慢；none：模型常驻 GPU，直至空闲卸载。",
    )
    precision: Precision = option("float16", "模型精度", "模型与显存")
    vae_precision: Precision = option(
        "float32", "VAE 精度", "模型与显存", "默认 FP32 避免部分 SDXL VAE 在半精度下溢出。"
    )
    gpu_id: int = option(0, "CUDA 设备编号", "模型与显存", ge=0, le=63, strict=True)
    vae_slicing: bool = option(True, "VAE slicing", "模型与显存", strict=True)
    vae_tiling: bool = option(True, "VAE tiling", "模型与显存", strict=True)
    attention_slicing: Literal["off", "auto", "max"] = option(
        "off", "注意力切片", "模型与显存", "off 使用 PyTorch SDPA；切片会降低速度。"
    )
    sampler: Sampler = option("euler_a", "采样器", "采样设置")
    scheduler_options: dict[str, JsonValue] = option(
        {},
        "采样器高级配置（JSON）",
        "采样设置",
        '所选 Diffusers scheduler 的构造参数，例如 {"use_karras_sigmas": true}；不支持的参数会报错。',
    )
    guidance_rescale: FiniteFloat = option(0, "CFG rescale", "采样设置", ge=0, le=1)
    clip_skip: int = option(
        0,
        "额外跳过 CLIP 层数",
        "采样设置",
        "0 使用 SDXL 默认的倒数第二层（相当于 WebUI Clip skip 2）。",
        ge=0,
        le=10,
        strict=True,
    )
    eta: FiniteFloat = option(0, "DDIM eta", "采样设置", "仅 DDIM 采样器使用。", ge=0, le=1)
    denoising_end: FiniteFloat | None = option(
        None, "去噪结束比例", "采样设置", "留空执行完整去噪。", gt=0, lt=1
    )
    generator_device: Literal["cpu", "cuda"] = option(
        "cpu", "随机数生成器设备", "采样设置", "切换设备可能改变相同 seed 的结果。"
    )
    timesteps: list[Timestep] | None = option(
        None,
        "自定义 timesteps（JSON 数组）",
        "采样设置",
        "降序整数数组，覆盖步数；需采样器支持。",
        min_length=1,
        max_length=1000,
    )
    sigmas: list[Sigma] | None = option(
        None,
        "自定义 sigmas（JSON 数组）",
        "采样设置",
        "降序数值数组，覆盖步数；与 timesteps 二选一，需采样器支持。",
        min_length=2,
        max_length=1001,
    )
    prompt_2: str | None = option(
        None,
        "第二文本编码器提示词",
        "SDXL 条件",
        '留空框表示继承主提示词；输入 JSON 空字符串 "" 可显式设为空。',
        max_length=4000,
    )
    negative_prompt_2: str | None = option(
        None,
        "第二文本编码器负向提示词",
        "SDXL 条件",
        '留空框表示继承主负向提示词；输入 JSON 空字符串 "" 可显式设为空。',
        max_length=4000,
    )
    original_size: tuple[Dimension, Dimension] | None = option(
        None, "原始尺寸 [高, 宽]", "SDXL 条件", "JSON 数组；留空跟随生成尺寸。"
    )
    target_size: tuple[Dimension, Dimension] | None = option(
        None, "目标尺寸 [高, 宽]", "SDXL 条件", "影响尺寸条件，不改变实际输出尺寸。"
    )
    crops_coords_top_left: tuple[Coordinate, Coordinate] = option(
        (0, 0), "裁剪起点 [上, 左]", "SDXL 条件"
    )
    negative_original_size: tuple[Dimension, Dimension] | None = option(
        None, "负向原始尺寸 [高, 宽]", "SDXL 条件"
    )
    negative_target_size: tuple[Dimension, Dimension] | None = option(
        None, "负向目标尺寸 [高, 宽]", "SDXL 条件"
    )
    negative_crops_coords_top_left: tuple[Coordinate, Coordinate] = option(
        (0, 0), "负向裁剪起点 [上, 左]", "SDXL 条件"
    )

    @model_validator(mode="after")
    def validate_generation(self):
        if self.width % 8 or self.height % 8:
            raise ValueError("Dimensions must be multiples of 8")
        if not self.prompt.strip():
            raise ValueError("Prompt must not be blank")
        if self.timesteps is not None and self.sigmas is not None:
            raise ValueError("Set timesteps or sigmas, not both")
        for values in (self.timesteps, self.sigmas):
            if values is not None and any(a <= b for a, b in zip(values, values[1:])):
                raise ValueError("Timesteps and sigmas must be strictly descending")
        return self


LOAD_FIELDS = (
    "model",
    "offload",
    "precision",
    "vae_precision",
    "gpu_id",
    "vae_slicing",
    "vae_tiling",
    "attention_slicing",
)


def load_options(request: dict, config: Config) -> dict:
    defaults = {name: Generate.model_fields[name].default for name in LOAD_FIELDS}
    defaults.update(model=config.model, offload=config.offload)
    return {
        name: request.get(name) if request.get(name) is not None else defaults[name]
        for name in LOAD_FIELDS
    }


def model_catalog(config: Config) -> dict:
    default = Path(config.model).expanduser()
    models = {
        path.name: str(path.resolve())
        for path in sorted(default.parent.glob("*.safetensors"))
        if path.is_file()
    }
    models[default.name] = str(default.resolve())
    return models
