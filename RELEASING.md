# Releasing

This project uses semantic versioning. Tags use the `vMAJOR.MINOR.PATCH` format, for example `v0.1.0`.

## Version Policy

- Patch: bug fixes, documentation corrections and dependency updates that do not change the public API contract.
- Minor: backwards-compatible endpoints, response fields, parser support or configuration.
- Major: breaking endpoint, response, parser, cache or configuration changes.

Until `1.0.0`, minor versions may still include breaking changes when parser behavior or data contracts need to mature. Breaking changes must be called out clearly in `CHANGELOG.md`.

## Version Locations

Update these files for every release:

- `pyproject.toml`: package version.
- `app/__init__.py`: runtime API version.
- `.env.example`: default `FUNDAMENTUS_API_USER_AGENT` version.
- `CHANGELOG.md`: release date, notes and compare links.

`app/main.py` and `/health` both expose the runtime version from `app.__version__`.

## Release Checklist

1. Create or update the `CHANGELOG.md` entry from `[Unreleased]` to the target version and date.
2. Update version locations listed above.
3. Run the full validation suite:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
```

4. Build the Docker image:

```bash
docker build -t fundamentus-data-api .
```

5. Commit the release changes:

```bash
git commit -m "chore: release v0.1.0"
```

6. Create and push the signed or annotated tag:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin main --tags
```

7. Draft the GitHub release from the `CHANGELOG.md` notes.

## GitHub Release Notes Template

```markdown
## Highlights

- 

## Changes

- 

## Validation

- `uv run ruff format --check .`
- `uv run ruff check .`
- `uv run mypy app`
- `uv run pytest`

## Upgrade Notes

- 
```
