from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from app.config import Settings
from app.scrapers.b3_historical_quotes import B3HistoricalQuoteProvider


def _record(reference: date, ticker: str, *, price_cents: int) -> str:
    values = [" "] * 245
    values[0:2] = "01"
    values[2:10] = reference.strftime("%Y%m%d")
    values[12:24] = f"{ticker:<12}"
    values[24:27] = "010"
    values[108:121] = f"{price_cents:013d}"
    values[210:217] = f"{1:07d}"
    return "".join(values)


def _archive(*records: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("COTAHIST.TXT", "\n".join(records))
    return buffer.getvalue()


def _expected(ticker: str, reference: date, price: str) -> dict[str, dict[date, Decimal]]:
    return {ticker: {reference: Decimal(price)}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_tickers", "second_tickers"),
    [
        ({"AZUL4"}, {"PETR4"}),
        ({"AZUL4", "PETR4"}, {"PETR4"}),
    ],
    ids=["disjoint", "overlapping"],
)
async def test_overlapping_ticker_requests_share_one_archive_download(
    first_tickers: set[str],
    second_tickers: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(
        _record(date(2020, 5, 29), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "PETR4", price_cents=5678),
    )
    provider = B3HistoricalQuoteProvider(Settings())
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(year: int) -> bytes:
        nonlocal calls
        assert year == 2020
        calls += 1
        started.set()
        await release.wait()
        return payload

    monkeypatch.setattr(provider, "_request", request)

    first = asyncio.create_task(provider.prices(2020, first_tickers))
    await started.wait()
    second = asyncio.create_task(provider.prices(2020, second_tickers))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert first_result == {
        ticker: {date(2020, 5, 29): Decimal(price)}
        for ticker, price in (("AZUL4", "12.34"), ("PETR4", "56.78"))
        if ticker in first_tickers
    }
    assert second_result == {
        ticker: {date(2020, 5, 29): Decimal(price)}
        for ticker, price in (("AZUL4", "12.34"), ("PETR4", "56.78"))
        if ticker in second_tickers
    }


@pytest.mark.asyncio
async def test_distinct_years_use_distinct_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        2020: _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234)),
        2021: _archive(_record(date(2021, 5, 28), "PETR4", price_cents=5678)),
    }
    provider = B3HistoricalQuoteProvider(Settings())
    calls: list[int] = []

    async def request(year: int) -> bytes:
        calls.append(year)
        await asyncio.sleep(0)
        return payloads[year]

    monkeypatch.setattr(provider, "_request", request)

    results = await asyncio.gather(
        provider.prices(2020, {"AZUL4"}),
        provider.prices(2021, {"PETR4"}),
    )

    assert sorted(calls) == [2020, 2021]
    assert tuple(results) == (
        _expected("AZUL4", date(2020, 5, 29), "12.34"),
        _expected("PETR4", date(2021, 5, 28), "56.78"),
    )


@pytest.mark.asyncio
async def test_sequential_requests_download_again_to_preserve_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(
        _record(date(2020, 5, 29), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "PETR4", price_cents=5678),
    )
    provider = B3HistoricalQuoteProvider(Settings())
    calls: list[int] = []

    async def request(year: int) -> bytes:
        calls.append(year)
        return payload

    monkeypatch.setattr(provider, "_request", request)

    await provider.prices(2020, {"AZUL4"})
    await provider.prices(2020, {"PETR4"})

    assert calls == [2020, 2020]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(
        _record(date(2020, 5, 29), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "PETR4", price_cents=5678),
    )
    provider = B3HistoricalQuoteProvider(Settings())
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(_year: int) -> bytes:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return payload

    monkeypatch.setattr(provider, "_request", request)

    first = asyncio.create_task(provider.prices(2020, {"AZUL4"}))
    await started.wait()
    second = asyncio.create_task(provider.prices(2020, {"PETR4"}))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    release.set()
    assert await second == _expected("PETR4", date(2020, 5, 29), "56.78")
    assert calls == 1


@pytest.mark.asyncio
async def test_404_and_http_errors_are_not_retained_between_requests() -> None:
    calls = 0

    def not_found(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    provider = B3HistoricalQuoteProvider(Settings(), httpx.MockTransport(not_found))

    assert await provider.prices(2020, {"AZUL4"}) == {}
    assert await provider.prices(2020, {"PETR4"}) == {}
    assert calls == 2

    calls = 0
    payload = _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234))

    def retryable_error(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    provider = B3HistoricalQuoteProvider(Settings(), httpx.MockTransport(retryable_error))

    with pytest.raises(httpx.HTTPStatusError):
        await provider.prices(2020, {"AZUL4"})
    assert await provider.prices(2020, {"AZUL4"}) == _expected(
        "AZUL4", date(2020, 5, 29), "12.34"
    )
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,settings",
    [
        (b"not a zip", Settings()),
        (b"large", Settings(archive_download_max_bytes=1)),
    ],
    ids=["invalid-zip", "download-budget"],
)
async def test_invalid_or_oversized_archives_keep_empty_semantics_and_retry(
    payload: bytes,
    settings: Settings,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    provider = B3HistoricalQuoteProvider(settings, httpx.MockTransport(handler))

    assert await provider.prices(2020, {"AZUL4"}) == {}
    assert await provider.prices(2020, {"AZUL4"}) == {}
    assert calls == 2
