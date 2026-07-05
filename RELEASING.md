# Releasing

This project uses semantic versioning. Tags use the `vMAJOR.MINOR.PATCH` format.

GitHub Releases are created automatically by `.github/workflows/release.yml` when a matching tag is pushed.

## Version Policy

- Patch: bug fixes, documentation corrections and dependency updates that do not change the public API contract.
- Minor: backwards-compatible endpoints, response fields, parser support or configuration.
- Major: breaking endpoint, response, parser, cache or configuration changes.

Until `1.0.0`, minor versions may still include breaking changes when parser behavior or data contracts need to mature. Breaking changes must be called out clearly in `CHANGELOG.md`.

## Version Locations

Update these files for every release:

- `CHANGELOG.md`: release date, notes and compare links.

The release workflow injects the pushed tag version into `pyproject.toml` before building artifacts. Runtime version is resolved from installed package metadata, with `FUNDAMENTUS_API_VERSION` available as an explicit environment override.

`app/main.py` and `/health` both expose the runtime version from `app.__version__`.

## Release Checklist

1. Create or update the `CHANGELOG.md` entry from `[Unreleased]` to the target version and date.
2. Confirm the changelog heading matches the tag without the `v` prefix, for example `## [X.Y.Z] - YYYY-MM-DD` for `vX.Y.Z`.
3. Optional: update the local development version in `pyproject.toml` when you want local package metadata to show the new version before tagging. This is not required for release artifacts.
4. Run the full validation suite:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
```

5. Build the Python distribution and Docker image:

```bash
uv build
docker build -t fundamentus-data-api .
```

6. Commit the release changes:

```bash
git commit -m "chore: release vX.Y.Z"
```

7. Create and push the signed or annotated tag:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
```

8. Check the generated GitHub Release and artifacts.

## Release Pipeline

The release workflow:

- Runs formatting, linting, type checking and tests.
- Reads the version from the pushed tag.
- Injects the tag version into `pyproject.toml` for the release build.
- Extracts the matching release notes from `CHANGELOG.md`.
- Builds the Python wheel and source distribution with `uv build`.
- Builds a Docker image and stores it as a `.docker.tar` artifact.
- Generates `SHA256SUMS`.
- Creates a GitHub Release with all files in `dist/` attached.

## Artifact List

- `fundamentus_data_api-<version>-py3-none-any.whl`
- `fundamentus_data_api-<version>.tar.gz`
- `fundamentus-data-api-v<version>.docker.tar`
- `SHA256SUMS`

The GitHub Release body is generated from the matching `CHANGELOG.md` section.
