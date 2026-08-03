"""Public metadata for :mod:`moseq2_test`."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("moseq2-test")
except PackageNotFoundError:  # pragma: no cover - source tree convenience
    __version__ = "0.1.0.dev0"

SCHEMA_VERSION = 1
WORKER_PROTOCOL_VERSION = 1

__all__ = ["SCHEMA_VERSION", "WORKER_PROTOCOL_VERSION", "__version__"]
