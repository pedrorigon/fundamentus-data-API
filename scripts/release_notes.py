from __future__ import annotations

import argparse
import re
from pathlib import Path

RELEASE_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - .*)?$")
LINK_REFERENCE = re.compile(r"^\[[^\]]+\]:\s+\S+")


def extract_release_notes(changelog: Path, version: str) -> str:
    lines = changelog.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = RELEASE_HEADING.match(line)
        if not match:
            continue
        if match.group("version") == version:
            start = index + 1
            continue
        if start is not None:
            end = index
            break

    if start is None:
        raise SystemExit(f"Could not find changelog section for version {version!r}.")

    section = [line for line in lines[start:end] if not LINK_REFERENCE.match(line)]
    notes = "\n".join(section).strip()
    if not notes:
        raise SystemExit(f"Changelog section for version {version!r} is empty.")

    return notes + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract release notes from CHANGELOG.md.")
    parser.add_argument("version", help="Release version without the v prefix, for example 0.1.1.")
    parser.add_argument("--changelog", default="CHANGELOG.md", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    notes = extract_release_notes(args.changelog, args.version)
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
