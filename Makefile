.PHONY: install format lint type test check run benchmark docker-build docker-up

install:
	uv sync --python 3.12 --extra dev

format:
	uv run ruff format .

lint:
	uv run ruff check .

type:
	uv run mypy app

test:
	uv run pytest

check: format lint type test

run:
	uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

benchmark:
	uv run python scripts/benchmark.py --ticker ITUB4 --hot-runs 10

docker-build:
	docker build -t fundamentus-data-api .

docker-up:
	docker compose up --build
