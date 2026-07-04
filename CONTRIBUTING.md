# Contributing

Thanks for considering a contribution.

## Development

```bash
uv sync --python 3.12 --extra dev
uv run ruff format .
uv run ruff check .
uv run mypy app
uv run pytest
```

## Pull Requests

- Keep changes focused and testable.
- Do not add browser automation for Fundamentus scraping unless direct HTTP no longer exposes the required data.
- Add or update fixture-based tests for parser changes.
- Do not commit generated caches, virtual environments or local `.env` files.
- Keep public API changes documented in `README.md` and `docs/API.md`.

## Scraping Policy

This project performs direct HTTP requests against public HTML. Do not add code that bypasses CAPTCHA, rate limits, authentication walls or other protection mechanisms.
