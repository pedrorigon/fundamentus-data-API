from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from app.api.dependencies import get_income_event_service
from app.config import Settings
from app.income.parsers import (
    parse_b3_income_events,
    parse_cvm_income_report_text,
    parse_fundos_net_xml,
    parse_status_invest_income_events,
)
from app.income.resolver import canonical_event_type, resolve_income_events
from app.income.service import IncomeEventService
from app.income.sources import (
    FundamentusIncomeSource,
    FundosNetIncomeSource,
    IncomeSourceResult,
    OfficialCompanyIncomeSource,
    StatusInvestIncomeSource,
    _fundos_net_candidates,
    _latest_cvm_documents,
    _read_cvm_zip,
)
from app.income.store import IncomeEventStore
from app.main import create_app
from app.models import (
    Dividend,
    IncomeEventBatchRequest,
    IncomeEventObservation,
    IncomeEventRefreshRequest,
    IncomeEventStatus,
    IncomeInstrumentRequest,
    IncomeSourceCoverage,
)


def _observation(
    source: str,
    *,
    lineage: str | None = None,
    event_type: str = "Dividendo",
    payment_date: date = date(2026, 9, 11),
    amount: str = "0.50",
    authority: int = 20,
    source_event_id: str | None = None,
    version: int = 1,
) -> IncomeEventObservation:
    return IncomeEventObservation(
        source=source,
        lineage=lineage or f"lineage:{source}",
        source_event_id=source_event_id or f"{source}-event",
        ticker="BBAS3",
        isin="BRBBASACNOR3",
        event_type=event_type,
        ex_date=date(2026, 9, 1),
        payment_date=payment_date,
        unit_price=Decimal(amount),
        source_version=version,
        authority=authority,
        payload_hash=f"hash-{source}-{version}",
    )


def test_b3_parser_filters_isin_deduplicates_and_returns_cvm_code() -> None:
    row = {
        "isinCode": "BRBBASACNOR3",
        "label": "JRS CAP PROPRIO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,03450628312",
        "relatedTo": "3º Trimestre/2026",
    }
    payload = [{"codeCVM": "1023", "cashDividends": [row, row, {**row, "isinCode": "OTHER"}]}]

    events, code = parse_b3_income_events(
        payload,
        ticker="BBAS3",
        requested_isin="BRBBASACNOR3",
    )

    assert code == "1023"
    assert len(events) == 1
    assert events[0].event_type == "JRS CAP PROPRIO"
    assert events[0].ex_date == date(2026, 9, 1)
    assert events[0].unit_price == Decimal("0.03450628312")
    assert parse_b3_income_events({}, ticker="BBAS3") == ([], None)
    invalid, _code = parse_b3_income_events(
        [{"cashDividends": [None, {"lastDatePrior": "bad", "rate": "-"}]}],
        ticker="BBAS3",
    )
    assert invalid == []


