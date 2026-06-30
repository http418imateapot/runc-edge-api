from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _local_version() -> str:
    return (Path(__file__).resolve().parent.parent / "VERSION").read_text(
        encoding="utf-8"
    ).strip()


try:
    __version__ = version("runc-edge-api")
except PackageNotFoundError:
    __version__ = _local_version()


__all__ = ["__version__"]
