"""Tests for wsl_gpu_guard.watchdog."""

from __future__ import annotations

import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wsl_gpu_guard.watchdog import (
    GpuWatchdog,
    _PID_FILE,
    check_rtld_global_cuda_libs,
    get_gpu_using_pids,
    warn_rtld_global_cuda,
)


# ---------------------------------------------------------------------------
# get_gpu_using_pids
# ---------------------------------------------------------------------------

class TestGetGpuUsingPids:
    def test_returns_list(self):
        # Should always return a list (may be empty if /dev/dxg is absent)
        result = get_gpu_using_pids()
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, int)

    def test_includes_self_when_dxg_open(self, tmp_path):
        """If a process has a fake 'dxg' fd open, it appears in results."""
        fake_dxg = tmp_path / "dxg"
        fake_dxg.touch()

        # Open the fake device file so this process has an fd to it
        fd = os.open(str(fake_dxg), os.O_RDONLY)
        try:
            with patch("wsl_gpu_guard.watchdog.DXG_DEVICE", fake_dxg):
                pids = get_gpu_using_pids()
            assert os.getpid() in pids
        finally:
            os.close(fd)

    def test_excludes_self_when_not_open(self, tmp_path):
        fake_dxg = tmp_path / "dxg"
        fake_dxg.touch()
        # Don't open it — this process should NOT be in the list
        with patch("wsl_gpu_guard.watchdog.DXG_DEVICE", fake_dxg):
            pids = get_gpu_using_pids()
        assert os.getpid() not in pids


# ---------------------------------------------------------------------------
# check_rtld_global_cuda_libs / warn_rtld_global_cuda
# ---------------------------------------------------------------------------

class TestRtldCheck:
    def test_returns_list(self):
        result = check_rtld_global_cuda_libs()
        assert isinstance(result, list)

    def test_empty_when_no_cuda(self):
        # On a machine without CUDA libs loaded globally this should be empty
        result = check_rtld_global_cuda_libs()
        # We can only assert it's a list; contents depend on the environment
        assert all(isinstance(x, str) for x in result)

    def test_warn_logs_when_risky(self, caplog):
        with patch(
            "wsl_gpu_guard.watchdog.check_rtld_global_cuda_libs",
            return_value=["libcublas.so.12"],
        ):
            import logging
            with caplog.at_level(logging.WARNING, logger="wsl_gpu_guard.watchdog"):
                warn_rtld_global_cuda()
        assert "RTLD_GLOBAL" in caplog.text
        assert "libcublas.so.12" in caplog.text

    def test_warn_silent_when_safe(self, caplog):
        with patch(
            "wsl_gpu_guard.watchdog.check_rtld_global_cuda_libs",
            return_value=[],
        ):
            import logging
            with caplog.at_level(logging.WARNING, logger="wsl_gpu_guard.watchdog"):
                warn_rtld_global_cuda()
        assert "RTLD_GLOBAL" not in caplog.text


# ---------------------------------------------------------------------------
# GpuWatchdog — basic construction and properties
# ---------------------------------------------------------------------------

class TestGpuWatchdogInit:
    def test_defaults(self, tmp_path):
        dog = GpuWatchdog(dxg_path=tmp_path / "dxg")
        assert dog.pids == []
        assert dog.signal_name == "SIGTERM"
        assert dog.reconnect_signal_name is None
        assert dog.poll_interval == 2.0
        assert not dog.gpu_only
        assert not dog.is_running

    def test_gpu_only_cleared_when_pids_given(self, tmp_path):
        dog = GpuWatchdog(pids=[1234], gpu_only=True, dxg_path=tmp_path / "dxg")
        assert not dog.gpu_only  # pids override gpu_only

    def test_gpu_present_reflects_path(self, tmp_path):
        dxg = tmp_path / "dxg"
        dog = GpuWatchdog(dxg_path=dxg)
        assert not dog.gpu_present
        dxg.touch()
        assert dog.gpu_present


# ---------------------------------------------------------------------------
# GpuWatchdog — start / stop lifecycle
# ---------------------------------------------------------------------------

