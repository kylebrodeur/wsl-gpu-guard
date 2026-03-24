"""Tests for wsl_gpu_guard.config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import tomllib

from wsl_gpu_guard import config as _cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_config_dir(tmp_path):
    """Context manager: redirect CONFIG_DIR and CONFIG_FILE to tmp_path."""
    cfg_dir = tmp_path / "wsl-gpu-guard"
    cfg_file = cfg_dir / "config.toml"
    return patch.multiple(
        "wsl_gpu_guard.config",
        CONFIG_DIR=cfg_dir,
        CONFIG_FILE=cfg_file,
    )


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

class TestLoad:
    def test_defaults_when_no_file(self, tmp_path):
        with _with_config_dir(tmp_path):
            cfg = _cfg.load()
        assert cfg.watch.signal == "SIGHUP"
        assert cfg.watch.reconnect_signal == "SIGHUP"
        assert cfg.watch.gpu_only is True
        assert cfg.watch.poll_interval == 2.0
        assert cfg.watch.pids == []

    def test_parses_valid_toml(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text(
            '[watch]\nsignal = "SIGTERM"\ngpu_only = false\npoll_interval = 5.0\npids = [42, 99]\n',
            encoding="utf-8",
        )
        with patch("wsl_gpu_guard.config.CONFIG_FILE", cfg_file):
            cfg = _cfg.load()
        assert cfg.watch.signal == "SIGTERM"
        assert cfg.watch.gpu_only is False
        assert cfg.watch.poll_interval == 5.0
        assert cfg.watch.pids == [42, 99]

    def test_returns_defaults_on_malformed_toml(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text("this is not valid toml ][", encoding="utf-8")
        with patch("wsl_gpu_guard.config.CONFIG_FILE", cfg_file):
            cfg = _cfg.load()
        assert cfg.watch.signal == "SIGHUP"  # default

    def test_partial_toml_fills_defaults(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text('[watch]\nsignal = "SIGINT"\n', encoding="utf-8")
        with patch("wsl_gpu_guard.config.CONFIG_FILE", cfg_file):
            cfg = _cfg.load()
        assert cfg.watch.signal == "SIGINT"
        assert cfg.watch.poll_interval == 2.0  # default unchanged

    def test_reconnect_signal_can_be_null(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        # In TOML, no key means default; explicitly omitting reconnect_signal
        cfg_file.write_text('[watch]\n', encoding="utf-8")
        with (
            patch("wsl_gpu_guard.config.CONFIG_FILE", cfg_file),
            patch("wsl_gpu_guard.config.CONFIG_DIR", cfg_dir),
        ):
            cfg = _cfg.load()
        # reconnect_signal defaults to "SIGHUP"
        assert cfg.watch.reconnect_signal == "SIGHUP"


# ---------------------------------------------------------------------------
# write_default()
# ---------------------------------------------------------------------------

class TestWriteDefault:
    def test_creates_file(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            path = _cfg.write_default()
        assert path == cfg_file
        assert cfg_file.exists()

    def test_does_not_overwrite_existing(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text("original", encoding="utf-8")
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.write_default()
        assert cfg_file.read_text() == "original"

    def test_written_file_is_valid_toml(self, tmp_path):
        import tomllib
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.write_default()
        with cfg_file.open("rb") as f:
            data = tomllib.load(f)
        assert "watch" in data

    def test_written_file_loadable_via_load(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.write_default()
            cfg = _cfg.load()
        assert cfg.watch.signal == "SIGHUP"

    def test_written_file_contains_cuda_section(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.write_default()
        with cfg_file.open("rb") as f:
            data = tomllib.load(f)
        assert "cuda" in data
        assert data["cuda"]["extra_venvs"] == []


# ---------------------------------------------------------------------------
# CudaConfig
# ---------------------------------------------------------------------------

class TestCudaConfig:
    def test_cuda_defaults_when_no_file(self, tmp_path):
        with _with_config_dir(tmp_path):
            cfg = _cfg.load()
        assert cfg.cuda.extra_venvs == []

    def test_cuda_extra_venvs_parsed(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text('[cuda]\nextra_venvs = ["/tmp/venv1"]\n', encoding="utf-8")
        with patch("wsl_gpu_guard.config.CONFIG_FILE", cfg_file):
            cfg = _cfg.load()
        assert cfg.cuda.extra_venvs == ["/tmp/venv1"]


# ---------------------------------------------------------------------------
# save_cuda_venvs()
# ---------------------------------------------------------------------------

class TestSaveCudaVenvs:
    def test_creates_cuda_section_on_fresh_file(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.save_cuda_venvs(["/a", "/b"])
        text = cfg_file.read_text()
        assert "[cuda]" in text
        assert '"/a"' in text
        assert '"/b"' in text

    def test_updates_existing_cuda_section(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text('[cuda]\nextra_venvs = ["/old"]\n', encoding="utf-8")
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.save_cuda_venvs(["/new1", "/new2"])
        text = cfg_file.read_text()
        assert '"/new1"' in text
        assert '"/new2"' in text
        assert '"/old"' not in text

    def test_does_not_touch_watch_section(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text(
            '[watch]\nsignal = "SIGTERM"\n\n[cuda]\nextra_venvs = []\n',
            encoding="utf-8",
        )
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.save_cuda_venvs(["/x"])
        text = cfg_file.read_text()
        assert 'signal = "SIGTERM"' in text

    def test_result_is_valid_toml(self, tmp_path):
        cfg_dir = tmp_path / "wsl-gpu-guard"
        cfg_file = cfg_dir / "config.toml"
        with patch.multiple("wsl_gpu_guard.config", CONFIG_DIR=cfg_dir, CONFIG_FILE=cfg_file):
            _cfg.save_cuda_venvs(["/path/one"])
        with cfg_file.open("rb") as f:
            data = tomllib.load(f)
        assert data["cuda"]["extra_venvs"] == ["/path/one"]
