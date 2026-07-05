from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_PATTERN = re.compile(r'(?m)^version = "[^"]+"$')


def sync_pyproject_version(pyproject: Path, version: str) -> None:
    normalized = version.removeprefix("v")
    content = pyproject.read_text(encoding="utf-8")
    updated, replacements = VERSION_PATTERN.subn(f'version = "{normalized}"', content, count=1)
    if replacements != 1:
        raise SystemExit(f"Could not update project version in {pyproject}.")
    pyproject.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject the release version into build metadata.")
    parser.add_argument("version", help="Release version, with or without the v prefix.")
    parser.add_argument("--pyproject", default="pyproject.toml", type=Path)
    args = parser.parse_args()

    sync_pyproject_version(args.pyproject, args.version)


if __name__ == "__main__":
    main()
