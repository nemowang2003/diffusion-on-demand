"""GPU dependencies exist only in this disposable subprocess."""

import argparse
import base64
import io
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--offload", choices=["model", "sequential", "none"], required=True)
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr

    def send(value):
        protocol.write(json.dumps(value) + "\n")
        protocol.flush()

    try:
        import torch
        from diffusers import EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline

        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA unavailable. Check the NVIDIA driver and libcuda.so search path."
            )
        pipe = StableDiffusionXLPipeline.from_single_file(
            args.model,
            torch_dtype=torch.float16,
            use_safetensors=True,
        )
        pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
        # Equivalent of --no-half-vae, including under CPU offloading.
        pipe.vae.to(dtype=torch.float32)
        pipe.vae.enable_slicing()
        pipe.vae.enable_tiling()
        if args.offload == "model":
            pipe.enable_model_cpu_offload()
        elif args.offload == "sequential":
            pipe.enable_sequential_cpu_offload()
        else:
            pipe.to("cuda")
        send({"ready": True})
        for line in sys.stdin:
            request = json.loads(line)
            try:
                with torch.inference_mode():
                    latents = pipe(
                        prompt=request["prompt"],
                        negative_prompt=request["negative_prompt"],
                        width=request["width"],
                        height=request["height"],
                        num_inference_steps=request["steps"],
                        guidance_scale=request["cfg_scale"],
                        # SDXL default is penultimate, equivalent to WebUI skip 2.
                        clip_skip=None,
                        output_type="latent",
                        generator=torch.Generator(device="cpu").manual_seed(request["seed"]),
                    ).images
                    # Decode explicitly: SDXL's built-in upcast branch assumes
                    # an FP16 VAE and otherwise leaves latents in FP16. Keeping
                    # the VAE in FP32 requires matching input dtype here.
                    decoded = pipe.vae.decode(
                        latents.to(dtype=torch.float32) / pipe.vae.config.scaling_factor,
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
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        send({"error": f"Inference failed: {type(exc).__name__}. See service logs."})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
