# diffusion-on-demand

本地 SDXL 生成 API。轻量 HTTP 服务常驻，收到生成请求才启动独立的
Diffusers / PyTorch 进程。请求共用 FIFO 队列，空闲 30 秒退出推理进程，
释放模型、CPU 内存和 CUDA 上下文。状态查询不会加载模型。

后端直接加载 Pony Diffusion V6 XL 的 `.safetensors`，无需 Forge。
这是独立 API，不提供 Forge WebUI、Forge API 协议或 ADetailer 扩展。
默认提示词使用 `rating_safe`。自动人脸/手部重绘尚未实现。

## 使用

目标平台：x86_64 Linux + NVIDIA CUDA，包括 NixOS WSL。

```sh
# 默认模型位于 ~/.local/share/diffusion-on-demand/models/pony-v6-xl.safetensors
nix run . -- download-model
# WSL 使用驱动路径适配过的包：
nix build .#wsl
```

下载器固定 Civitai model 257749 / version 290640 / file 228616，
只接受 SHA-256 为 `67ab2fd8ec439a89b3fedb15cc65f54336af163c7eb5e4f2acc98f090a29b0b3`
的文件。优先 Civitai；401/403 时使用作者 AstraliteHeart 在 Hugging Face 的
相同文件镜像，固定 revision。可通过 `CIVITAI_TOKEN` 提供凭据。
模型不放入 Git 或 Nix store；手动下载后可用 `verify-model` 校验。
下载需要约 6.94 GB 空间；失败时清理临时文件，不覆盖已有文件。

创建 `~/.config/diffusion-on-demand/config.toml`：

```toml
idle_seconds = 30
offload = "model"
# 仅当入口已经由 Tailscale Serve 等限制访问时显式关闭鉴权。
auth_required = false
```

```sh
./result/bin/diffusion-on-demand serve
base=$(./result/bin/diffusion-on-demand endpoint)
curl --fail-with-body "$base/generate" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"an adult woman wearing a blue coat in a snowy garden","seed":42}' \
  -o image.png
curl "$base/status"
curl -X POST "$base/unload"
```

默认开启 Bearer 鉴权，需在启动环境设置 `DIFFUSION_ON_DEMAND_API_KEY`，
客户端发送 `Authorization: Bearer KEY`。`/health` 不鉴权。
每次只返回一张 PNG，随机 seed 通过响应头 `X-Seed` 返回。
不在服务端保存生成图片。`/docs` 提供交互式 API 文档。

| 生成字段 | 默认值 | 范围 |
| --- | --- | --- |
| `prompt` | 必填 | 非空，最多 4000 字符 |
| `width` / `height` | 832 / 1216 | 512–1216，64 的倍数，总像素不超过 1024² |
| `steps` | 30 | 1–50 |
| `cfg_scale` | 6.5 | 1–15 |
| `seed` | -1（随机） | -1 或 0–2³²−1 |

采样器固定 Euler ancestral。默认正向质量/风格前缀和负向提示词位于
`config.py`，可通过配置的 `prompt_prefix`、`negative_prompt` 更改。
SDXL 的 Diffusers 默认取 CLIP 倒数第二层，对齐 WebUI 的 Clip skip 2；
直接传 `clip_skip=2` 会额外跳层。跨后端相同 seed 不保证相同图片。

VAE 使用 FP32，启用 VAE slicing/tiling，注意力采用 PyTorch SDPA。
`offload="model"` 将暂不用的模块移至 CPU；显存不足可先试 768×1024 或
`offload="sequential"`（更慢），或关闭其他 GPU 工作负载。
本服务不协调 llama-on-demand 等其他应用的显存使用。
首次加载还会从 Hugging Face 下载 SDXL 配置和 tokenizer；之后使用缓存。
不要把 Python 依赖锁定等同于模型配置已离线缓存。

队列默认最多 16 个请求（含正在执行的请求），超限/排队超时返回 429；
加载失败、OOM 返回 503，加载或生成超时返回 504。失败会退出推理进程，
下一请求重新加载。默认加载/生成分别限时 600 秒，排队限时 3600 秒；
可设 `startup_timeout`、`request_timeout`、`queue_timeout`。

## Home Manager

```nix
{
  imports = [inputs.diffusion.homeManagerModules.default];
  services.diffusion-on-demand = {
    enable = true;
    package = inputs.diffusion.lib.mkPackage {
      inherit pkgs;
      driverPath = "/usr/lib/wsl/lib:/run/opengl-driver/lib"; # WSL
    };
    settings = {
      model = "${config.xdg.dataHome}/diffusion-on-demand/models/pony-v6-xl.safetensors";
      auth_required = false; # 入口由 Tailnet 控制时
      idle_seconds = 30;
      offload = "model";
    };
  };
}
```

模块启动 `diffusion-on-demand.service`，TCP 默认端口 0，由 OS 分配。
实际 URL 原子写入 `$XDG_RUNTIME_DIR/diffusion-on-demand/endpoint` 后通知
systemd READY。`diffusion-on-demand endpoint` 可发现入口。
`ExecStartPost` 可读取 `services.diffusion-on-demand.endpoint-file` 发布该端点；
`%t` 由 systemd 展开。停止服务时杀死整个进程组。
发布至 Tailscale Serve 的 `/diffusion/` 时，外部接口为
`https://<tailscale-host>/diffusion/generate`，代理需移除该路径前缀，并配置 `root_path="/diffusion"` 使 API 文档正确寻址。
每次重启服务都要重新发布新端口；主机的 Windows/Tailscale 操作留在主机配置。

```sh
systemctl --user status diffusion-on-demand
journalctl --user -u diffusion-on-demand
```

需要 API key 或 HF_TOKEN 时用模块的 `environment-file`，文件权限 0600，
不要将密钥写入 Nix store。默认服务鉴权开启；省略环境文件不会绕过鉴权。

## 开发与构建

```sh
nix develop
uv sync --locked
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests -v
nix flake check
nix build .#wsl
```

uv 管理 Python 依赖，uv2nix 从 `uv.lock` 打包。生产包只含运行依赖；
GPU 依赖属于 `inference` extra，只在子进程里导入。`packages.default` 包含
推理依赖，`packages.wsl` 增加 WSL 驱动路径，`packages.gateway` 不含推理依赖，
用于轻量检查或配合自定义 `worker_command`。
`lib.mkPythonSet`、`lib.mkPackage` 支持其他 flake 复用构建基础设施。
单元测试使用独立假 worker 验证队列、空闲退出、取消、超时、错误恢复和鉴权，
不下载模型或占用 GPU。
