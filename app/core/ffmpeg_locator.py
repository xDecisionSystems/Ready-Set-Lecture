"""Finds the ffmpeg/ffprobe binaries to shell out to.

Checked in order: PATH (covers a normal install, and CI), then the
conda-layout location relative to the running interpreter (covers local dev
in a readysetlecture conda env), then packaging/vendor (covers a PyInstaller
build, which bundles the binaries there for milestone 6).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_EXE = ".exe" if sys.platform == "win32" else ""


def _candidates(name: str) -> list[Path]:
    conda_bin_dir = "Library/bin" if sys.platform == "win32" else "bin"
    frozen_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return [
        Path(sys.prefix) / conda_bin_dir / f"{name}{_EXE}",
        frozen_root / "packaging" / "vendor" / f"{name}{_EXE}",
    ]


def find_binary(name: str) -> str:
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for candidate in _candidates(name):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        f"Could not locate '{name}'. Looked on PATH and in: "
        + ", ".join(str(c) for c in _candidates(name))
    )


def find_ffmpeg() -> str:
    return find_binary("ffmpeg")


def find_ffprobe() -> str:
    return find_binary("ffprobe")
