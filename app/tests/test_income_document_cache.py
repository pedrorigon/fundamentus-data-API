from __future__ import annotations

import asyncio
import sys
import time
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.income.sources import OfficialCompanyIncomeSource, _await_until
from app.models import IncomeEventObservation, IncomeInstrumentRequest


@pytest.mark.asyncio
async def test_cvm_document_cache_evicts_oldest_by_retained_text_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    links = [
        "https://cvm.test/one.pdf",
        "https://cvm.test/two.pdf",
        "https://cvm.test/three.pdf",
    ]
    texts = {link: token * 128 for link, token in zip(links, ("a", "b", "c"), strict=True)}
    calls: dict[str, int] = {link: 0 for link in links}

    def handler(request: httpx.Request) -> httpx.Response:
        link = str(request.url)
        calls[link] += 1
        return httpx.Response(200, content=texts[link].encode())

    monkeypatch.setattr("app.income.sources._pdf_text", lambda content: content.decode())
    text_size = sys.getsizeof(texts[links[0]])
    source = OfficialCompanyIncomeSource(
        Settings(
            cvm_income_document_cache_max_bytes=text_size * 2,
            ticker_cache_max_entries=10,
        ),
        httpx.MockTransport(handler),
    )

    async with source._client() as client:
        for link in links:
            assert await source._cvm_document_text(client, link) == texts[link]

        assert list(source._cvm_documents) == links[1:]
        assert source._cvm_document_cache_bytes == text_size * 2

        # The oldest document was evicted and therefore has to be fetched
        # again; the newest two remain available from the bounded cache.
        assert await source._cvm_document_text(client, links[0]) == texts[links[0]]
        assert await source._cvm_document_text(client, links[2]) == texts[links[2]]

    assert calls[links[0]] == 2
    assert calls[links[1]] == 1
    assert calls[links[2]] == 1
    assert source._cvm_document_cache_bytes == text_size * 2


@pytest.mark.asyncio
async def test_cvm_document_cache_keeps_last_good_text_when_new_success_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = "https://cvm.test/report.pdf"
    responses = [b"small", b"large text that cannot fit", b"failure"]

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = responses.pop(0)
        if payload == b"failure":
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    monkeypatch.setattr("app.income.sources._pdf_text", lambda content: content.decode())
    small_size = sys.getsizeof("small")
    source = OfficialCompanyIncomeSource(
        Settings(
            income_source_index_ttl_seconds=0,
            cvm_income_document_cache_max_bytes=small_size,
        ),
        httpx.MockTransport(handler),
    )

    async with source._client() as client:
        assert await source._cvm_document_text(client, link) == "small"
        assert source._cvm_documents[link][1] == "small"
        assert source._cvm_document_cache_bytes == small_size

        # The current successful value is returned even though it is too large
        # to retain; the previous value remains the last-good fallback.
        assert await source._cvm_document_text(client, link) == "large text that cannot fit"
        assert source._cvm_documents[link][1] == "small"
        assert source._cvm_document_cache_bytes == small_size

        with pytest.raises(httpx.HTTPStatusError):
            await source._cvm_document_text(client, link)

        await asyncio.sleep(0)
        assert source._cvm_documents[link][1] == "small"
        assert source._cvm_document_cache_bytes == small_size

    oversized = "x" * 128
    source._cache_cvm_document("oversized", oversized)
    assert "oversized" not in source._cvm_documents


@pytest.mark.asyncio
async def test_official_collection_returns_validated_progress_before_source_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_link = "https://cvm.test/fast.pdf"
    hung_link = "https://cvm.test/hung.pdf"
    b3_row = {
        "isinCode": "BRBBASACNOR3",
        "label": "DIVIDENDO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,50",
    }
    rows = [
        {
            "Codigo_CVM": "1023",
            "Categoria": "Relatório Proventos",
            "Link_Download": fast_link,
            "Versao": "1",
            "Protocolo_Entrega": "fast",
            "Data_Entrega": "2026-08-20",
        },
        {
            "Codigo_CVM": "1023",
            "Categoria": "Relatório Proventos",
            "Link_Download": hung_link,
            "Versao": "1",
            "Protocolo_Entrega": "hung",
            "Data_Entrega": "2026-08-19",
        },
    ]
    fast_observation = IncomeEventObservation(
        source="cvm",
        lineage="cvm:fast",
        source_event_id="cvm:fast",
        ticker="BBAS3",
        isin="BRBBASACNOR3",
        event_type="Dividendo",
        ex_date=date(2026, 8, 1),
        payment_date=date(2026, 8, 15),
        unit_price=Decimal("0.10"),
        source_version=1,
        authority=100,
        payload_hash="fast",
    )
    release_hung = asyncio.Event()

    source = OfficialCompanyIncomeSource(Settings(income_source_total_timeout_seconds=0.1))

    async def b3_payload(
        _client: httpx.AsyncClient,
        _ticker: str,
        *,
        deadline: float | None,
    ) -> object:
        del deadline
        return [{"codeCVM": "1023", "cashDividends": [b3_row]}]

    async def cvm_rows(
        _client: httpx.AsyncClient,
        year: int,
        *,
        deadline: float | None,
    ) -> list[dict[str, str]]:
        del deadline
        return rows if year == 2026 else []

    async def cvm_document(
        _client: httpx.AsyncClient,
        _instrument: IncomeInstrumentRequest,
        row: dict[str, str],
        *,
        deadline: float | None,
    ) -> list[IncomeEventObservation]:
        del deadline
        if row["Link_Download"] == hung_link:
            await release_hung.wait()
        return [fast_observation]

    monkeypatch.setattr(source, "_b3_payload", b3_payload)
    monkeypatch.setattr(source, "_cvm_rows", cvm_rows)
    monkeypatch.setattr(source, "_cvm_document", cvm_document)

    started = time.monotonic()
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3")],
        date(2026, 8, 27),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert result.coverage[0].status == "partial"
    assert result.coverage[0].complete is False
    assert result.coverage[0].detail is not None
    assert len(result.observations) == 2
    assert {item.source for item in result.observations} == {"b3", "cvm"}
    assert not source._cvm_document_tasks


