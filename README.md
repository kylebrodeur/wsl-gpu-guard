# wsl-gpu-guard

Graceful GPU hot-removal protection for WSL2 on NVIDIA Optimus (hybrid-graphics) laptops.

On Optimus laptops the discrete GPU powers off when you unplug AC power. In WSL2 this
causes `/dev/dxg` (the kernel bridge to the Windows GPU driver) to disappear. Any process
holding an open CUDA context at that moment will crash — taking WSL2 down with it.

`wsl-gpu-guard` prevents that crash by:

1. **Proactively** — a Windows Task Scheduler task runs `scripts/on-ac-disconnect.ps1 (bundled in package)` the
   moment AC is unplugged. It sends SIGUSR1 to the watchdog daemon running in WSL2, which
   then signals your CUDA processes to release the GPU *before* it powers down.
2. **Reactively** — the watchdog polls `/dev/dxg` every 2 seconds and fires again if the
   device disappears unexpectedly (driver crash, sleep, etc.).
3. **Safely** — by default the watchdog sends **SIGHUP** (not SIGTERM), so a well-behaved
   server (like the audio-transcription API) falls back to CPU and keeps running rather than
   dying.

---

## Requirements

- WSL2 on Windows 10/11
- NVIDIA Optimus laptop (or any machine where the GPU can be hot-removed)
- Python 3.11+
- `uv` (recommended) or pip

---

## Installation

```bash
# From the repo root
uv sync
uv run wsl-gpu-guard --version
```

To install as a system tool:

```bash
uv tool install .
wsl-gpu-guard --version
```

---

## Quick start

### One-time setup

```bash
wsl-gpu-guard install
```

That's it. This single command:
1. Writes a config file at `~/.config/wsl-gpu-guard/config.toml`
2. Installs and enables a **systemd user service** that starts the watchdog automatically on every WSL2 boot
3. Registers a **Windows Task Scheduler task** that fires `on-ac-disconnect.ps1` when you unplug AC or the machine sleeps

### Check everything

```bash
wsl-gpu-guard status
```

Shows: GPU device state, which PIDs have `/dev/dxg` open, systemd service status, Windows task state, and any RTLD_GLOBAL warnings.

### Remove everything

```bash
wsl-gpu-guard uninstall
```

### Customise

```bash
wsl-gpu-guard config          # view current config
wsl-gpu-guard config --init   # write default config if none exists
$EDITOR ~/.config/wsl-gpu-guard/config.toml
wsl-gpu-guard install-service # re-install service after config changes
```

### Manual control (without the systemd service)

```bash
# Watch a specific process
wsl-gpu-guard watch --pid 1234 --signal SIGHUP

# Auto-detect GPU-using processes (safe for VSCode sessions)
wsl-gpu-guard watch --gpu-only --signal SIGHUP --reconnect-signal SIGHUP
```

---

## Signal flow

```
AC unplug detected by Windows
        │
        ▼
Task Scheduler fires on-ac-disconnect.ps1
        │  (reads /tmp/.wsl-gpu-guard.pid)
        ▼
wsl.exe kill -s USR1 <watchdog-pid>
        │
        ▼
GpuWatchdog._handle_sigusr1()  ← pre-emptive, before GPU powers off
        │  fires _fire(removed=True)
        ▼
os.kill(<server-pid>, SIGHUP)
        │
        ▼
Server SIGHUP handler: release CUDA, switch to CPU, keep serving
        │
        ▼  (8 second grace period in .ps1)
GPU powers down safely — no crash
```

If `/dev/dxg` later disappears anyway (driver crash, unexpected removal), the polling
loop fires a second time as a backstop.

---

## Publishing to PyPI

```bash
# Build
uv build

# Upload (requires PyPI account + API token)
uv publish --token $PYPI_TOKEN
```

Once published, users install and set up with:

```bash
pip install wsl-gpu-guard
# or
uv tool install wsl-gpu-guard

wsl-gpu-guard install
```

---

## CLI reference

### `wsl-gpu-guard install`

Full one-time setup. Writes config, installs systemd service, registers Windows task.

### `wsl-gpu-guard uninstall`

Removes the systemd service and Windows task. Config file is kept.

### `wsl-gpu-guard status`

Check whether `/dev/dxg` is present, list GPU-using PIDs, and check for RTLD_GLOBAL
CUDA libs.

### `wsl-gpu-guard watch [options]`

Start the watchdog daemon.

| Option | Default | Description |
|--------|---------|-------------|
| `--pid PID` | — | PID to signal (repeatable). Mutually exclusive with `--self`/`--parent`. |
| `--self` | — | Signal this process (useful for testing). |
| `--parent` | — | Signal the parent process. |
| `--gpu-only` | off | Auto-detect GPU-using PIDs from `/proc/*/fd` at fire time. Ignored if `--pid` is set. |
| `--signal` | `SIGTERM` | Signal sent on GPU removal. Use `SIGHUP` for graceful CPU fallback. |
| `--reconnect-signal` | none | Signal sent when GPU reappears (e.g. `SIGHUP` to re-enable CUDA). |
| `--interval` | `2.0` | Poll interval in seconds. |
| `--no-rtld-check` | off | Skip the RTLD_GLOBAL CUDA lib check at startup. |

