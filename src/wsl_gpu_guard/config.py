"""User configuration for wsl-gpu-guard.

Config file location: ~/.config/wsl-gpu-guard/config.toml

Example config::

    [watch]
    signal = "SIGHUP"
    reconnect_signal = "SIGHUP"
    gpu_only = true
    poll_interval = 2.0
    # pids = [1234, 5678]   # explicit PIDs; overrides gpu_only

    [service]
    # Extra flags appended to ExecStart in the systemd unit (advanced).
    extra_args = ""
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

CONFIG_DIR = Path.home() / ".config" / "wsl-gpu-guard"
CONFIG_FILE = CONFIG_DIR / "config.toml"

_DEFAULT_SIGNAL: Literal["SIGTERM", "SIGINT", "SIGHUP"] = "SIGHUP"
_DEFAULT_RECONNECT: Literal["SIGTERM", "SIGINT", "SIGHUP"] = "SIGHUP"


@dataclass
class WatchConfig:
    signal: str = _DEFAULT_SIGNAL
    reconnect_signal: str | None = _DEFAULT_RECONNECT
    gpu_only: bool = True
    poll_interval: float = 2.0
    pids: list[int] = field(default_factory=list)


@dataclass
class GuardConfig:
    watch: WatchConfig = field(default_factory=WatchConfig)


def load() -> GuardConfig:
    """Load config from ~/.config/wsl-gpu-guard/config.toml, returning defaults if absent."""
    if not CONFIG_FILE.exists():
        return GuardConfig()

    try:
        with CONFIG_FILE.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return GuardConfig()

    watch_raw = raw.get("watch", {})
    watch = WatchConfig(
        signal=watch_raw.get("signal", _DEFAULT_SIGNAL),
        reconnect_signal=watch_raw.get("reconnect_signal", _DEFAULT_RECONNECT),
        gpu_only=watch_raw.get("gpu_only", True),
        poll_interval=float(watch_raw.get("poll_interval", 2.0)),
        pids=list(watch_raw.get("pids", [])),
    )
    return GuardConfig(watch=watch)


def write_default() -> Path:
    """Write a default config file and return its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        return CONFIG_FILE

    CONFIG_FILE.write_text(
        """\
[watch]
# Signal sent to watched processes on GPU removal.
# SIGHUP = graceful CPU fallback (server keeps running).
# SIGTERM = full shutdown.
signal = "SIGHUP"

# Signal sent when GPU reappears (AC plugged back in).
# Set to null to disable reconnect signalling.
reconnect_signal = "SIGHUP"

# Auto-detect GPU-using processes via /proc/*/fd at fire time.
# Ignores processes that don't hold /dev/dxg open (e.g. VSCode).
# Overridden if pids is set.
gpu_only = true

# How often to poll /dev/dxg (seconds).
poll_interval = 2.0

# Explicit PIDs to signal (overrides gpu_only).
# pids = [1234, 5678]
""",
        encoding="utf-8",
    )
    return CONFIG_FILE
