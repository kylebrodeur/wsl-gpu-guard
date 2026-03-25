"""Tests for wsl_gpu_guard.cli — parser and pure helpers."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from wsl_gpu_guard.cli import (
    _build_parser,
    _build_service_unit,
    _discover_nvidia_wheel_libs,
    _find_executable,
    _write_cuda_env_file,
)
from wsl_gpu_guard import config as _cfg


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

class TestParser:
    def setup_method(self):
        self.parser = _build_parser()

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            self.parser.parse_args(["--version"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "wsl-gpu-guard" in (captured.out + captured.err)

    def test_subcommand_required(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args([])

    def test_install_subcommand(self):
        args = self.parser.parse_args(["install"])
        assert args.command == "install"

    def test_uninstall_subcommand(self):
        args = self.parser.parse_args(["uninstall"])
        assert args.command == "uninstall"

    def test_status_subcommand(self):
        args = self.parser.parse_args(["status"])
        assert args.command == "status"

    def test_config_subcommand_defaults(self):
        args = self.parser.parse_args(["config"])
        assert args.command == "config"
        assert not args.init

    def test_config_init_flag(self):
        args = self.parser.parse_args(["config", "--init"])
        assert args.init

    def test_watch_defaults(self):
        args = self.parser.parse_args(["watch"])
        assert args.command == "watch"
        assert args.pid is None
        assert not args.self
        assert not args.parent
        assert not args.gpu_only
        assert args.signal is None
        assert args.reconnect_signal is None
        assert args.interval is None
        assert not args.no_rtld_check

    def test_watch_pid_flag(self):
        args = self.parser.parse_args(["watch", "--pid", "1234", "--pid", "5678"])
        assert args.pid == [1234, 5678]

    def test_watch_self_flag(self):
        args = self.parser.parse_args(["watch", "--self"])
        assert args.self

    def test_watch_parent_flag(self):
        args = self.parser.parse_args(["watch", "--parent"])
        assert args.parent

    def test_watch_signal_choices(self):
        for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
            args = self.parser.parse_args(["watch", "--signal", sig])
            assert args.signal == sig

    def test_watch_invalid_signal_rejected(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["watch", "--signal", "SIGKILL"])

    def test_watch_interval(self):
        args = self.parser.parse_args(["watch", "--interval", "5.0"])
        assert args.interval == 5.0

    def test_watch_no_rtld_check(self):
        args = self.parser.parse_args(["watch", "--no-rtld-check"])
        assert args.no_rtld_check

    def test_watch_pid_and_self_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["watch", "--pid", "123", "--self"])

    def test_verbose_flag(self):
        args = self.parser.parse_args(["-v", "status"])
        assert args.verbose


# ---------------------------------------------------------------------------
# _build_service_unit
# ---------------------------------------------------------------------------

class TestBuildServiceUnit:
    def _cfg_with(self, **kwargs):
        w = _cfg.WatchConfig(**kwargs)
        return _cfg.GuardConfig(watch=w)

    def test_contains_exec_start(self):
        cfg = self._cfg_with()
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "ExecStart=/usr/bin/wsl-gpu-guard" in unit

    def test_watch_subcommand_appended(self):
        cfg = self._cfg_with()
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert unit.count("watch") >= 1
        assert "ExecStart=" in unit
        # The 'watch' subcommand should appear immediately after the executable
        for line in unit.splitlines():
            if line.startswith("ExecStart="):
                assert " watch" in line
                # flags come after 'watch', not before
                watch_pos = line.index(" watch")
                assert not any(f in line[:watch_pos] for f in ("--signal", "--interval", "--pid"))

    def test_gpu_only_flag_included(self):
        cfg = self._cfg_with(gpu_only=True, pids=[])
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--gpu-only" in unit

    def test_explicit_pids_override_gpu_only(self):
        cfg = self._cfg_with(gpu_only=True, pids=[42, 99])
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--pid 42" in unit
        assert "--pid 99" in unit
        assert "--gpu-only" not in unit

    def test_signal_flag_included(self):
        cfg = self._cfg_with(signal="SIGHUP")
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--signal SIGHUP" in unit

    def test_reconnect_signal_flag_included(self):
        cfg = self._cfg_with(reconnect_signal="SIGHUP")
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--reconnect-signal SIGHUP" in unit

    def test_no_reconnect_signal_flag_when_none(self):
        cfg = self._cfg_with(reconnect_signal=None)
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--reconnect-signal" not in unit

    def test_restart_on_failure_present(self):
        cfg = self._cfg_with()
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "Restart=on-failure" in unit

    def test_interval_flag_included(self):
        cfg = self._cfg_with(poll_interval=3.5)
        with patch("wsl_gpu_guard.cli._find_executable", return_value="/usr/bin/wsl-gpu-guard"):
            unit = _build_service_unit(cfg)
        assert "--interval 3.5" in unit


# ---------------------------------------------------------------------------
# _find_executable
# ---------------------------------------------------------------------------

class TestFindExecutable:
    def test_returns_string(self):
        result = _find_executable()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_uses_which_when_available(self):
        with patch("wsl_gpu_guard.cli.shutil.which", return_value="/usr/local/bin/wsl-gpu-guard"):
            result = _find_executable()
        assert result == "/usr/local/bin/wsl-gpu-guard"

    def test_falls_back_to_sibling_of_python(self, tmp_path):
        fake_bin = tmp_path / "wsl-gpu-guard"
        fake_bin.touch()
        with (
            patch("wsl_gpu_guard.cli.shutil.which", return_value=None),
            patch("wsl_gpu_guard.cli.sys.executable", str(tmp_path / "python3")),
        ):
            result = _find_executable()
        assert result == str(fake_bin)


# ---------------------------------------------------------------------------
# cuda-setup parser
# ---------------------------------------------------------------------------

class TestCudaSetupParser:
    def setup_method(self):
        self.parser = _build_parser()

    def test_cuda_setup_subcommand(self):
        args = self.parser.parse_args(["cuda-setup"])
        assert args.command == "cuda-setup"
        assert args.venv is None

    def test_cuda_setup_venv_flag(self):
        args = self.parser.parse_args(["cuda-setup", "--venv", "/tmp/myenv"])
        assert args.venv == "/tmp/myenv"


# ---------------------------------------------------------------------------
# _discover_nvidia_wheel_libs
# ---------------------------------------------------------------------------

class TestDiscoverNvidiaWheelLibs:
    def _make_nvidia_lib(self, sp: Path, package: str) -> Path:
        lib_dir = sp / "nvidia" / package / "lib"
        lib_dir.mkdir(parents=True)
        return lib_dir

    def test_finds_dirs_in_site_packages(self, tmp_path):
        sp = tmp_path / "site-packages"
        cublas = self._make_nvidia_lib(sp, "cublas")
        cudnn  = self._make_nvidia_lib(sp, "cudnn")
        cfg = _cfg.GuardConfig()
        with (
            patch("wsl_gpu_guard.cli.site.getsitepackages", return_value=[str(sp)]),
            patch("wsl_gpu_guard.cli.site.getusersitepackages", return_value=""),
        ):
            result = _discover_nvidia_wheel_libs(cfg)
        assert cublas in result
        assert cudnn in result

    def test_finds_dirs_in_extra_venv(self, tmp_path):
        venv = tmp_path / "myenv"
        sp = venv / "lib" / "python3.12" / "site-packages"
        cublas = self._make_nvidia_lib(sp, "cublas")
        cfg = _cfg.GuardConfig(cuda=_cfg.CudaConfig(extra_venvs=[str(venv)]))
        with (
            patch("wsl_gpu_guard.cli.site.getsitepackages", return_value=[]),
            patch("wsl_gpu_guard.cli.site.getusersitepackages", return_value=""),
        ):
            result = _discover_nvidia_wheel_libs(cfg)
        assert cublas in result

    def test_deduplicates_symlinked_paths(self, tmp_path):
        sp1 = tmp_path / "sp1"
        sp2 = tmp_path / "sp2"
        real = self._make_nvidia_lib(sp1, "cublas")
        # sp2/nvidia/cublas/lib -> real
        (sp2 / "nvidia" / "cublas").mkdir(parents=True)
        (sp2 / "nvidia" / "cublas" / "lib").symlink_to(real)
        cfg = _cfg.GuardConfig()
        with (
            patch("wsl_gpu_guard.cli.site.getsitepackages", return_value=[str(sp1), str(sp2)]),
            patch("wsl_gpu_guard.cli.site.getusersitepackages", return_value=""),
        ):
            result = _discover_nvidia_wheel_libs(cfg)
        assert len(result) == 1

    def test_empty_when_nothing_found(self, tmp_path):
        cfg = _cfg.GuardConfig()
        with (
            patch("wsl_gpu_guard.cli.site.getsitepackages", return_value=[str(tmp_path)]),
            patch("wsl_gpu_guard.cli.site.getusersitepackages", return_value=""),
        ):
            result = _discover_nvidia_wheel_libs(cfg)
        assert result == []


# ---------------------------------------------------------------------------
# _write_cuda_env_file
# ---------------------------------------------------------------------------

class TestWriteCudaEnvFile:
    def test_writes_ld_library_path(self, tmp_path):
        with (
            patch("wsl_gpu_guard.cli._CUDA_ENV_DIR", tmp_path),
            patch("wsl_gpu_guard.cli._CUDA_ENV_FILE", tmp_path / "cuda-wheels.conf"),
        ):
            _write_cuda_env_file([Path("/a/lib"), Path("/b/lib")])
        text = (tmp_path / "cuda-wheels.conf").read_text()
        assert "LD_LIBRARY_PATH=/a/lib:/b/lib\n" in text

    def test_no_broken_empty_prefix_when_no_dirs(self, tmp_path):
        with (
            patch("wsl_gpu_guard.cli._CUDA_ENV_DIR", tmp_path),
            patch("wsl_gpu_guard.cli._CUDA_ENV_FILE", tmp_path / "cuda-wheels.conf"),
        ):
            _write_cuda_env_file([])
        text = (tmp_path / "cuda-wheels.conf").read_text()
        assert "LD_LIBRARY_PATH=:" not in text

    def test_idempotent(self, tmp_path):
        env_file = tmp_path / "cuda-wheels.conf"
        dirs = [Path("/x/lib")]
        with (
            patch("wsl_gpu_guard.cli._CUDA_ENV_DIR", tmp_path),
            patch("wsl_gpu_guard.cli._CUDA_ENV_FILE", env_file),
        ):
            _write_cuda_env_file(dirs)
            content1 = env_file.read_text()
            _write_cuda_env_file(dirs)
            content2 = env_file.read_text()
        assert content1 == content2