@pytest.mark.asyncio
async def test_official_collection_deadline_bounds_b3_wait_without_complete_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = OfficialCompanyIncomeSource(Settings(income_source_total_timeout_seconds=0.1))
    release = asyncio.Event()

    async def b3_payload(
        _client: httpx.AsyncClient,
        _ticker: str,
        *,
        deadline: float | None,
    ) -> object:
        if deadline is None:
            await release.wait()
        else:
            await asyncio.wait_for(
                release.wait(),
                timeout=max(deadline - time.monotonic(), 0),
            )
        return []

    monkeypatch.setattr(source, "_b3_payload", b3_payload)
    started = time.monotonic()
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3")],
        date(2026, 8, 27),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert result.observations == []
    assert result.coverage[0].status == "partial"
    assert result.coverage[0].complete is False
    assert result.coverage[0].detail == "official income source deadline exceeded"
    release.set()


@pytest.mark.asyncio
async def test_cvm_document_producer_owns_client_after_all_waiters_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"report")

    monkeypatch.setattr("app.income.sources._pdf_text", lambda content: content.decode())
    source = OfficialCompanyIncomeSource(Settings(), httpx.MockTransport(handler))
    link = "https://cvm.test/report.pdf"

    async with source._client() as client:
        waiter = asyncio.create_task(source._cvm_document_text(client, link))
        await started.wait()
        producer = source._cvm_document_tasks[link]
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    # The request-scoped client is closed before the shared producer is
    # released; the producer owns a separate bounded client lifetime.
    release.set()
    assert await asyncio.shield(producer) == "report"
    await asyncio.sleep(0)
    assert source._cvm_document_tasks == {}
    assert source._cvm_documents[link][1] == "report"


@pytest.mark.asyncio
async def test_cvm_document_deadline_cancels_semaphore_queue_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200, content=b"never")

    monkeypatch.setattr("app.income.sources._pdf_text", lambda content: content.decode())
    source = OfficialCompanyIncomeSource(
        Settings(upstream_concurrency=1),
        httpx.MockTransport(handler),
    )
    links = ("https://cvm.test/one.pdf", "https://cvm.test/two.pdf")
    deadline = time.monotonic() + 0.05

    async with source._client() as client:
        results = await asyncio.gather(
            *(source._cvm_document_text(client, link, deadline=deadline) for link in links),
            return_exceptions=True,
        )

    assert all(isinstance(result, TimeoutError) for result in results)
    await asyncio.sleep(0)
    assert source._cvm_download_semaphore._value == 1
    assert source._cvm_document_tasks == {}


@pytest.mark.asyncio
async def test_official_collection_keeps_b3_observations_when_cvm_has_no_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "isinCode": "BRBBASACNOR3",
        "label": "DIVIDENDO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,50",
    }
    source = OfficialCompanyIncomeSource(Settings(income_source_total_timeout_seconds=0.1))

    async def b3_payload(
        _client: httpx.AsyncClient,
        _ticker: str,
        *,
        deadline: float | None,
    ) -> object:
        del deadline
        return [{"codeCVM": "1023", "cashDividends": [row]}]

    async def cvm_rows(
        _client: httpx.AsyncClient,
        _year: int,
        *,
        deadline: float | None,
    ) -> list[dict[str, str]]:
        del deadline
        return []

    monkeypatch.setattr(source, "_b3_payload", b3_payload)
    monkeypatch.setattr(source, "_cvm_rows", cvm_rows)
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3")],
        date(2026, 8, 27),
    )

    assert len(result.observations) == 1
    assert result.observations[0].source == "b3"
    assert result.coverage[0].status == "complete"
    assert result.coverage[0].complete is True


