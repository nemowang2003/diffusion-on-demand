"""GPU dependencies exist only in this disposable subprocess."""

import argparse
import base64
import inspect
import io
import json
import sys

SCHEDULERS = {
    "euler_a": ("EulerAncestralDiscreteScheduler", {}),
    "euler": ("EulerDiscreteScheduler", {}),
    "ddim": ("DDIMScheduler", {}),
    "dpmpp_2m": (
        "DPMSolverMultistepScheduler",
        {"algorithm_type": "dpmsolver++", "solver_order": 2},
    ),
    "dpmpp_2m_sde": (
        "DPMSolverMultistepScheduler",
        {"algorithm_type": "sde-dpmsolver++", "solver_order": 2},
    ),
    "unipc": ("UniPCMultistepScheduler", {}),
    "heun": ("HeunDiscreteScheduler", {}),
    "lms": ("LMSDiscreteScheduler", {}),
}


def make_scheduler(library, config, request):
    name, defaults = SCHEDULERS[request["sampler"]]
    cls = getattr(library, name)
    overrides = {**defaults, **request["scheduler_options"]}
    unknown = set(overrides) - (set(inspect.signature(cls.__init__).parameters) - {"self"})
    if unknown:
        raise ValueError(f"Unsupported {name} options: {', '.join(sorted(unknown))}")
    scheduler = cls.from_config(config, **overrides)
    for key in ("timesteps", "sigmas"):
        if (
            request.get(key) is not None
            and key not in inspect.signature(scheduler.set_timesteps).parameters
        ):
            raise ValueError(f"{name} does not support custom {key}")
    return scheduler


def pipeline_options(request):
    # Never pass loading options or scheduler constructors to pipeline.__call__.
    options = {
        "prompt": request["prompt"],
        "negative_prompt": request["negative_prompt"],
        "width": request["width"],
        "height": request["height"],
        "num_inference_steps": request["steps"],
        "guidance_scale": request["cfg_scale"],
        "clip_skip": request["clip_skip"] or None,
        **{
            key: request[key]
            for key in (
                "prompt_2",
                "negative_prompt_2",
                "guidance_rescale",
                "eta",
                "denoising_end",
                "timesteps",
                "sigmas",
                "original_size",
                "target_size",
                "crops_coords_top_left",
                "negative_original_size",
                "negative_target_size",
                "negative_crops_coords_top_left",
            )
            if request.get(key) is not None
        },
    }
    # The subprocess protocol uses JSON arrays. SDXL concatenates these values
    # with its tuple defaults, so restore tuples before calling the pipeline.
    for key in (
        "original_size",
        "target_size",
        "crops_coords_top_left",
        "negative_original_size",
        "negative_target_size",
        "negative_crops_coords_top_left",
    ):
        if key in options:
            options[key] = tuple(options[key])
    # Diffusers treats a bare empty string as "inherit prompt". A one-item
    # batch preserves an explicitly empty second encoder prompt.
    for key in ("prompt_2", "negative_prompt_2"):
        if options.get(key) == "":
            options[key] = [""]
    return options


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--load-options", required=True)
    args = parser.parse_args()
    options = json.loads(args.load_options)
    protocol = sys.stdout
    sys.stdout = sys.stderr

    def send(value):
        protocol.write(json.dumps(value) + "\n")
        protocol.flush()

    try:
        import diffusers
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA unavailable. Check the NVIDIA driver and libcuda.so search path."
            )
        torch.cuda.set_device(options["gpu_id"])
        device = f"cuda:{options['gpu_id']}"
        if (
            "bfloat16" in (options["precision"], options["vae_precision"])
            and not torch.cuda.is_bf16_supported()
        ):
            raise ValueError("The selected CUDA device does not support bfloat16")
        pipe = diffusers.StableDiffusionXLPipeline.from_single_file(
            options["model"],
            torch_dtype=getattr(torch, options["precision"]),
            use_safetensors=True,
        )
        scheduler_config = dict(pipe.scheduler.config)
        vae_dtype = getattr(torch, options["vae_precision"])
        pipe.vae.to(dtype=vae_dtype)
        if options["vae_slicing"]:
            pipe.vae.enable_slicing()
        if options["vae_tiling"]:
            pipe.vae.enable_tiling()
        if options["attention_slicing"] != "off":
            pipe.enable_attention_slicing(options["attention_slicing"])
        if options["offload"] == "model":
            pipe.enable_model_cpu_offload(gpu_id=options["gpu_id"])
        elif options["offload"] == "sequential":
            pipe.enable_sequential_cpu_offload(gpu_id=options["gpu_id"])
        else:
            pipe.to(device)
        send({"ready": True})
        for line in sys.stdin:
            request = json.loads(line)
            try:
                pipe.scheduler = make_scheduler(diffusers, scheduler_config, request)
                with torch.inference_mode():
                    latents = pipe(
                        **pipeline_options(request),
                        output_type="latent",
                        generator=torch.Generator(
                            device="cpu" if request["generator_device"] == "cpu" else device
                        ).manual_seed(request["seed"]),
                    ).images
                    # Decode explicitly to match the independently chosen VAE
                    # precision, including with CPU offload hooks installed.
                    decoded = pipe.vae.decode(
                        latents.to(dtype=vae_dtype) / pipe.vae.config.scaling_factor,
                        return_dict=False,
                    )[0]
                    image = pipe.image_processor.postprocess(decoded, output_type="pil")[0]
                    pipe.maybe_free_model_hooks()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                send({"image": base64.b64encode(buffer.getvalue()).decode()})
            except torch.cuda.OutOfMemoryError:
                send(
                    {
                        "error": 'CUDA out of memory. Try 768x1024 or offload="sequential"; close other GPU workloads.'
                    }
                )
                return
            except (ValueError, TypeError) as exc:
                send({"error": str(exc)})
                return
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        send({"error": f"Inference failed: {type(exc).__name__}. See service logs."})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