### `wsl-gpu-guard install-task`

Register the Windows Task Scheduler task that runs `scripts/on-ac-disconnect.ps1 (bundled in package)`
on AC disconnect and sleep events. Requires `powershell.exe` in PATH.

### `wsl-gpu-guard uninstall-task`

Remove the Task Scheduler task.

---

## Python API

```python
from wsl_gpu_guard.watchdog import GpuWatchdog, get_gpu_using_pids

# Basic usage — watch a known PID
dog = GpuWatchdog(pids=[os.getpid()], signal_name="SIGHUP")
dog.start()

# GPU-only auto-detect — only signals CUDA-using processes
dog = GpuWatchdog(gpu_only=True, signal_name="SIGHUP", reconnect_signal_name="SIGHUP")
dog.start()

# As an async context manager
async with GpuWatchdog.async_context(pids=[server_pid], signal_name="SIGHUP") as dog:
    await run_server()

# Query GPU-using PIDs directly
pids = get_gpu_using_pids()
print(f"Processes with /dev/dxg open: {pids}")
```

### `GpuWatchdog` parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pids` | `list[int]` | `[]` | PIDs to signal on GPU removal. |
| `signal_name` | `str` | `"SIGTERM"` | Signal sent on removal. |
| `reconnect_signal_name` | `str\|None` | `None` | Signal sent when GPU reappears. |
| `on_remove_callback` | `callable\|None` | `None` | Called before signals are sent on removal. |
| `on_reconnect_callback` | `callable\|None` | `None` | Called when GPU reappears. |
| `poll_interval` | `float` | `2.0` | Seconds between `/dev/dxg` checks. |
| `gpu_only` | `bool` | `False` | Auto-detect GPU-using PIDs at fire time (ignored if `pids` is set). |
| `check_rtld_global` | `bool` | `True` | Warn at startup if CUDA libs are loaded with `RTLD_GLOBAL`. |
| `dxg_path` | `Path` | `/dev/dxg` | Override the device path (useful for testing). |

---

## RTLD_GLOBAL safety check

Loading CUDA shared libraries with `ctypes.CDLL(lib, mode=RTLD_GLOBAL)` injects their
symbols into the process-global symbol table. In WSL2 this can corrupt the CUDA driver's
internal symbol resolution (which routes through `/usr/lib/wsl/lib/libcuda.so.1`) and
cause the GPU to crash.

`wsl-gpu-guard status` and `wsl-gpu-guard watch` (at startup) both check for this
condition using `RTLD_NOLOAD | RTLD_GLOBAL` probing and log a warning with a fix hint.

**The fix** — instead of loading CUDA libs with `RTLD_GLOBAL`, prepend the lib directories
to `LD_LIBRARY_PATH` before any CUDA operations:

```python
import os, site
from pathlib import Path

def prepend_cuda_wheel_paths() -> None:
    lib_dirs = []
    for root in site.getsitepackages() + [site.getusersitepackages()]:
        for lib_dir in (Path(root) / "nvidia").glob("*/lib"):
            if lib_dir.is_dir():
                lib_dirs.append(str(lib_dir.resolve()))
    if lib_dirs:
        current = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(lib_dirs) + (f":{current}" if current else "")
```

---

## WSL2 CUDA stack

The correct stack on WSL2 (nothing extra to install in Linux):

```
Windows NVIDIA driver  (installed on Windows side only)
        │
        ▼
/usr/lib/wsl/lib/libcuda.so.1   ← provided by WSL2, registered via ld.wsl.conf
        │
        ▼
libcublas.so.12 / libcudnn.so.9  ← from nvidia-cublas-cu12 / nvidia-cudnn-cu12 Python wheels
        │                           (or system CUDA toolkit — NOT the full Linux NVIDIA driver)
        ▼
ctranslate2 / faster-whisper
```

**Do NOT install** `nvidia-driver`, `cuda-drivers`, or any package that installs a Linux
NVIDIA kernel module inside WSL2. The Windows driver handles everything. Installing a Linux
driver will conflict with the WSL2 bridge and cause crashes.

---

## Troubleshooting

### `nvidia-smi` returns "Failed to initialize NVML: N/A"

The GPU is currently powered off. On Optimus laptops this happens on battery power. Plug
in AC and try again.

### `/dev/dxg` is present but `ctranslate2.get_cuda_device_count()` returns 0

Same cause — dGPU is off. `/dev/dxg` is always present (it's the driver stub), but CUDA
returns `CUDA_ERROR_NO_DEVICE (100)` when the hardware is off.

### Watchdog fires immediately on start

`/dev/dxg` may not exist (no NVIDIA GPU, or not on WSL2). Run `wsl-gpu-guard status` to
diagnose.

### PowerShell script not found during `install-task`

Run `install-task` from the `wsl-gpu-guard` source directory, or ensure the `windows/`
directory is present next to the installed package.