def test_fundos_net_parser_reads_income_and_amortization() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <DadosEconomicoFinanceiros><InformeRendimentos><Provento>
      <CodISIN>BRHGLGCTF004</CodISIN><CodNegociacao>HGLG11</CodNegociacao>
      <Rendimento><DataBase>2026-07-31</DataBase><ValorProvento>1.17</ValorProvento>
      <DataPagamento>2026-08-14</DataPagamento><PeriodoReferencia>Julho</PeriodoReferencia></Rendimento>
      <Amortizacao><DataBase>2026-07-31</DataBase><ValorProvento>0.25</ValorProvento>
      <DataPagamento>2026-08-14</DataPagamento></Amortizacao>
    </Provento></InformeRendimentos></DadosEconomicoFinanceiros>"""

    events = parse_fundos_net_xml(xml, document_id="1272107", version=2)

    assert [event.event_type for event in events] == ["Rendimento", "Amortização"]
    assert events[0].ticker == "HGLG11"
    assert events[0].source_version == 2
    assert events[0].authority == 100
    assert parse_fundos_net_xml(b"not xml", document_id="bad") == []
    assert parse_fundos_net_xml(b"<root><Provento/></root>", document_id="empty") == []


def test_status_invest_parser_keeps_ex_and_payment_dates() -> None:
    payload = json.dumps(
        [
            {
                "id": 7,
                "et": "RENDIMENTO",
                "etd": "Rendimento",
                "ed": "31/07/2026",
                "pd": "14/08/2026",
                "v": 1.17,
            },
            {"id": 8, "et": "RENDIMENTO", "ed": "bad", "pd": "14/08/2026", "v": 1},
        ]
    ).replace('"', "&quot;")
    html = f'<section id="earning-section"><input id="results" value="{payload}"></section>'

    events = parse_status_invest_income_events(html, ticker="HGLG11")

    assert len(events) == 1
    assert events[0].ex_date == date(2026, 7, 31)
    assert events[0].payment_date == date(2026, 8, 14)
    assert parse_status_invest_income_events("<html></html>", ticker="HGLG11") == []
    assert (
        parse_status_invest_income_events(
            '<section id="earning-section"><input id="results" value="{}"></section>',
            ticker="HGLG11",
        )
        == []
    )
    assert (
        parse_status_invest_income_events(
            '<section id="earning-section"><input id="results" value="not-json"></section>',
            ticker="HGLG11",
        )
        == []
    )


def test_cvm_parser_repairs_wrapped_isin_and_amount() -> None:
    text = """Provento
    Data Aprovação
    Ultimo dia de negociação com Direitos
    14/08/2026
    01/09/2026
    Código ISIN
    Valor Bruto (R$/Unidade)
    Data Pagamento
    BRBBASACN
    OR3
    0,0345062831
    2
    3º Trimestre 2026 Não A Vista 11/09/2026
    """

    events = parse_cvm_income_report_text(text, ticker="BBAS3", document_id="1559729", version=2)

    assert len(events) == 1
    assert events[0].isin == "BRBBASACNOR3"
    assert events[0].unit_price == Decimal("0.03450628312")
    assert events[0].source_event_id == "cvm:1559729:0"
    assert (
        parse_cvm_income_report_text("missing fields", ticker="BBAS3", document_id="x", version=1)
        == []
    )


def test_resolver_prefers_authority_and_attaches_generic_official_type() -> None:
    observations = [
        _observation("cvm", event_type="Provento", authority=100),
        _observation("status", event_type="JCP", authority=20),
        _observation("fundamentus", event_type="Juros Sobre Capital Próprio", authority=20),
    ]

    events = resolve_income_events(observations)

    assert len(events) == 1
    assert events[0].event_type == "Juros Sobre Capital Próprio"
    assert events[0].status is IncomeEventStatus.verified
    assert events[0].field_sources["payment_date"] == "cvm"
    assert events[0].projectable is True
    assert canonical_event_type("rend. trib.") == "Rendimento"


def test_resolver_requires_independent_lineages_and_detects_official_conflict() -> None:
    same_lineage = [
        _observation("first", lineage="copied:b3"),
        _observation("second", lineage="copied:b3"),
    ]
    assert resolve_income_events(same_lineage)[0].status is IncomeEventStatus.tentative

    corroborated = [*same_lineage, _observation("third", lineage="independent")]
    assert resolve_income_events(corroborated)[0].status is IncomeEventStatus.corroborated

    conflicting = [
        _observation("cvm", authority=100),
        _observation("b3", authority=90, payment_date=date(2026, 9, 12)),
    ]
    assert resolve_income_events(conflicting)[0].status is IncomeEventStatus.conflicted
    assert resolve_income_events([_observation("invalid", amount="0")]) == []


@pytest.mark.asyncio
async def test_store_publishes_semantic_changes_and_filters_reads(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    first = _observation("cvm", authority=100, version=1)
    corrected = _observation("cvm", authority=100, version=2, payment_date=date(2026, 9, 12))
    await store.save_observations([first, corrected])
    observations = await store.observations(["BBAS3"])
    assert observations == [corrected]

    canonical = resolve_income_events(observations)
    assert await store.publish(canonical) == 1
    assert await store.publish(canonical) == 0
    assert (await store.events(["BBAS3"]))[0].revision == 1
    assert await store.events(["PETR4"]) == []
    assert await store.events([]) == []
    assert await store.observations([]) == []
    await store.save_coverage([])
    assert await store.publish([]) == 0
    filtered = await store.events(
        ["BBAS3"],
        from_date=date(2026, 9, 12),
        to_date=date(2026, 9, 12),
    )
    assert filtered
    changes, cursor, has_more = await store.changes(0, limit=1)
    assert len(changes) == 1
    assert cursor == await store.cursor() == 1
    assert has_more is False

    replacement = resolve_income_events(
        [_observation("cvm", authority=100, amount="0.75", source_event_id="replacement")]
    )
    assert await store.publish(replacement, scope_tickers=["BBAS3"]) == 2
    visible = await store.events(["BBAS3"])
    assert visible == replacement
    changes, _cursor, _has_more = await store.changes(1, limit=10)
    assert {item.status for item in changes} == {
        IncomeEventStatus.cancelled,
        IncomeEventStatus.verified,
    }
    await store.close()
    with pytest.raises(RuntimeError, match="not started"):
        await store.events(["BBAS3"])


class _Source:
    name = "fake"
    snapshot_sources: tuple[str, ...] = ("official",)

    def __init__(self, *, delay: float = 0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        del as_of
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("offline")
        return IncomeSourceResult(
            [_observation("official", authority=100)],
            [
                IncomeSourceCoverage(
                    source=self.name, ticker=instruments[0].ticker, status="complete", complete=True
                )
            ],
        )


@pytest.mark.asyncio
async def test_service_singleflight_batch_and_failed_source(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    source = _Source(delay=0.02)
    failed = _Source(fail=True)
    failed.name = "failed"
    service = IncomeEventService(store, [source, failed])
    request = IncomeEventRefreshRequest(instruments=[IncomeInstrumentRequest(ticker="bbas3")])

    first, second = await asyncio.gather(service.refresh(request), service.refresh(request))

    assert first == second
    assert source.calls == 1
    assert first.failed_sources == ["failed"]
    batch = await service.batch(IncomeEventBatchRequest(tickers=["bbas3"]))
    assert len(batch.events) == 1
    changed = await service.changes(0, 100)
    assert changed.events == batch.events

    source.fail = False
    source.collect = _empty_source_collect.__get__(source, _Source)  # type: ignore[method-assign]
    refreshed = await service.refresh(request)
    assert refreshed.published == 1
    assert (await service.batch(IncomeEventBatchRequest(tickers=["BBAS3"]))).events == []
    await store.close()


async def _empty_source_collect(
    self: _Source,
    instruments: Sequence[IncomeInstrumentRequest],
    as_of: date,
) -> IncomeSourceResult:
    del as_of
    self.calls += 1
    return IncomeSourceResult(
        [],
        [
            IncomeSourceCoverage(
                source=self.name,
                ticker=instruments[0].ticker,
                status="empty",
                complete=True,
            )
        ],
    )


@pytest.mark.asyncio
async def test_income_event_routes_are_cached_and_refresh_is_local(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    service = IncomeEventService(store, [_Source()])
    app = create_app()
    app.dependency_overrides[get_income_event_service] = lambda: service
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        refreshed = await client.post(
            "/v2/income-events/refresh",
            json={"instruments": [{"ticker": "BBAS3"}]},
        )
        assert refreshed.status_code == 200
        batch = await client.post("/v2/income-events/batch", json={"tickers": ["BBAS3"]})
        assert batch.status_code == 200
        assert batch.headers["etag"] == 'W/"income-1"'
        changes = await client.get("/v2/income-events/changes", params={"cursor": 0})
        assert changes.json()["events"][0]["ticker"] == "BBAS3"
    await store.close()


@pytest.mark.asyncio
async def test_status_source_tries_fund_then_stock_path() -> None:
    payload = json.dumps(
        [{"id": 1, "et": "DIVIDENDO", "ed": "01/09/2026", "pd": "11/09/2026", "v": 0.5}]
    ).replace('"', "&quot;")

    def handler(request: httpx.Request) -> httpx.Response:
        if "fundos-imobiliarios" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200,
            text=f'<section id="earning-section"><input id="results" value="{payload}"></section>',
        )

    source = StatusInvestIncomeSource(Settings(), httpx.MockTransport(handler))
    result = await source.collect([IncomeInstrumentRequest(ticker="BBAS3")], date(2026, 8, 27))
    assert len(result.observations) == 1
    assert result.coverage[0].complete is True


@pytest.mark.asyncio
async def test_fundamentus_source_filters_incomplete_rows_and_isolates_failure() -> None:
    class Assets:
        async def get_dividends(
            self,
            ticker: str,
            *,
            force_refresh: bool,
        ) -> tuple[list[Dividend], bool]:
            assert force_refresh is True
            if ticker == "FAIL3":
                raise RuntimeError("offline")
            return (
                [
                    Dividend(
                        ex_date=date(2026, 9, 1),
                        payment_date=date(2026, 9, 11),
                        value=Decimal("0.5"),
                        type="Dividendo",
                        is_future_payment=True,
                        is_future_ex_date=True,
                        raw={},
                    ),
                    Dividend(
                        ex_date=None,
                        payment_date=date(2026, 9, 11),
                        value=Decimal("0.5"),
                        type="Dividendo",
                        is_future_payment=True,
                        is_future_ex_date=False,
                        raw={},
                    ),
                ],
                False,
            )

    source = FundamentusIncomeSource(Assets())  # type: ignore[arg-type]
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3"), IncomeInstrumentRequest(ticker="FAIL3")],
        date(2026, 8, 27),
    )
    assert len(result.observations) == 1
    assert [item.complete for item in result.coverage] == [True, False]


@pytest.mark.asyncio
async def test_status_source_marks_http_failure_incomplete() -> None:
    source = StatusInvestIncomeSource(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3")],
        date(2026, 8, 27),
    )
    assert result.observations == []
    assert result.coverage[0].complete is False


@pytest.mark.asyncio
async def test_official_company_source_combines_b3_and_cvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_payload = (
        "CNPJ_Companhia;Nome_Companhia;Codigo_CVM;Data_Referencia;Categoria;Tipo;Especie;Assunto;"
        "Data_Entrega;Tipo_Apresentacao;Protocolo_Entrega;Versao;Link_Download\n"
        "00;BB;1023;2026-08-19;Relatório Proventos;;;;2026-08-19;AP;;2;https://cvm.test/report.pdf\n"
    ).encode("iso-8859-1")
    archive = _zip(csv_payload)
    b3 = [{"codeCVM": "1023", "cashDividends": []}]
    report = (
        "Ultimo dia de negociação com Direitos\n14/08/2026\n01/09/2026\n"
        "Código ISIN\nBRBBASACNOR3 0,5 11/09/2026"
    )
    archive_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal archive_requests
        if request.url.host == "cvm.test":
            return httpx.Response(200, content=b"pdf")
        if "ipe_cia_aberta_2026" in request.url.path:
            archive_requests += 1
            return httpx.Response(200, content=archive)
        if "ipe_cia_aberta_2025" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=b3)

    monkeypatch.setattr("app.income.sources._pdf_text", lambda _content: report)
    settings = Settings(
        b3_listed_companies_base_url="https://b3.test",
        cvm_open_data_base_url="https://dados.test",
    )
    source = OfficialCompanyIncomeSource(settings, httpx.MockTransport(handler))
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3")],
        date(2026, 8, 27),
    )
    assert len(result.observations) == 1
    assert result.observations[0].source == "cvm"
    assert result.coverage[0].complete is True
    await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3", isin="BRBBASACNOR3")],
        date(2026, 8, 27),
    )
    assert archive_requests == 1


@pytest.mark.asyncio
async def test_official_company_source_uses_b3_without_cvm_code() -> None:
    row = {
        "isinCode": "BRBBASACNOR3",
        "label": "DIVIDENDO",
        "lastDatePrior": "01/09/2026",
        "paymentDate": "11/09/2026",
        "rate": "0,5",
    }
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=[{"cashDividends": [row]}])
    )
    source = OfficialCompanyIncomeSource(
        Settings(b3_listed_companies_base_url="https://b3.test"),
        transport,
    )
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3")],
        date(2026, 8, 27),
    )
    assert len(result.observations) == 1


@pytest.mark.asyncio
async def test_official_company_source_isolates_transport_failure() -> None:
    source = OfficialCompanyIncomeSource(
        Settings(b3_listed_companies_base_url="https://b3.test"),
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="BBAS3")],
        date(2026, 8, 27),
    )
    assert result.coverage[0].complete is False


@pytest.mark.asyncio
async def test_fundos_net_source_filters_requested_ticker() -> None:
    listing = {
        "data": [
            {"id": 1272107, "versao": 2, "status": "AC", "descricaoFundo": "CSHG LOGISTICA FII"}
        ]
    }
    xml = b"""<DadosEconomicoFinanceiros><InformeRendimentos><Provento>
    <CodISIN>BRHGLGCTF004</CodISIN><CodNegociacao>HGLG11</CodNegociacao><Rendimento>
    <DataBase>2026-07-31</DataBase><ValorProvento>1.17</ValorProvento>
    <DataPagamento>2026-08-14</DataPagamento></Rendimento></Provento></InformeRendimentos>
    </DadosEconomicoFinanceiros>"""

    requests = {"index": 0, "document": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        key = "index" if "pesquisar" in request.url.path else "document"
        requests[key] += 1
        return (
            httpx.Response(200, json=listing)
            if "pesquisar" in request.url.path
            else httpx.Response(200, content=xml)
        )

    source = FundosNetIncomeSource(
        Settings(fundos_net_base_url="https://fnet.test"),
        httpx.MockTransport(handler),
    )
    result = await source.collect(
        [IncomeInstrumentRequest(ticker="HGLG11", name="CSHG Logística")],
        date(2026, 8, 27),
    )
    assert len(result.observations) == 1
    assert result.observations[0].source_version == 2
    repeated = await asyncio.gather(
        source.collect(
            [IncomeInstrumentRequest(ticker="HGLG11", name="CSHG Logística")],
            date(2026, 8, 27),
        ),
        source.collect(
            [IncomeInstrumentRequest(ticker="HGLG11", name="CSHG Logística")],
            date(2026, 8, 27),
        ),
    )
    assert all(item.observations for item in repeated)
    assert requests == {"index": 1, "document": 1}


@pytest.mark.asyncio
async def test_fundos_net_source_preserves_empty_and_failed_coverage() -> None:
    empty = FundosNetIncomeSource(
        Settings(fundos_net_base_url="https://fnet.test"),
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": []})),
    )
    result = await empty.collect(
        [IncomeInstrumentRequest(ticker="HGLG11")],
        date(2026, 8, 27),
    )
    assert result.coverage[0].status == "empty"

    invalid = FundosNetIncomeSource(
        Settings(fundos_net_base_url="https://fnet.test"),
        httpx.MockTransport(lambda _request: httpx.Response(200, json=[])),
    )
    failed = await invalid.collect(
        [IncomeInstrumentRequest(ticker="HGLG11")],
        date(2026, 8, 27),
    )
    assert failed.coverage[0].status == "failed"


def test_source_index_helpers_are_bounded_and_versioned() -> None:
    rows = [
        {"id": 1, "descricaoFundo": "CSHG LOGISTICA"},
        {"id": 2, "descricaoFundo": "OTHER"},
    ]
    selected = _fundos_net_candidates(
        rows,
        {"HGLG11": IncomeInstrumentRequest(ticker="HGLG11", name="CSHG Logística")},
        1,
    )
    assert [row["id"] for row in selected] == [1]

    cvm = [
        {"Link_Download": "?numProtocolo=1", "Versao": "1", "Data_Entrega": "2026-01-01"},
        {"Link_Download": "?numProtocolo=1", "Versao": "2", "Data_Entrega": "2026-01-02"},
    ]
    assert _latest_cvm_documents(cvm)[0]["Versao"] == "2"
    assert _read_cvm_zip(_zip(b"a;b\n1;2\n")) == [{"a": "1", "b": "2"}]
    with pytest.raises(ValueError, match="exactly one"):
        _read_cvm_zip(_zip_many())


def _zip(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("ipe.csv", content)
    return buffer.getvalue()


def _zip_many() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("first.csv", b"a\n1\n")
        archive.writestr("second.csv", b"a\n2\n")
    return buffer.getvalue()
