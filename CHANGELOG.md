# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

[Unreleased]: https://github.com/kylebrodeur/wsl-gpu-guard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kylebrodeur/wsl-gpu-guard/releases/tag/v0.1.0
