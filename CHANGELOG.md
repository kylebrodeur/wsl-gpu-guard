# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.2.1] – 2026-03-24

### Fixed

- `uninstall` now removes `~/.config/environment.d/cuda-wheels.conf` and
  clears `LD_LIBRARY_PATH` from the live systemd user session so no stale
  paths remain after removal.
- `uninstall-service` now removes `/tmp/.wsl-gpu-guard.pid` if it exists
  after stopping the service, preventing the Windows PowerShell script from
  sending SIGUSR1 to an unrelated process on a stale PID.
- `cuda-setup` no longer appends `:$LD_LIBRARY_PATH` to the env file value.
  When `LD_LIBRARY_PATH` was unset in the systemd environment, the variable
  expanded to an empty string, leaving a trailing colon that caused the
  dynamic linker to search the current working directory.

---

## [0.2.0] – 2026-03-24

### Added

- `cuda-setup` subcommand — discovers nvidia wheel lib dirs across configured Python
  environments and writes `~/.config/environment.d/cuda-wheels.conf` so that
  `libcublas.so.12` is available on `LD_LIBRARY_PATH` for every new systemd user session.
  No per-project path hacks needed.
- `--venv PATH` flag on `cuda-setup` — adds a venv root (or project directory containing
  `.venv`) to the scan. Stored in config so future runs work without the flag.
- `[cuda]` section in `~/.config/wsl-gpu-guard/config.toml` with `extra_venvs` list.
- `CudaConfig` dataclass and `save_cuda_venvs()` helper in `config.py`.
- `cmd_install` now runs `cuda-setup` automatically as part of the one-shot setup.

### Fixed

- Systemd service was crashing on every start because `_build_service_unit` placed CLI
  flags before the `watch` subcommand (e.g. `wsl-gpu-guard --signal SIGHUP watch`). Fixed
  to `wsl-gpu-guard watch --signal SIGHUP`.

---

## [0.1.0] – 2026-03-24

### Added

- `GpuWatchdog` — background thread that polls `/dev/dxg` and sends configurable
  signals to watched processes when the GPU is hot-removed or reconnected.
- `get_gpu_using_pids()` — scans `/proc/*/fd` to find only the processes that have
  `/dev/dxg` open, so innocent processes (VSCode, terminals) are never signalled.
- `check_rtld_global_cuda_libs()` / `warn_rtld_global_cuda()` — detects CUDA
  libraries loaded with `RTLD_GLOBAL`, a known cause of WSL2 GPU crashes.
- `windows/on-ac-disconnect.ps1` — bundled PowerShell script that fires a Windows
  toast notification and sends `SIGUSR1` to the watchdog daemon before the dGPU
  powers down on AC unplug. Bundled inside the Python wheel.
- SIGUSR1 handler in `GpuWatchdog` — enables pre-emptive signalling from the
  Windows side before `/dev/dxg` actually disappears.
- Reconnect signalling — optional signal sent to watched processes when the GPU
  reappears (e.g. AC plugged back in).
- PID file at `/tmp/.wsl-gpu-guard.pid` — written on `start()`, removed on
  `stop()`, so the PowerShell script can find the daemon without manual config.
- `config.py` — user config at `~/.config/wsl-gpu-guard/config.toml` (stdlib
  `tomllib`, zero runtime dependencies).
- Full CLI:
  - `install` — one-shot: write config, enable systemd user service, register
    Windows Task Scheduler task.
  - `uninstall` — tear down service and task.
  - `status` — GPU state, active PIDs, service state, task state, RTLD warnings.
  - `config` — view or initialise the config file.
  - `watch` — run the daemon directly.
  - `install-service` / `uninstall-service` — manage the systemd unit.
  - `install-task` / `uninstall-task` — manage the Windows Task Scheduler task.
- Systemd user service generation from current config (auto-restarts on failure).
- PyPI-ready packaging: wheel bundles the PowerShell script; zero runtime deps.

[Unreleased]: https://github.com/kylebrodeur/wsl-gpu-guard/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/kylebrodeur/wsl-gpu-guard/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kylebrodeur/wsl-gpu-guard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kylebrodeur/wsl-gpu-guard/releases/tag/v0.1.0
