# wsl-gpu-guard

Graceful GPU hot-removal protection for WSL2 on NVIDIA Optimus (hybrid-graphics) laptops.

On Optimus laptops the discrete GPU powers off when you unplug AC power. In WSL2 this
causes `/dev/dxg` (the kernel bridge to the Windows GPU driver) to disappear. Any process
holding an open CUDA context at that moment will crash — taking WSL2 down with it.

`wsl-gpu-guard` prevents that crash by:

1. **Proactively** — a Windows Task Scheduler task fires the bundled `on-ac-disconnect.ps1`
   the moment AC is unplugged. It sends SIGUSR1 to the watchdog daemon running in WSL2,
   which then signals your CUDA processes to release the GPU *before* it powers down.
2. **Reactively** — the watchdog polls `/dev/dxg` every 2 seconds and fires again if the
   device disappears unexpectedly (driver crash, sleep, etc.).
3. **Safely** — by default the watchdog sends **SIGHUP** (not SIGTERM), so a well-behaved
   server falls back to CPU and keeps running rather than dying.

---

## Requirements

- WSL2 on Windows 10/11
- NVIDIA Optimus laptop (or any machine where the GPU can be hot-removed)
- Python 3.11+
- `uv` (recommended) or pip
- systemd enabled in WSL2 (required for the auto-start service — see below)

### Enable systemd in WSL2

If not already enabled, add this to `/etc/wsl.conf` inside WSL2, then restart:

```ini
[boot]
systemd=true
```

```powershell
# In Windows PowerShell / CMD:
wsl --shutdown
```

---

## Installation

```bash
# From the repo root
uv sync
uv run wsl-gpu-guard --version
```

To install as a system tool (available everywhere in your WSL2 session):

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

This single command:
1. Writes a default config to `~/.config/wsl-gpu-guard/config.toml`
2. Installs and enables a **systemd user service** that starts the watchdog automatically on every WSL2 boot
3. Registers a **Windows Task Scheduler task** that fires `on-ac-disconnect.ps1` on AC unplug and sleep

### Check everything

```bash
wsl-gpu-guard status
```

Output includes: GPU device state, which PIDs have `/dev/dxg` open, systemd service status,
Windows task state, and any RTLD_GLOBAL warnings.

### Remove everything

```bash
wsl-gpu-guard uninstall
```

The config file at `~/.config/wsl-gpu-guard/config.toml` is kept — delete it manually if desired.

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

# Auto-detect GPU-using processes (ignores VSCode, terminals, etc.)
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
        ▼  (8 second grace period in on-ac-disconnect.ps1)
GPU powers down safely — no crash
```

If `/dev/dxg` later disappears anyway (driver crash, unexpected removal), the polling
loop fires a second time as a backstop.

---

## Testing & verification

### Unit tests

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

All 55 tests run in under a second and require no GPU or WSL2-specific environment.

### Verify the installation

```bash
wsl-gpu-guard status
```

Expected output when AC is plugged in and the GPU is on:

```
/dev/dxg  : present
GPU PIDs  : [1234, 5678]  (processes with /dev/dxg open)
Service   : active, enabled  (~/.config/systemd/user/wsl-gpu-guard.service)
Win task  : Ready  (wsl-gpu-guard-ac-disconnect)
```

When on battery (Optimus GPU off):

```
/dev/dxg  : absent
           GPU not accessible — battery power (Optimus) or no NVIDIA GPU
```

### Smoke test: watchdog fires on GPU removal

Run this in one terminal to watch the watchdog signal itself:

```bash
wsl-gpu-guard watch --self --signal SIGHUP --interval 1 --no-rtld-check
```

Then in another terminal, simulate GPU removal by renaming the device node (requires root):

```bash
# Simulate removal (root required)
sudo mv /dev/dxg /dev/dxg.bak
# Watchdog should log the removal and send SIGHUP within 1 second
sudo mv /dev/dxg.bak /dev/dxg
# Watchdog should log the reappearance
```

### Smoke test: pre-emptive SIGUSR1 path

With the watchdog running (any `watch` invocation), send SIGUSR1 directly to simulate
what the Windows PowerShell script does:

```bash
# Get the watchdog PID
cat /tmp/.wsl-gpu-guard.pid