class TestGpuWatchdogLifecycle:
    def test_start_stop(self, tmp_path):
        dxg = tmp_path / "dxg"
        dxg.touch()
        dog = GpuWatchdog(dxg_path=dxg, poll_interval=0.05, check_rtld_global=False)
        dog.start()
        assert dog.is_running
        dog.stop()
        assert not dog.is_running

    def test_pid_file_written_and_removed(self, tmp_path):
        dxg = tmp_path / "dxg"
        dxg.touch()
        pid_file = tmp_path / "guard.pid"

        with patch("wsl_gpu_guard.watchdog._PID_FILE", pid_file):
            dog = GpuWatchdog(dxg_path=dxg, poll_interval=0.05, check_rtld_global=False)
            dog.start()
            assert pid_file.exists()
            assert pid_file.read_text() == str(os.getpid())
            dog.stop()
            assert not pid_file.exists()

    def test_async_context_manager(self, tmp_path):
        import asyncio

        dxg = tmp_path / "dxg"
        dxg.touch()

        async def _run():
            async with GpuWatchdog.async_context(
                dxg_path=dxg, poll_interval=0.05, check_rtld_global=False
            ) as dog:
                assert dog.is_running
            assert not dog.is_running

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# GpuWatchdog — removal detection
# ---------------------------------------------------------------------------

class TestGpuWatchdogRemoval:
    def test_fires_callback_on_removal(self, tmp_path):
        dxg = tmp_path / "dxg"
        dxg.touch()
        fired = threading.Event()

        dog = GpuWatchdog(
            dxg_path=dxg,
            poll_interval=0.05,
            on_remove_callback=lambda: fired.set(),
            check_rtld_global=False,
        )
        dog.start()
        time.sleep(0.1)
        dxg.unlink()  # simulate GPU removal
        fired.wait(timeout=1.0)
        dog.stop()

        assert fired.is_set()

    def test_signals_target_pid_on_removal(self, tmp_path):
        dxg = tmp_path / "dxg"
        dxg.touch()
        received = threading.Event()

        original_handler = signal.getsignal(signal.SIGHUP)
        signal.signal(signal.SIGHUP, lambda s, f: received.set())
        try:
            dog = GpuWatchdog(
                pids=[os.getpid()],
                signal_name="SIGHUP",
                dxg_path=dxg,
                poll_interval=0.05,
                check_rtld_global=False,
            )
            dog.start()
            time.sleep(0.1)
            dxg.unlink()
            received.wait(timeout=1.0)
            dog.stop()
        finally:
            signal.signal(signal.SIGHUP, original_handler)

        assert received.is_set()

    def test_fires_reconnect_callback_on_reappearance(self, tmp_path):
        dxg = tmp_path / "dxg"
        reconnected = threading.Event()

        dog = GpuWatchdog(
            dxg_path=dxg,
            poll_interval=0.05,
            on_reconnect_callback=lambda: reconnected.set(),
            check_rtld_global=False,
        )
        dog.start()
        time.sleep(0.1)
        dxg.touch()  # GPU appears from absent state
        reconnected.wait(timeout=1.0)
        dog.stop()

        assert reconnected.is_set()

    def test_sigusr1_triggers_preemptive_signal(self, tmp_path):
        dxg = tmp_path / "dxg"
        dxg.touch()
        received = threading.Event()

        original_handler = signal.getsignal(signal.SIGHUP)
        signal.signal(signal.SIGHUP, lambda s, f: received.set())
        try:
            dog = GpuWatchdog(
                pids=[os.getpid()],
                signal_name="SIGHUP",
                dxg_path=dxg,
                poll_interval=0.5,
                check_rtld_global=False,
            )
            dog.start()
            # Send SIGUSR1 (as Windows script would) before dxg disappears
            os.kill(os.getpid(), signal.SIGUSR1)
            received.wait(timeout=1.0)
            dog.stop()
        finally:
            signal.signal(signal.SIGHUP, original_handler)

        assert received.is_set()