@pytest.mark.asyncio
async def test_deadline_expiry_closes_unstarted_coroutine() -> None:
    async def never_started() -> None:
        await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        await _await_until(never_started(), time.monotonic() - 1)
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    with pytest.raises(TimeoutError):
        await _await_until(future, time.monotonic() - 1)


@pytest.mark.asyncio
async def test_shared_document_producer_keeps_long_waiter_after_short_waiter_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"report")

    monkeypatch.setattr("app.income.sources._pdf_text", lambda content: content.decode())
    source = OfficialCompanyIncomeSource(Settings(), httpx.MockTransport(handler))
    link = "https://cvm.test/report.pdf"

    async with source._client() as client:
        long_deadline = time.monotonic() + 1
        short_deadline = time.monotonic() + 0.05
        first = asyncio.create_task(source._cvm_document_text(client, link, deadline=long_deadline))
        await started.wait()
        second = asyncio.create_task(
            source._cvm_document_text(client, link, deadline=short_deadline)
        )
        with pytest.raises(TimeoutError):
            await second
        release.set()
        assert await first == "report"

    await asyncio.sleep(0)
    assert source._cvm_document_tasks == {}


@pytest.mark.asyncio
async def test_cvm_cache_keeps_pinned_entry_when_no_unpinned_space_exists() -> None:
    text = "x" * 128
    source = OfficialCompanyIncomeSource(
        Settings(cvm_income_document_cache_max_bytes=sys.getsizeof(text)),
    )
    source._cvm_documents["pinned"] = (time.monotonic(), text)
    source._cvm_document_sizes["pinned"] = sys.getsizeof(text)
    source._cvm_document_cache_bytes = sys.getsizeof(text)
    producer = asyncio.create_task(asyncio.sleep(60))
    source._cvm_document_tasks["pinned"] = producer  # type: ignore[assignment]

    source._cache_cvm_document("new", text)

    assert list(source._cvm_documents) == ["pinned"]
    assert source._cvm_document_cache_bytes == sys.getsizeof(text)
    producer.cancel()
    await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_official_collection_marks_document_failure_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b3_row = {
        "isinCode": "BRBBASACNOR3",
        "label": "DIVIDENDO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,50",
    }
    source = OfficialCompanyIncomeSource(Settings(income_source_total_timeout_seconds=0.1))

    async def b3_payload(
        _client: httpx.AsyncClient,
        _ticker: str,
        *,
        deadline: float | None,
    ) -> object:
        del deadline
        return [{"codeCVM": "1023", "cashDividends": [b3_row]}]

    async def cvm_rows(
        _client: httpx.AsyncClient,
        year: int,
        *,
        deadline: float | None,
    ) -> list[dict[str, str]]:
        del deadline
        if year != 2026:
            return []
        return [
            {
                "Codigo_CVM": "1023",
                "Categoria": "Relatório Proventos",
                "Link_Download": "https://cvm.test/failing.pdf",
                "Versao": "1",
                "Protocolo_Entrega": "failing",
                "Data_Entrega": "2026-08-20",
            }
        ]

    async def cvm_document(
        _client: httpx.AsyncClient,
        _instrument: IncomeInstrumentRequest,
        _row: dict[str, str],
        *,
        deadline: float | None,
    ) -> list[IncomeEventObservation]:
        del deadline
        raise ValueError("invalid CVM report")

    monkeypatch.setattr(source, "_b3_payload", b3_payload)
    monkeypatch.setattr(source, "_cvm_rows", cvm_rows)
    monkeypatch.setattr(source, "_cvm_document", cvm_document)
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3")],
        date(2026, 8, 27),
    )

    assert len(result.observations) == 1
    assert result.observations[0].source == "b3"
    assert result.coverage[0].status == "partial"
    assert result.coverage[0].complete is False
    assert result.coverage[0].detail == "CVM income document refresh was incomplete"


@pytest.mark.asyncio
async def test_instrument_preserves_b3_when_cvm_deadline_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "isinCode": "BRBBASACNOR3",
        "label": "DIVIDENDO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,50",
    }
    source = OfficialCompanyIncomeSource(Settings())

    async def b3_payload(
        _client: httpx.AsyncClient,
        _ticker: str,
        *,
        deadline: float | None,
    ) -> object:
        del deadline
        return [{"codeCVM": "1023", "cashDividends": [row]}]

    async def cvm_events(*_args: object, **_kwargs: object) -> object:
        raise TimeoutError("deadline")

    monkeypatch.setattr(source, "_b3_payload", b3_payload)
    monkeypatch.setattr(source, "_cvm_events", cvm_events)
    async with source._client() as client:
        result = await source._instrument(
            client,
            IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3"),
            date(2026, 8, 27),
            deadline=time.monotonic() + 1,
        )

    assert result.complete is False
    assert result.detail == "official income source deadline exceeded while loading CVM data"
    assert len(result.observations) == 1
    assert result.observations[0].source == "b3"
