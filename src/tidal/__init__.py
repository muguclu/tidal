"""Tidal — cloud-native time-series platform for front-office trading on Azure."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tidal")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]
