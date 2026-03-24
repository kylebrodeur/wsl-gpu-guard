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
class CudaConfig:
    extra_venvs: list[str] = field(default_factory=list)


@dataclass
class GuardConfig:
    watch: WatchConfig = field(default_factory=WatchConfig)
    cuda: CudaConfig = field(default_factory=CudaConfig)


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
    cuda_raw = raw.get("cuda", {})
    cuda = CudaConfig(
        extra_venvs=list(cuda_raw.get("extra_venvs", [])),
    )
    return GuardConfig(watch=watch, cuda=cuda)


def save_cuda_venvs(venvs: list[str]) -> None:
    """Persist an updated extra_venvs list into the [cuda] section of CONFIG_FILE.

    Creates the config file (via write_default) if it does not yet exist.
    Replaces the [cuda] block in-place so that [watch] comments are preserved.
    """
    write_default()  # idempotent — creates file only if absent
    text = CONFIG_FILE.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    array = "[" + ", ".join(f'"{v}"' for v in venvs) + "]"
    new_block = f"[cuda]\nextra_venvs = {array}\n"

    # Find the [cuda] section, if present
    cuda_start: int | None = None
    cuda_end: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "[cuda]":
            cuda_start = i
        elif cuda_start is not None and line.strip().startswith("[") and i > cuda_start:
            cuda_end = i
            break

    if cuda_start is not None:
        end = cuda_end if cuda_end is not None else len(lines)
        lines[cuda_start:end] = [new_block]
    else:
        # Append, with a blank line separator if the file doesn't end with one
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n" + new_block)

    tmp = CONFIG_FILE.with_suffix(".toml.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


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

[cuda]
# Additional venv roots to scan for nvidia wheel lib dirs.
# Run 'wsl-gpu-guard cuda-setup --venv PATH' to add an entry here.
# extra_venvs = ["/home/user/projects/myapp"]
extra_venvs = []
""",
        encoding="utf-8",
    )
    return CONFIG_FILE
