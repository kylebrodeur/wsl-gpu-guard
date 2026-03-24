"""CLI entry point for wsl-gpu-guard.

Commands
--------
wsl-gpu-guard install          Full setup: systemd service + Windows task (run once)
wsl-gpu-guard uninstall        Tear down everything installed by 'install'
wsl-gpu-guard watch            Run the watchdog daemon directly
wsl-gpu-guard status           Show GPU state, active PIDs, service status
wsl-gpu-guard install-service  Install + enable the systemd user service
wsl-gpu-guard uninstall-service Stop + remove the systemd user service
wsl-gpu-guard install-task     Register the Windows Task Scheduler task
wsl-gpu-guard uninstall-task   Remove the Windows Task Scheduler task
wsl-gpu-guard config           Show or initialise the config file
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from wsl_gpu_guard import __version__
from wsl_gpu_guard import config as _cfg
from wsl_gpu_guard.watchdog import (
    DXG_DEVICE,
    GpuWatchdog,
    check_rtld_global_cuda_libs,
    get_gpu_using_pids,
)

logger = logging.getLogger("wsl_gpu_guard")

_SERVICE_NAME = "wsl-gpu-guard"
_TASK_NAME = "wsl-gpu-guard-ac-disconnect"
_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
_SERVICE_FILE = _SERVICE_DIR / f"{_SERVICE_NAME}.service"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


def _powershell(script: str) -> subprocess.CompletedProcess:
    """Run a PowerShell command via powershell.exe (available in WSL2)."""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )


def _windows_script_path() -> Path:
    """Return the bundled on-ac-disconnect.ps1 path (inside the installed package)."""
    return Path(__file__).parent / "scripts" / "on-ac-disconnect.ps1"


def _find_executable() -> str:
    """Return the absolute path to the wsl-gpu-guard executable."""
    found = shutil.which("wsl-gpu-guard")
    if found:
        return found
    # Fallback: same directory as the current Python executable
    candidate = Path(sys.executable).parent / "wsl-gpu-guard"
    if candidate.exists():
        return str(candidate)
    # Last resort: use sys.argv[0] resolved
    return str(Path(sys.argv[0]).resolve())


def _systemctl(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, check=check,
    )


def _systemd_available() -> bool:
    result = _systemctl(["status"], check=False)
    return result.returncode in (0, 3)  # 3 = no units loaded, still running


def _service_active() -> bool:
    r = _systemctl(["is-active", _SERVICE_NAME])
    return r.stdout.strip() == "active"


def _service_enabled() -> bool:
    r = _systemctl(["is-enabled", _SERVICE_NAME])
    return r.stdout.strip() == "enabled"


def _build_service_unit(cfg: _cfg.GuardConfig) -> str:
    """Render the systemd unit file content from current config."""
    exec_path = _find_executable()
    w = cfg.watch

    flags = []
    if w.pids:
        for pid in w.pids:
            flags += ["--pid", str(pid)]
    elif w.gpu_only:
        flags.append("--gpu-only")

    flags += ["--signal", w.signal]
    if w.reconnect_signal:
        flags += ["--reconnect-signal", w.reconnect_signal]
    flags += ["--interval", str(w.poll_interval)]

    exec_start = exec_path + (" " + " ".join(flags) if flags else "") + " watch"

    return f"""\
[Unit]
Description=WSL2 GPU hot-removal watchdog
Documentation=https://github.com/kylebrodeur/wsl-gpu-guard
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    # GPU device
    present = DXG_DEVICE.exists()
    print(f"/dev/dxg  : {'present' if present else 'absent'}")
    if present:
        gpu_pids = get_gpu_using_pids()
        if gpu_pids:
            print(f"GPU PIDs  : {gpu_pids}  (processes with /dev/dxg open)")
        else:
            print("GPU PIDs  : none")
    else:
        print(
            "           GPU not accessible — battery power (Optimus) or no NVIDIA GPU"
        )

    # Systemd service
    if _systemd_available():
        active = "active" if _service_active() else "inactive"
        enabled = "enabled" if _service_enabled() else "disabled"
        print(f"Service   : {active}, {enabled}  ({_SERVICE_FILE})")
    else:
        print("Service   : systemd not available")

    # Windows task
    if shutil.which("powershell.exe"):
        res = _powershell(
            f"(Get-ScheduledTask -TaskName '{_TASK_NAME}' -ErrorAction SilentlyContinue)"
            ".State"
        )
        state = res.stdout.strip() or "not installed"
        print(f"Win task  : {state}  ({_TASK_NAME})")
    else:
        print("Win task  : powershell.exe not found")

    # RTLD_GLOBAL check
    risky = check_rtld_global_cuda_libs()
    if risky:
        print(
            f"\n[WARNING] RTLD_GLOBAL CUDA libs in this process: {', '.join(risky)}\n"
            "  Fix: use LD_LIBRARY_PATH instead of ctypes.CDLL(..., mode=RTLD_GLOBAL)."
        )

    return 0 if present else 1


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------

