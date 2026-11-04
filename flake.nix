{
  description = "On-demand local SDXL image generation";
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
  };
  outputs = {
    self,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
    ...
  }: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};
    workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = self;};
    project = pyproject-nix.lib.project.loadPyproject {projectRoot = self;};
  in {
    lib.mkPythonSet = {
      pkgs,
      python ? pkgs.python313,
    }:
      (pkgs.callPackage pyproject-nix.build.packages {inherit python;}).overrideScope (pkgs.lib.composeManyExtensions [
        pyproject-build-systems.overlays.wheel
        (workspace.mkPyprojectOverlay {sourcePreference = "wheel";})
        (final: prev: {
          torch = prev.torch.overrideAttrs (old: let
            cuda-libraries = with final; [
              nvidia-cublas
              nvidia-cuda-cupti
              nvidia-cuda-nvrtc
              nvidia-cuda-runtime
              nvidia-cudnn-cu13
              nvidia-cufft
              nvidia-cufile
              nvidia-cusolver
              nvidia-cusparse
              nvidia-cusparselt-cu13
              nvidia-nccl-cu13
              nvidia-nvjitlink
              nvidia-nvshmem-cu13
            ];
          in {
            buildInputs = (old.buildInputs or []) ++ cuda-libraries;
            preFixup =
              (old.preFixup or "")
              + pkgs.lib.concatMapStringsSep "\n"
              (dependency: "addAutoPatchelfSearchPath ${dependency}/${python.sitePackages}/nvidia")
              cuda-libraries;
            # The installed NVIDIA driver supplies this at runtime.
            autoPatchelfIgnoreMissingDeps = ["libcuda.so.1"];
            passthru = (old.passthru or {}) // {inherit cuda-libraries;};
          });
          nvidia-cusolver = prev.nvidia-cusolver.overrideAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [final.nvidia-cublas final.nvidia-cusparse final.nvidia-nvjitlink];
            preFixup =
              (old.preFixup or "")
              + ''
                addAutoPatchelfSearchPath ${final.nvidia-cublas}/${python.sitePackages}/nvidia/cu13/lib
                addAutoPatchelfSearchPath ${final.nvidia-cusparse}/${python.sitePackages}/nvidia/cu13/lib
                addAutoPatchelfSearchPath ${final.nvidia-nvjitlink}/${python.sitePackages}/nvidia/cu13/lib
              '';
          });
          nvidia-cudnn-cu13 = prev.nvidia-cudnn-cu13.overrideAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [final.nvidia-cublas];
            preFixup =
              (old.preFixup or "")
              + ''
                addAutoPatchelfSearchPath ${final.nvidia-cublas}/${python.sitePackages}/nvidia/cu13/lib
              '';
          });
          nvidia-cufile = prev.nvidia-cufile.overrideAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [pkgs.rdma-core];
          });
          nvidia-nvshmem-cu13 = prev.nvidia-nvshmem-cu13.overrideAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [pkgs.rdma-core];
            # Optional distributed NVSHMEM plugins are unused by single-GPU SDXL.
            autoPatchelfIgnoreMissingDeps = [
              "libcuda.so.1"
              "libnvidia-ml.so.1"
              "libmpi.so.40"
              "libfabric.so.1"
              "libpmix.so.2"
              "liboshmem.so.40"
              "libucs.so.0"
              "libucp.so.0"
            ];
          });
          diffusion-on-demand = prev.diffusion-on-demand.overrideAttrs (old: {
            uvBuildFlags = (old.uvBuildFlags or []) ++ ["--force-pep517"];
            nativeBuildInputs = (old.nativeBuildInputs or []) ++ final.resolveBuildSystem {uv-build = [];};
            meta = (old.meta or {}) // project.renderers.meta {};
          });
        })
      ]);
    lib.mkPackage = {
      pkgs,
      inference ? true,
      python ? pkgs.python313,
      driverPath ? "/run/opengl-driver/lib",
    }: let
      python-set = self.lib.mkPythonSet {inherit pkgs python;};
      environment = python-set.mkVirtualEnv "diffusion-on-demand-env" {
        diffusion-on-demand = pkgs.lib.optional inference "inference";
      };
      app = (pkgs.callPackages pyproject-nix.build.util {}).mkApplication {
        venv = environment;
        package = python-set.diffusion-on-demand;
      };
    in
      app.overrideAttrs (old: {
        nativeBuildInputs = (old.nativeBuildInputs or []) ++ [pkgs.makeWrapper];
        postFixup =
          (old.postFixup or "")
          + pkgs.lib.optionalString inference ''
            # cuDNN loads its engines with dlopen; ELF RPATH alone is insufficient.
            cuda_dirs=$(find ${pkgs.lib.escapeShellArgs (map (dependency: "${dependency}/${python.sitePackages}/nvidia") python-set.torch.cuda-libraries)} \
              -name '*.so*' -printf '%h\n' | sort -u | paste -sd :)
            wrapProgram "$out/bin/diffusion-on-demand" --prefix LD_LIBRARY_PATH : "$cuda_dirs":${pkgs.lib.escapeShellArg driverPath}
          '';
        passthru = (old.passthru or {}) // {python-env = environment;};
      });
    packages.${system} = {
      default = self.lib.mkPackage {inherit pkgs;};
      gateway = self.lib.mkPackage {
        inherit pkgs;
        inference = false;
      };
      wsl = self.lib.mkPackage {
        inherit pkgs;
        driverPath = "/usr/lib/wsl/lib:/run/opengl-driver/lib";
      };
    };
    apps.${system}.default = {
      type = "app";
      program = "${self.packages.${system}.default}/bin/diffusion-on-demand";
      meta = project.renderers.meta {};
    };
    homeManagerModules.default = import ./nix/home-manager.nix {mkPackage = self.lib.mkPackage;};
    checks.${system} = let
      package = self.packages.${system}.gateway;
    in {
      home-manager = let
        configs = import ./tests/home-manager.nix {
          inherit pkgs;
          module = self.homeManagerModules.default;
        };
      in
        pkgs.runCommand "diffusion-on-demand-home-manager-check" {} ''
          ${package.python-env}/bin/python - <<'PYTHON'
          import tomllib
          from pathlib import Path
          assert tomllib.loads(Path("${configs.unauthenticated}").read_text())["auth_required"] is False
          assert tomllib.loads(Path("${configs.authenticated}").read_text())["auth_required"] is True
          PYTHON
          touch "$out"
        '';
      inference = pkgs.runCommand "diffusion-on-demand-inference-import" {} ''
        ${self.packages.${system}.default.python-env}/bin/python - <<'PYTHON'
        import inspect
        import diffusers
        from diffusion_on_demand.options import Generate
        from diffusion_on_demand.worker import SCHEDULERS, make_scheduler, pipeline_options
        for sampler in SCHEDULERS:
            request = Generate(prompt="test", sampler=sampler).model_dump()
            scheduler = make_scheduler(diffusers, {}, request)
            scheduler.set_timesteps(4)
        request = Generate(prompt="test", sampler="dpmpp_2m", scheduler_options={"use_karras_sigmas": True}).model_dump()
        make_scheduler(diffusers, {}, request).set_timesteps(4)
        assert set(pipeline_options(request)) <= set(inspect.signature(diffusers.StableDiffusionXLPipeline.__call__).parameters)
        PYTHON
        touch "$out"
      '';
      cli = pkgs.runCommand "diffusion-on-demand-cli-check" {} ''
        ${package}/bin/diffusion-on-demand --help
        touch "$out"
      '';
      unit = pkgs.runCommand "diffusion-on-demand-unit-check" {} ''
        export PYTHONDONTWRITEBYTECODE=1
        export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
        ${package.python-env}/bin/python -m unittest discover -s ${self}/tests
        touch "$out"
      '';
    };
    devShells.${system}.default = pkgs.mkShell {
      packages = [pkgs.uv pkgs.python313];
      UV_PYTHON = "${pkgs.python313}/bin/python";
      UV_PYTHON_DOWNLOADS = "never";
      LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [pkgs.stdenv.cc.cc.lib pkgs.zlib] + ":/usr/lib/wsl/lib:/run/opengl-driver/lib";
    };
    formatter.${system} = pkgs.alejandra;
  };
}
