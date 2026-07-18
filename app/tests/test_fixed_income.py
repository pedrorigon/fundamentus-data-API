from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.api.dependencies import get_fixed_income_valuation_service
from app.cache import CacheStore
from app.config import Settings
from app.main import create_app, lifespan
from app.models import FixedIncomeValuationRequest
from app.scrapers.anbima_fixed_income import (
    AnbimaDebentureProvider,
    parse_anbima_debenture_prices,
)
from app.services.fixed_income import FixedIncomeValuationService, _cached_prices


def _payload(*rows: str) -> bytes:
    header = (
        "ANBIMA\r\n\r\n"
        "Código@Nome@Repac./  Venc.@Índice/ Correção@Taxa de Compra@Taxa de Venda@"
        "Taxa Indicativa@Desvio Padrão@Intervalo Indicativo Minimo@"
        "Intervalo Indicativo Máximo@PU@% PU Par / % VNE@Duration@% Reune@Referência NTN-B\r\n"
    )
    return (header + "\r\n".join(rows)).encode("latin-1")


def _row(identifier: str, price: str) -> str:
    return f"{identifier}@Emissor@17/07/2030@DI + 1%@--@--@1@0@1@1@{price}@100@100@@"


def _cache() -> CacheStore:
    return CacheStore(sqlite_enabled=False, sqlite_path=Path("unused.sqlite3"))


def test_parser_maps_positive_prices_and_ignores_invalid_rows() -> None:
    prices = parse_anbima_debenture_prices(
        _payload(
            _row("AALM12", "1.064,248862"),
            _row("EMPTY1", "--"),
            _row("BAD1", "invalid"),
            _row("ZERO1", "0"),
        )
    )

    assert prices == {"AALM12": Decimal("1064.248862")}
    assert parse_anbima_debenture_prices(b"unexpected") == {}


@pytest.mark.asyncio
async def test_provider_downloads_the_official_daily_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/db260717.txt")
        assert request.headers["user-agent"].startswith("fundamentus-data-api/")
        return httpx.Response(200, content=_payload(_row("AALM12", "1064,25")))

    provider = AnbimaDebentureProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.prices(date(2026, 7, 17)) == {"AALM12": Decimal("1064.25")}


@pytest.mark.asyncio
async def test_provider_treats_missing_daily_file_as_empty() -> None:
    provider = AnbimaDebentureProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(404)),
    )

    assert await provider.prices(date(2026, 7, 18)) == {}


class _Provider:
    def __init__(self, prices_by_date: dict[date, dict[str, Decimal]]) -> None:
        self.prices_by_date = prices_by_date
        self.calls: list[date] = []

    async def prices(self, reference: date) -> dict[str, Decimal]:
        self.calls.append(reference)
        return self.prices_by_date.get(reference, {})


@pytest.mark.asyncio
async def test_service_uses_previous_business_day_and_caches_files() -> None:
    friday = date(2026, 7, 17)
    sunday = date(2026, 7, 19)
    provider = _Provider({friday: {"AALM12": Decimal("1064.25")}})
    service = FixedIncomeValuationService(
        Settings(),
        _cache(),
        provider=provider,  # type: ignore[arg-type]
    )
    request = FixedIncomeValuationRequest(
        identifiers=["aalm12", "CDB925623O7", "AALM12"],
        dates=[sunday, sunday],
    )

    first = await service.resolve(request)
    second = await service.resolve(request)

    valuation = first.valuations["AALM12"][0]
    assert valuation.requested_date == sunday
    assert valuation.reference_date == friday
    assert valuation.unit_price == Decimal("1064.25")
    assert valuation.source == "anbima"
    assert first.valuations["CDB925623O7"] == []
    assert first.unavailable == ["CDB925623O7"]
    assert second == first
    assert provider.calls == [sunday, date(2026, 7, 18), friday]


class _FailingProvider:
    async def prices(self, _reference: date) -> dict[str, Decimal]:
        raise httpx.ConnectError("offline")


@pytest.mark.asyncio
async def test_service_returns_unavailable_when_source_fails() -> None:
    service = FixedIncomeValuationService(
        Settings(),
        _cache(),
        provider=_FailingProvider(),  # type: ignore[arg-type]
    )

    result = await service.resolve(
        FixedIncomeValuationRequest(identifiers=["CDB925623O7"], dates=[date(2026, 7, 17)])
    )

    assert result.unavailable == ["CDB925623O7"]
    assert result.valuations == {"CDB925623O7": []}


def test_request_rejects_unsafe_identifiers() -> None:
    with pytest.raises(ValidationError):
        FixedIncomeValuationRequest(
            identifiers=["../../secret"],
            dates=[date(2026, 7, 17)],
        )


def test_cached_prices_rejects_unexpected_payloads() -> None:
    assert _cached_prices([]) == {}
    assert _cached_prices({"AALM12": None, 1: "2", "OK": 3}) == {"OK": Decimal("3")}


@pytest.mark.asyncio
async def test_fixed_income_endpoint_returns_resolved_values() -> None:
    provider = _Provider({date(2026, 7, 17): {"AALM12": Decimal("1064.25")}})
    service = FixedIncomeValuationService(
        Settings(),
        _cache(),
        provider=provider,  # type: ignore[arg-type]
    )
    app = create_app()
    app.dependency_overrides[get_fixed_income_valuation_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/fixed-income/valuations/resolve",
            json={"identifiers": ["AALM12"], "dates": ["2026-07-17"]},
        )

    assert response.status_code == 200
    assert response.json()["valuations"]["AALM12"][0] == {
        "identifier": "AALM12",
        "requested_date": "2026-07-17",
        "reference_date": "2026-07-17",
        "unit_price": "1064.25",
        "currency": "BRL",
        "source": "anbima",
        "method": "indicative",
    }


def test_fixed_income_dependency_reads_application_state() -> None:
    service = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(fixed_income_valuation_service=service))
    )

    assert get_fixed_income_valuation_service(request) is service  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lifespan_registers_fixed_income_service() -> None:
    app = create_app()

    async with lifespan(app):
        assert isinstance(
            app.state.fixed_income_valuation_service,
            FixedIncomeValuationService,
        )