def cmd_watch(args: argparse.Namespace) -> int:
    # Args override config; config provides defaults
    cfg = _cfg.load()
    w = cfg.watch

    pids: list[int] = []
    if args.pid:
        pids = args.pid
    elif args.self:
        pids = [os.getpid()]
    elif args.parent:
        pids = [os.getppid()]

    sig = args.signal or w.signal
    reconnect_sig = args.reconnect_signal if args.reconnect_signal is not None else w.reconnect_signal
    interval = args.interval if args.interval is not None else w.poll_interval
    gpu_only = args.gpu_only or (w.gpu_only and not pids)

    if not pids and not gpu_only:
        print(
            "No PIDs specified and --gpu-only not set. "
            "The watchdog will log events but not signal anyone.\n"
            "Tip: run 'wsl-gpu-guard install' to set up automatic protection.",
            file=sys.stderr,
        )

    def on_remove():
        print("\n[wsl-gpu-guard] GPU removed — signalling processes.", flush=True)

    def on_reconnect():
        print("\n[wsl-gpu-guard] GPU reappeared — signalling processes.", flush=True)

    dog = GpuWatchdog(
        pids=pids,
        signal_name=sig,
        reconnect_signal_name=reconnect_sig,
        on_remove_callback=on_remove,
        on_reconnect_callback=on_reconnect if reconnect_sig else None,
        poll_interval=interval,
        check_rtld_global=not args.no_rtld_check,
        gpu_only=gpu_only,
    )

    def _shutdown(signum, frame):
        print("\n[wsl-gpu-guard] Shutting down.", file=sys.stderr)
        dog.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    dog.start()
    print(f"[wsl-gpu-guard] Watching {dog.dxg_path} (interval={interval}s)", flush=True)

    try:
        while dog.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dog.stop()

    return 0


# ---------------------------------------------------------------------------
# Subcommand: install-service
# ---------------------------------------------------------------------------

def cmd_install_service(args: argparse.Namespace) -> int:
    if not _systemd_available():
        print(
            "[error] systemd is not running. Enable it in /etc/wsl.conf:\n"
            "  [boot]\n  systemd=true\n"
            "Then restart WSL: wsl --shutdown",
            file=sys.stderr,
        )
        return 1

    cfg = _cfg.load()
    unit = _build_service_unit(cfg)

    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_FILE.write_text(unit, encoding="utf-8")
    print(f"Wrote {_SERVICE_FILE}")

    _systemctl(["daemon-reload"])
    r = _systemctl(["enable", "--now", _SERVICE_NAME])
    if r.returncode != 0:
        print(f"[error] systemctl enable failed:\n{r.stderr or r.stdout}", file=sys.stderr)
        return 1

    print(f"Service '{_SERVICE_NAME}' enabled and started.")
    print("  Logs: journalctl --user -u wsl-gpu-guard -f")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: uninstall-service
# ---------------------------------------------------------------------------