# Simulate the Windows AC-disconnect trigger
kill -s USR1 $(cat /tmp/.wsl-gpu-guard.pid)
```

The watchdog should immediately log `SIGUSR1 received (Windows AC-disconnect event)` and
signal any watched processes.

### Test the Windows Task Scheduler task

After running `wsl-gpu-guard install-task`, verify it appears in Task Scheduler:

```powershell
# In Windows PowerShell:
Get-ScheduledTask -TaskName "wsl-gpu-guard-ac-disconnect"
```

To trigger it manually (simulates AC unplug without actually unplugging):

```powershell
Start-ScheduledTask -TaskName "wsl-gpu-guard-ac-disconnect"
```

You should see the Windows toast notification and the watchdog log `SIGUSR1 received`
within a second.

### Check GPU-using PIDs

```bash
uv run python -c "from wsl_gpu_guard.watchdog import get_gpu_using_pids; print(get_gpu_using_pids())"
```

Returns a list of PIDs with `/dev/dxg` open. Should include any running CUDA processes and
exclude VSCode, terminals, etc.

### Check RTLD_GLOBAL status

```bash
wsl-gpu-guard status
```

If any CUDA libs are loaded globally (a crash risk), the status output ends with:

```
[WARNING] RTLD_GLOBAL CUDA libs in this process: libcublas.so.12
  Fix: use LD_LIBRARY_PATH instead of ctypes.CDLL(..., mode=RTLD_GLOBAL).
```

### Follow watchdog logs

```bash
journalctl --user -u wsl-gpu-guard -f
```

---

## CLI reference

### `wsl-gpu-guard install`

Full one-time setup: write config, install systemd user service, register Windows Task
Scheduler task. Safe to re-run.

### `wsl-gpu-guard uninstall`

Stop and remove the systemd service and Windows task. Config file is kept.

### `wsl-gpu-guard status`

Show `/dev/dxg` presence, GPU-using PIDs, service state, Windows task state, and RTLD_GLOBAL
warnings for the current process.

### `wsl-gpu-guard config [--init]`

Show the current config file, or write the default config if `--init` is passed and no file
exists yet.

### `wsl-gpu-guard watch [options]`

Start the watchdog daemon directly (bypassing the systemd service).

| Option | Default | Description |
|--------|---------|-------------|
| `--pid PID` | — | PID to signal (repeatable). Mutually exclusive with `--self`/`--parent`. |
| `--self` | — | Signal this process (useful for testing). |
| `--parent` | — | Signal the parent process. |
| `--gpu-only` | off | Auto-detect GPU-using PIDs from `/proc/*/fd` at fire time. Ignored if `--pid` is set. |
| `--signal` | `SIGHUP` (from config) | Signal sent on GPU removal. |
| `--reconnect-signal` | `SIGHUP` (from config) | Signal sent when GPU reappears. |
| `--interval` | `2.0` (from config) | Poll interval in seconds. |
| `--no-rtld-check` | off | Skip the RTLD_GLOBAL CUDA lib check at startup. |

All options default to values from `~/.config/wsl-gpu-guard/config.toml` when the file
exists. CLI flags override config values.

### `wsl-gpu-guard install-service` / `uninstall-service`

Install or remove the systemd user service independently of the Windows task.

### `wsl-gpu-guard install-task` / `uninstall-task`

Register or remove the Windows Task Scheduler task independently of the systemd service.
Requires `powershell.exe` in PATH (standard on WSL2).

---

## Python API

```python
import os
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

Note: the CLI and config layer default `signal_name` to `"SIGHUP"` — the Python class
default of `"SIGTERM"` only applies when using the API directly without a config file.

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
ctranslate2 / faster-whisper / your application
```

**Do NOT install** `nvidia-driver`, `cuda-drivers`, or any package that installs a Linux
NVIDIA kernel module inside WSL2. The Windows driver handles everything. Installing a Linux
driver will conflict with the WSL2 bridge and cause crashes.

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

## Troubleshooting

### `nvidia-smi` returns "Failed to initialize NVML: N/A"

The GPU is currently powered off. On Optimus laptops this happens on battery power. Plug
in AC and try again.

### `/dev/dxg` is present but CUDA returns no devices

Same cause — dGPU is off. `/dev/dxg` is always present (it's the driver stub), but CUDA
returns `CUDA_ERROR_NO_DEVICE (100)` when the hardware is off.

### Watchdog fires immediately on start

`/dev/dxg` may not exist on this machine (no NVIDIA GPU, or the dGPU is powered off on
battery). The watchdog logs a warning at startup. Run `wsl-gpu-guard status` to diagnose.

### `install` fails with "systemd is not running"

Enable systemd in `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then run `wsl --shutdown` from Windows and reopen WSL2.

### PowerShell script not found during `install-task`

The script is bundled inside the installed Python package. If you see this error, the
package may not be properly installed. Try:

```bash
uv tool install .      # from the repo root
wsl-gpu-guard install-task
```

### SIGUSR1 has no effect

The watchdog may not be running. Check:

```bash
wsl-gpu-guard status        # is the service active?
cat /tmp/.wsl-gpu-guard.pid # does the PID file exist?
```

If the PID file exists but the process is gone, the watchdog crashed — check logs:

```bash
journalctl --user -u wsl-gpu-guard -n 50
```
