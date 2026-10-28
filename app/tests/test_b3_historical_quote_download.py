from __future__ import annotations

import asyncio
import threading
from datetime import date
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from app.config import Settings
from app.scrapers import b3_historical_quotes as b3_quotes
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
    parse_calls = 0
    parse_tickers: list[set[str]] = []
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        parse_tickers.append(tickers)
        return original_parser(payload, tickers)

    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)
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
    assert parse_calls == 1
    assert parse_tickers == [first_tickers | second_tickers]
    expected_first = {
        ticker: {date(2020, 5, 29): Decimal(price)}
        for ticker, price in (("AZUL4", "12.34"), ("PETR4", "56.78"))
        if ticker in first_tickers
    }
    expected_second = {
        ticker: {date(2020, 5, 29): Decimal(price)}
        for ticker, price in (("AZUL4", "12.34"), ("PETR4", "56.78"))
        if ticker in second_tickers
    }
    assert first_result == expected_first
    assert second_result == expected_second

    first_snapshot = {ticker: dict(series) for ticker, series in first_result.items()}
    second_snapshot = {ticker: dict(series) for ticker, series in second_result.items()}
    first_ticker = next(iter(first_result))
    second_ticker = next(iter(second_result))
    first_result[first_ticker][date(2020, 5, 29)] = Decimal("99.99")
    assert second_result == second_snapshot
    first_after_first_mutation = {ticker: dict(series) for ticker, series in first_result.items()}
    second_result[second_ticker][date(2020, 5, 29)] = Decimal("88.88")
    assert first_result[first_ticker][date(2020, 5, 29)] == Decimal("99.99")
    assert first_result == first_after_first_mutation
    assert first_result != first_snapshot


@pytest.mark.asyncio
async def test_distinct_years_use_distinct_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        2020: _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234)),
        2021: _archive(_record(date(2021, 5, 28), "PETR4", price_cents=5678)),
    }
    provider = B3HistoricalQuoteProvider(Settings())
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        return original_parser(payload, tickers)

    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)
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
    assert parse_calls == 2
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
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        return original_parser(payload, tickers)

    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)
    calls: list[int] = []

    async def request(year: int) -> bytes:
        calls.append(year)
        return payload

    monkeypatch.setattr(provider, "_request", request)

    await provider.prices(2020, {"AZUL4"})
    await provider.prices(2020, {"PETR4"})

    assert calls == [2020, 2020]
    assert parse_calls == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(
        _record(date(2020, 5, 29), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "PETR4", price_cents=5678),
    )
    provider = B3HistoricalQuoteProvider(Settings())
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        return original_parser(payload, tickers)

    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)
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
    assert parse_calls == 1


@pytest.mark.asyncio
async def test_all_cancelled_waiters_allow_a_fresh_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234))
    provider = B3HistoricalQuoteProvider(Settings())
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    calls = 0
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        return original_parser(payload, tickers)

    async def request(_year: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            download_started.set()
            await release_download.wait()
        return payload

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)

    first = asyncio.create_task(provider.prices(2020, {"AZUL4"}))
    await download_started.wait()
    flight = provider._parse_flights[2020]
    assert flight.task is not None
    producer = flight.task
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    release_download.set()
    await producer
    assert not provider._parse_flights

    assert await provider.prices(2020, {"AZUL4"}) == _expected("AZUL4", date(2020, 5, 29), "12.34")
    assert calls == 2
    assert parse_calls == 2


@pytest.mark.asyncio
async def test_late_caller_parses_frozen_payload_without_missing_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(
        _record(date(2020, 5, 29), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "PETR4", price_cents=5678),
    )
    provider = B3HistoricalQuoteProvider(Settings())
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    parse_started = threading.Event()
    release_parse = threading.Event()
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    def parse(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        if parse_calls == 1:
            parse_started.set()
            assert release_parse.wait(timeout=5)
        return original_parser(payload, tickers)

    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)

    async def request(_year: int) -> bytes:
        download_started.set()
        await release_download.wait()
        return payload

    monkeypatch.setattr(provider, "_request", request)

    first = asyncio.create_task(provider.prices(2020, {"AZUL4"}))
    await download_started.wait()
    release_download.set()
    assert await asyncio.to_thread(parse_started.wait, 5)

    second_result = await provider.prices(2020, {"PETR4"})
    assert second_result == _expected("PETR4", date(2020, 5, 29), "56.78")
    assert parse_calls == 2

    release_parse.set()
    assert await first == _expected("AZUL4", date(2020, 5, 29), "12.34")


@pytest.mark.asyncio
async def test_parser_failure_does_not_poison_a_later_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234))
    provider = B3HistoricalQuoteProvider(Settings())
    calls = 0
    parse_calls = 0
    original_parser = b3_quotes.parse_b3_historical_quotes

    async def request(_year: int) -> bytes:
        nonlocal calls
        calls += 1
        return payload

    def parse(content: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        nonlocal parse_calls
        parse_calls += 1
        if parse_calls == 1:
            raise RuntimeError("synthetic parser failure")
        return original_parser(content, tickers)

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(b3_quotes, "parse_b3_historical_quotes", parse)

    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        await provider.prices(2020, {"AZUL4"})

    assert await provider.prices(2020, {"AZUL4"}) == _expected("AZUL4", date(2020, 5, 29), "12.34")
    assert calls == 2
    assert parse_calls == 2


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
    assert await provider.prices(2020, {"AZUL4"}) == _expected("AZUL4", date(2020, 5, 29), "12.34")
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
