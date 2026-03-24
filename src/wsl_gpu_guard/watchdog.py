"""GPU device watchdog for WSL2.

Monitors /dev/dxg (the WSL2 kernel driver interface to the Windows GPU stack).
When the file disappears (AC unplug on Optimus laptops) or becomes unreadable,
sends a configurable signal to target processes before the kernel can panic.

Usage::

    from wsl_gpu_guard.watchdog import GpuWatchdog

    dog = GpuWatchdog(pids=[1234], on_remove="SIGTERM", poll_interval=2.0)
    dog.start()          # starts background thread
    ...
    dog.stop()

Or as an async context manager::

    async with GpuWatchdog.async_context(pids=[1234]) as dog:
        ...
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import signal
import site
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

_PID_FILE = Path("/tmp/.wsl-gpu-guard.pid")

logger = logging.getLogger(__name__)

DXG_DEVICE = Path("/dev/dxg")
_SIGNAL_MAP: dict[str, int] = {
    "SIGTERM": signal.SIGTERM,
    "SIGINT": signal.SIGINT,
    "SIGHUP": signal.SIGHUP,
}


# ---------------------------------------------------------------------------
# GPU process detection
# ---------------------------------------------------------------------------

def get_gpu_using_pids() -> list[int]:
    """Return PIDs of all processes that currently have /dev/dxg open.

    Scans /proc/*/fd/ symlinks.  Useful for auto-detecting which processes
    are actually using the GPU so you don't accidentally signal innocent
    processes (e.g. VSCode server, terminals) that don't hold a CUDA context.
    """
    pids: list[int] = []
    dxg = DXG_DEVICE.resolve() if DXG_DEVICE.exists() else DXG_DEVICE
    proc = Path("/proc")
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if fd.resolve() == dxg:
                        pids.append(int(pid_dir.name))
                        break
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return pids


# ---------------------------------------------------------------------------
# RTLD_GLOBAL safety check
# ---------------------------------------------------------------------------

def check_rtld_global_cuda_libs() -> list[str]:
    """Return names of CUDA libs that appear to be loaded with RTLD_GLOBAL.

    RTLD_GLOBAL makes a library's symbols globally visible in the process, which
    can corrupt the WSL2 CUDA driver's internal symbol table and cause GPU crashes.
    This function checks whether known CUDA library sonames are already resolvable
    as global symbols — a sign that something loaded them with RTLD_GLOBAL.

    Returns a list of library names that look dangerously loaded (empty = safe).
    """
    risky: list[str] = []
    cuda_sonames = [
        "libcublas.so.12",
        "libcudnn.so.9",
        "libcurand.so.10",
        "libcufft.so.11",
    ]
    for soname in cuda_sonames:
        try:
            # RTLD_NOLOAD (0x4) returns the handle only if already loaded;
            # combined with RTLD_GLOBAL (0x100) it tells us the lib is present
            # in the global symbol namespace.
            RTLD_NOLOAD = getattr(ctypes, "RTLD_NOLOAD", 0x4)
            RTLD_GLOBAL = getattr(ctypes, "RTLD_GLOBAL", 0x100)
            handle = ctypes.CDLL(soname, mode=RTLD_NOLOAD | RTLD_GLOBAL)
            if handle._handle:
                risky.append(soname)
        except OSError:
            pass
    return risky


def warn_rtld_global_cuda() -> None:
    """Log a warning if CUDA libs are loaded with RTLD_GLOBAL.

    Call this before starting the watchdog to surface dangerous library loading
    that is known to cause WSL2 GPU crashes.
    """
    risky = check_rtld_global_cuda_libs()
    if risky:
        logger.warning(
            "RTLD_GLOBAL CUDA libs detected — these may destabilise the WSL2 "
            "GPU driver and cause /dev/dxg to disappear unexpectedly: %s. "
            "Fix: use LD_LIBRARY_PATH instead of ctypes.CDLL(..., mode=RTLD_GLOBAL).",
            ", ".join(risky),
        )


class GpuWatchdog:
    """Poll /dev/dxg and fire a callback or signal when the GPU is removed.

    Args:
        pids: Process IDs to signal on GPU removal.  Pass an empty list to
              only fire the ``on_remove_callback`` without signalling anything.
        signal_name: Signal to send on removal — "SIGTERM" (default), "SIGINT",
              or "SIGHUP".
        reconnect_signal_name: Signal to send when the GPU comes back after a
              removal (e.g. AC plugged back in).  Defaults to None (no signal).
              Useful for telling a server to re-initialise CUDA.
        on_remove_callback: Optional callable invoked with no arguments when the
                            GPU is removed, *before* signals are sent.
        on_reconnect_callback: Optional callable invoked when the GPU reappears.
        poll_interval: Seconds between /dev/dxg existence checks (default 2.0).
        dxg_path: Override device path (useful for testing).
        check_rtld_global: If True (default), log a warning at startup if any
              CUDA libs appear to be loaded with RTLD_GLOBAL, which is a known
              cause of WSL2 GPU crashes.
        gpu_only: If True, dynamically discover pids from /proc/*/fd at fire
              time, targeting only processes that have /dev/dxg open.  Ignored
              if ``pids`` is also provided.
    """

    def __init__(
        self,
        pids: list[int] | None = None,
        signal_name: Literal["SIGTERM", "SIGINT", "SIGHUP"] = "SIGTERM",
        reconnect_signal_name: Literal["SIGTERM", "SIGINT", "SIGHUP"] | None = None,
        on_remove_callback: Callable[[], None] | None = None,
        on_reconnect_callback: Callable[[], None] | None = None,
        poll_interval: float = 2.0,
        dxg_path: Path = DXG_DEVICE,
        check_rtld_global: bool = True,
        gpu_only: bool = False,
    ) -> None:
        self.pids = list(pids or [])
        self.signal_name = signal_name
        self.reconnect_signal_name = reconnect_signal_name
        self.on_remove_callback = on_remove_callback
        self.on_reconnect_callback = on_reconnect_callback
        self.poll_interval = poll_interval
        self.dxg_path = dxg_path
        self.check_rtld_global = check_rtld_global
        self.gpu_only = gpu_only and not self.pids

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread."""
        if self.check_rtld_global:
            warn_rtld_global_cuda()
        if not self.dxg_path.exists():
            logger.warning(
                "%s does not exist — GPU watchdog has nothing to monitor "
                "(is this a WSL2 environment with a CUDA-capable GPU?)",
                self.dxg_path,
            )
        self._stop_event.clear()
        self._fired = False
        self._write_pid_file()
        # Install SIGUSR1 handler so the Windows on-ac-disconnect.ps1 script
        # can trigger a pre-emptive GPU removal signal via `kill -s USR1 <pid>`.
        signal.signal(signal.SIGUSR1, self._handle_sigusr1)
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="wsl-gpu-guard",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "GPU watchdog started (device=%s, interval=%.1fs, "
            "signal=%s, reconnect_signal=%s, pids=%s, pid_file=%s)",
            self.dxg_path,
            self.poll_interval,
            self.signal_name,
            self.reconnect_signal_name or "none",
            self.pids,
            _PID_FILE,
        )

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 2)
        self._remove_pid_file()
        logger.debug("GPU watchdog stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def gpu_present(self) -> bool:
        """Return True if /dev/dxg currently exists."""
        return self.dxg_path.exists()

    # ------------------------------------------------------------------
    # Async context manager helper
    # ------------------------------------------------------------------

    @classmethod
    def async_context(cls, **kwargs):
        """Return an async context manager that start/stops the watchdog."""
        return _AsyncWatchdogContext(cls(**kwargs))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_pid_file(self) -> None:
        try:
            _PID_FILE.write_text(str(os.getpid()))
            atexit.register(self._remove_pid_file)
        except OSError as exc:
            logger.warning("Could not write PID file %s: %s", _PID_FILE, exc)

    def _remove_pid_file(self) -> None:
        try:
            _PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _handle_sigusr1(self, signum: int, frame: object) -> None:
        """Pre-emptive GPU removal from the Windows on-ac-disconnect.ps1 script."""
        if not self._fired:
            self._fired = True
            logger.warning(
                "SIGUSR1 received (Windows AC-disconnect event) — "
                "pre-emptively signalling PIDs %s before GPU powers down.",
                self.pids,
            )
            self._fire(removed=True)

    def _poll_loop(self) -> None:
        was_present = self.dxg_path.exists()

        while not self._stop_event.is_set():
            now_present = self.dxg_path.exists()

            if was_present and not now_present and not self._fired:
                self._fired = True
                logger.warning(
                    "%s disappeared — GPU was hot-removed (AC unplug?). "
                    "Sending %s to PIDs %s.",
                    self.dxg_path,
                    self.signal_name,
                    self.pids,
                )
                self._fire(removed=True)

            elif not was_present and now_present:
                # GPU came back (resumed / plugged in)
                self._fired = False
                logger.info("%s reappeared — GPU is back.", self.dxg_path)
                self._fire_reconnect()

            was_present = now_present
            self._stop_event.wait(self.poll_interval)

    def _fire(self, *, removed: bool) -> None:
        if self.on_remove_callback is not None:
            try:
                self.on_remove_callback()
            except Exception:
                logger.exception("on_remove_callback raised an exception")

        targets = self.pids if self.pids else (get_gpu_using_pids() if self.gpu_only else [])
        if not targets:
            logger.debug("No target PIDs to signal on GPU removal.")
            return

        sig = _SIGNAL_MAP.get(self.signal_name, signal.SIGTERM)
        for pid in targets:
            try:
                os.kill(pid, sig)
                logger.info("Sent %s to PID %d", self.signal_name, pid)
            except ProcessLookupError:
                logger.debug("PID %d no longer exists, skipping signal", pid)
            except PermissionError:
                logger.error(
                    "Permission denied sending %s to PID %d", self.signal_name, pid
                )

    def _fire_reconnect(self) -> None:
        if self.on_reconnect_callback is not None:
            try:
                self.on_reconnect_callback()
            except Exception:
                logger.exception("on_reconnect_callback raised an exception")

        if self.reconnect_signal_name is None:
            return

        sig = _SIGNAL_MAP.get(self.reconnect_signal_name, signal.SIGHUP)
        for pid in self.pids:
            try:
                os.kill(pid, sig)
                logger.info(
                    "Sent %s (reconnect) to PID %d", self.reconnect_signal_name, pid
                )
            except ProcessLookupError:
                logger.debug("PID %d no longer exists, skipping reconnect signal", pid)
            except PermissionError:
                logger.error(
                    "Permission denied sending %s to PID %d",
                    self.reconnect_signal_name,
                    pid,
                )


class _AsyncWatchdogContext:
    def __init__(self, dog: GpuWatchdog) -> None:
        self._dog = dog

    async def __aenter__(self) -> GpuWatchdog:
        self._dog.start()
        return self._dog

    async def __aexit__(self, *_) -> None:
        self._dog.stop()