def cmd_uninstall_service(args: argparse.Namespace) -> int:
    _systemctl(["disable", "--now", _SERVICE_NAME])
    if _SERVICE_FILE.exists():
        _SERVICE_FILE.unlink()
        print(f"Removed {_SERVICE_FILE}")
    _systemctl(["daemon-reload"])
    print(f"Service '{_SERVICE_NAME}' removed.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: install-task
# ---------------------------------------------------------------------------

def cmd_install_task(args: argparse.Namespace) -> int:
    if not shutil.which("powershell.exe"):
        print("[error] powershell.exe not found in PATH.", file=sys.stderr)
        return 1

    ps1 = _windows_script_path()
    if not ps1.exists():
        print(f"[error] PowerShell script not found at {ps1}.", file=sys.stderr)
        return 1

    result = subprocess.run(["wslpath", "-w", str(ps1)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[error] wslpath failed: {result.stderr}", file=sys.stderr)
        return 1

    win_ps1 = result.stdout.strip()

    register_cmd = f"""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -WindowStyle Hidden -File "{win_ps1}"'
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

$ev_trigger = Get-CimClass -Namespace ROOT\\Microsoft\\Windows\\TaskScheduler `
    -ClassName MSFT_TaskEventTrigger | New-CimInstance -ClientOnly
$ev_trigger.Subscription = '<QueryList><Query Id="0" Path="System">' +
    '<Select Path="System">*[System[Provider[@Name="Microsoft-Windows-Kernel-Power"] and EventID=105]]</Select>' +
    '</Query></QueryList>'
$ev_trigger.Enabled = $true

$sleep_trigger = Get-CimClass -Namespace ROOT\\Microsoft\\Windows\\TaskScheduler `
    -ClassName MSFT_TaskEventTrigger | New-CimInstance -ClientOnly
$sleep_trigger.Subscription = '<QueryList><Query Id="0" Path="System">' +
    '<Select Path="System">*[System[Provider[@Name="Microsoft-Windows-Kernel-Power"] and EventID=42]]</Select>' +
    '</Query></QueryList>'
$sleep_trigger.Enabled = $true

$task = Register-ScheduledTask `
    -TaskName '{_TASK_NAME}' `
    -Action $action `
    -Trigger @($ev_trigger, $sleep_trigger) `
    -Settings $settings `
    -Principal $principal `
    -Force
if ($task) {{ Write-Output "OK: task registered as '{_TASK_NAME}'" }}
"""

    print(f"Registering Windows Task Scheduler task '{_TASK_NAME}'...")
    res = _powershell(register_cmd)
    if res.returncode != 0 or "OK:" not in res.stdout:
        print(f"[error] Task registration failed:\n{res.stderr or res.stdout}", file=sys.stderr)
        return 1

    print(res.stdout.strip())
    print(
        f"\nTask '{_TASK_NAME}' fires on AC-disconnect (EventID 105) and sleep (EventID 42).\n"
        "Verify in: Task Scheduler → Task Scheduler Library"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: uninstall-task
# ---------------------------------------------------------------------------

def cmd_uninstall_task(args: argparse.Namespace) -> int:
    if not shutil.which("powershell.exe"):
        print("[error] powershell.exe not found in PATH.", file=sys.stderr)
        return 1
    res = _powershell(f"Unregister-ScheduledTask -TaskName '{_TASK_NAME}' -Confirm:$false")
    if res.returncode != 0:
        print(f"[error] {res.stderr or res.stdout}", file=sys.stderr)
        return 1
    print(f"Task '{_TASK_NAME}' removed.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: install  (one-shot full setup)
# ---------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    print("=== wsl-gpu-guard: full install ===\n")

    # Write default config if none exists
    cfg_path = _cfg.write_default()
    print(f"Config    : {cfg_path}")

    # Systemd service
    svc_rc = cmd_install_service(args)

    # Windows task (best-effort — may be on a machine without powershell.exe)
    task_rc = 0
    if shutil.which("powershell.exe"):
        task_rc = cmd_install_task(args)
    else:
        print("\nWindows Task Scheduler: skipped (powershell.exe not found).")
        print(
            "  To install later from a WSL2 environment:\n"
            "    wsl-gpu-guard install-task"
        )

    if svc_rc != 0 or task_rc != 0:
        print("\n[error] Install completed with errors.", file=sys.stderr)
        return 1

    print(
        "\nAll done. The watchdog now starts automatically on WSL2 boot.\n"
        "\nUseful commands:\n"
        "  wsl-gpu-guard status          — check GPU + service + task state\n"
        "  wsl-gpu-guard uninstall       — remove everything\n"
        f"  journalctl --user -u {_SERVICE_NAME} -f  — follow watchdog logs\n"
        f"  Edit config: {_cfg.CONFIG_FILE}"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: uninstall  (tear down everything)
# ---------------------------------------------------------------------------

def cmd_uninstall(args: argparse.Namespace) -> int:
    print("=== wsl-gpu-guard: uninstall ===\n")
    cmd_uninstall_service(args)
    if shutil.which("powershell.exe"):
        cmd_uninstall_task(args)
    print("\nwsl-gpu-guard has been removed.")
    print(f"Config file kept at {_cfg.CONFIG_FILE} — delete manually if desired.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: config
# ---------------------------------------------------------------------------

def cmd_config(args: argparse.Namespace) -> int:
    if args.init:
        path = _cfg.write_default()
        print(f"Config initialised at {path}")
        return 0

    if not _cfg.CONFIG_FILE.exists():
        print(f"No config file found. Run 'wsl-gpu-guard config --init' to create one.")
        print(f"Expected location: {_cfg.CONFIG_FILE}")
        return 1

    print(f"Config file: {_cfg.CONFIG_FILE}\n")
    print(_cfg.CONFIG_FILE.read_text())
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wsl-gpu-guard",
        description="Graceful GPU hot-removal protection for WSL2 (NVIDIA Optimus laptops).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Quick start:\n"
            "  wsl-gpu-guard install        # one-time setup\n"
            "  wsl-gpu-guard status         # check everything\n"
            "  wsl-gpu-guard uninstall      # remove everything\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    subs = parser.add_subparsers(dest="command", required=True)

    # install
    subs.add_parser(
        "install",
        help="Full one-time setup: systemd service + Windows Task Scheduler task",
    )

    # uninstall
    subs.add_parser(
        "uninstall",
        help="Remove everything installed by 'install'",
    )

    # status
    subs.add_parser(
        "status",
        help="Show GPU state, active PIDs, service and task status",
    )

    # config
    cfg_p = subs.add_parser("config", help="Show or initialise the config file")
    cfg_p.add_argument(
        "--init", action="store_true",
        help=f"Write default config to {_cfg.CONFIG_FILE} if it doesn't exist",
    )

    # watch
    watch = subs.add_parser(
        "watch",
        help="Run the watchdog daemon directly (use 'install' for automatic startup)",
    )
    pid_group = watch.add_mutually_exclusive_group()
    pid_group.add_argument("--pid", type=int, action="append", metavar="PID",
                           help="PID to signal (repeatable)")
    pid_group.add_argument("--self", action="store_true",
                           help="Signal this process (testing)")
    pid_group.add_argument("--parent", action="store_true",
                           help="Signal the parent process")
    watch.add_argument("--gpu-only", action="store_true",
                       help="Auto-detect GPU-using PIDs at fire time (default from config)")
    watch.add_argument("--signal", default=None,
                       choices=["SIGTERM", "SIGINT", "SIGHUP"],
                       help="Signal on GPU removal (default from config: SIGHUP)")
    watch.add_argument("--reconnect-signal", default=None,
                       choices=["SIGTERM", "SIGINT", "SIGHUP"],
                       metavar="SIGNAL",
                       help="Signal when GPU reappears (default from config: SIGHUP)")
    watch.add_argument("--interval", type=float, default=None, metavar="SECONDS",
                       help="Poll interval in seconds (default from config: 2.0)")
    watch.add_argument("--no-rtld-check", action="store_true",
                       help="Skip RTLD_GLOBAL CUDA lib check at startup")

    # install-service / uninstall-service
    subs.add_parser("install-service",
                    help="Install and enable the systemd user service")
    subs.add_parser("uninstall-service",
                    help="Stop, disable, and remove the systemd user service")

    # install-task / uninstall-task
    subs.add_parser("install-task",
                    help="Register the Windows Task Scheduler task")
    subs.add_parser("uninstall-task",
                    help="Remove the Windows Task Scheduler task")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    handlers = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "config": cmd_config,
        "watch": cmd_watch,
        "install-service": cmd_install_service,
        "uninstall-service": cmd_uninstall_service,
        "install-task": cmd_install_task,
        "uninstall-task": cmd_uninstall_task,
    }

    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
