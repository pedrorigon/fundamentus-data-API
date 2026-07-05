import os
from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

_PACKAGE_NAME = "fundamentus-data-api"


def _resolve_version() -> str:
    configured = os.getenv("FUNDAMENTUS_API_VERSION")
    if configured:
        return configured.removeprefix("v")

    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()
