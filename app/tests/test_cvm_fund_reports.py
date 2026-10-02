from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.core.errors import APIError, ProviderInvalidResponseError, ProviderUnavailableError
from app.models import InstrumentMetadata, InstrumentType
from app.scrapers.cvm_fund_reports import (
    CvmFundReportProvider,
    FundReportSeries,
    merge_report_series,
    parse_daily_fund_reports,
    parse_fiagro_reports,
    parse_fii_reports,
)


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("latin-1"))
    return buffer.getvalue()


def _instrument(
    instrument_type: InstrumentType = InstrumentType.fii,
) -> InstrumentMetadata:
    return InstrumentMetadata(
        ticker="XPML11",
        name="XP MALLS FDO INV IMOB FII RESP LIM",
        instrument_type=instrument_type,
        isin="BRXPMLCTF000",
    )


def test_fii_parser_matches_the_fund_name_when_an_isin_is_reused() -> None:
    payload = _zip(
        {
            "inf_mensal_fii_geral_2026.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Nome_Fundo_Classe;Codigo_ISIN;"
                "Quantidade_Cotas_Emitidas;Data_Funcionamento;Segmento_Atuacao;"
                "Nome_Administrador\n"
                "07.583.627/0001-61;2026-06-01;PENINSULA FII;BRXPMLCTF000;"
                "100;2019-01-01;Tijolo;OLD ADMIN\n"
                "28.757.546/0001-00;2026-06-01;XP MALLS FII;BRXPMLCTF000;"
                "20000000;2017-12-15;Shoppings;XP ADMINISTRADORA\n"
            ),
            "inf_mensal_fii_complemento_2026.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Valor_Patrimonial_Cotas;"
                "Percentual_Dividend_Yield_Mes;Percentual_Rentabilidade_Patrimonial_Mes;"
                "Percentual_Rentabilidade_Efetiva_Mes;Patrimonio_Liquido;"
                "Total_Numero_Cotistas;Percentual_Despesas_Taxa_Administracao\n"
                "07.583.627/0001-61;2026-06-01;900,00;0,0010;0,0020;0,0030;"
                "90000;10;0,0009\n"
                "28.757.546/0001-00;2026-06-01;102,50;0,0095;0,0120;0,0140;"
                "2050000000;150000;0,0006\n"
            ),
            "inf_mensal_fii_ativo_passivo_2026.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Total_Necessidades_Liquidez;"
                "Total_Investido;Valores_Receber;Total_Passivo;Direitos_Bens_Imoveis;"
                "Imoveis_Renda_Acabados;CRI;Disponibilidades;Titulos_Publicos\n"
                "28.757.546/0001-00;2026-06-01;75000000;2000000000;125000000;"
                "150000000;1800000000;1800000000;100000000;50000000;25000000\n"
            ),
        }
    )

    result = parse_fii_reports(payload, _instrument())

    assert result.cnpj == "28757546000100"
    assert result.reports[0].nav_per_share == Decimal("102.50")
    assert result.reports[0].monthly_distribution_yield == Decimal("0.0095")
    assert result.reports[0].monthly_nav_return == Decimal("0.012")
    assert result.reports[0].monthly_effective_return == Decimal("0.014")
    assert result.reports[0].net_assets == Decimal("2050000000")
    assert result.reports[0].issued_shares == Decimal("20000000")
    assert result.reports[0].shareholder_count == Decimal("150000")
    assert result.reports[0].administration_fee_ratio == Decimal("0.0072")
    assert result.reports[0].total_assets == Decimal("2200000000")
    assert result.reports[0].total_liabilities == Decimal("150000000")
    assert result.reports[0].property_assets == Decimal("1800000000")
    assert result.reports[0].credit_assets == Decimal("100000000")
    assert result.reports[0].liquid_assets == Decimal("75000000")
    assert result.reports[0].inception_date == date(2017, 12, 15)
    assert result.reports[0].segment == "Shoppings"
    assert result.reports[0].administrator == "XP ADMINISTRADORA"


def test_fii_parser_rejects_archives_without_required_members_or_matches() -> None:
    assert parse_fii_reports(_zip({"other.csv": "a;b\n1;2\n"}), _instrument()).reports == ()

    payload = _zip(
        {
            "geral.csv": (
                "CNPJ_Fundo_Classe;Nome_Fundo_Classe;Codigo_ISIN\n1;OTHER;BROTHER00000\n"
            ),
            "complemento.csv": ("CNPJ_Fundo_Classe;Data_Referencia;Valor_Patrimonial_Cotas\n"),
        }
    )
    assert parse_fii_reports(payload, _instrument()).reports == ()


@pytest.mark.asyncio
async def test_provider_rejects_oversized_or_invalid_archives() -> None:
    oversized = CvmFundReportProvider(
        Settings(archive_download_max_bytes=1),
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"large")),
    )
    invalid = CvmFundReportProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not a zip")),
    )

    with pytest.raises(ProviderInvalidResponseError):
        await oversized._request("/oversized.zip")
    with pytest.raises(ProviderInvalidResponseError):
        await invalid._request("/invalid.zip")


def test_fiagro_parser_accepts_legacy_isin_check_digits() -> None:
    payload = _zip(
        {
            "inf_mensal_fiagro_202606.csv": (
                "CNPJ_Classe;Nome_Classe;Data_Referencia;Codigo_ISIN;"
                "Valor_Patrimonial_Cotas;Dividend_Yield_Mes;Rentabilidade_Patrimonial_Mes;"
                "Rentabilidade_Efetiva_Mes\n"
                "41745701000137;KINEA CREDITO AGRO;2026-06-01;BRKNCACTF014;"
                "100.59;1.12;-0.18;0.94\n"
            ),
            "inf_mensal_fiagro_subclasse_202606.csv": "CNPJ_Classe;Nome_Subclasse\n",
        }
    )
    instrument = InstrumentMetadata(
        ticker="KNCA11",
        name="KINEA CREDITO AGRO FIAGRO",
        instrument_type=InstrumentType.fiagro,
        isin="BRKNCACTF006",
    )

    result = parse_fiagro_reports(payload, instrument)

    assert result.cnpj == "41745701000137"
    assert result.reports[0].nav_per_share == Decimal("100.59")
    assert result.reports[0].monthly_distribution_yield == Decimal("0.0112")


def test_fiagro_parser_maps_asset_breakdown_to_fund_fields() -> None:
    header = (
        "CNPJ_Classe;Nome_Classe;Data_Referencia;Codigo_ISIN;Valor_Patrimonial_Cotas;"
        "Dividend_Yield_Mes;Cotas_Emitidas;Valor_Ativo;Total_Passivo;Total_Necessidades_Liquidez;"
        "Imoveis_Rurais;Titulos_Securitizacao;CRA;CRI;Valor_Titulos_Credito;CPR;"
        "Cotas_Fundos_Investimento;FIAGRO\n"
    )
    # Values of the Kinea Credito Agro report for August 2026; subtotals and
    # their components are both present and must not be added twice.
    row = (
        "41745701000137;KINEA CREDITO AGRO;2026-08-01;BRKNCACTF014;100.12;0.85;21599919;"
        "2183328169.43;20743852.51;435397057.07;;1316018389.86;1251159155.09;64859234.77;"
        "133188620.97;133188620.97;298594736.08;298594736.08\n"
    )
    payload = _zip(
        {
            "inf_mensal_fiagro_202608.csv": header + row,
            "inf_mensal_fiagro_subclasse_202608.csv": "CNPJ_Classe;Nome_Subclasse\n",
        }
    )
    instrument = InstrumentMetadata(
        ticker="KNCA11",
        name="KINEA CREDITO AGRO FIAGRO",
        instrument_type=InstrumentType.fiagro,
        isin="BRKNCACTF014",
    )

    report = parse_fiagro_reports(payload, instrument).reports[0]

    assert report.total_assets == Decimal("2183328169.43")
    assert report.total_liabilities == Decimal("20743852.51")
    assert report.liquid_assets == Decimal("435397057.07")
    assert report.credit_assets == Decimal("1449207010.83")
    assert report.property_assets is None
    assert report.issued_shares == Decimal("21599919")


def test_daily_fund_parser_keeps_the_last_nav_of_each_month() -> None:
    payload = _zip(
        {
            "inf_diario_fi_202607.csv": (
                "CNPJ_FUNDO_CLASSE;DT_COMPTC;VL_QUOTA\n"
                "42.730.834/0001-00;2026-07-01;98.50\n"
                "42.730.834/0001-00;2026-07-24;99.75\n"
                "00.000.000/0001-00;2026-07-24;10.00\n"
            )
        }
    )

    result = parse_daily_fund_reports(payload, "42730834000100")

    assert result == (result[0],)
    assert result[0].as_of == date(2026, 7, 24)
    assert result[0].nav_per_share == Decimal("99.75")


def test_report_series_merge_prefers_the_cnpj_with_more_observations() -> None:
    first = parse_daily_fund_reports(
        _zip(
            {
                "inf_diario_fi_202601.csv": (
                    "CNPJ_FUNDO_CLASSE;DT_COMPTC;VL_QUOTA\n1;2026-01-30;10\n"
                )
            }
        ),
        "1",
    )
    second = parse_daily_fund_reports(
        _zip(
            {
                "inf_diario_fi_202602.csv": (
                    "CNPJ_FUNDO_CLASSE;DT_COMPTC;VL_QUOTA\n2;2026-01-30;20\n2;2026-02-27;21\n"
                )
            }
        ),
        "2",
    )

    result = merge_report_series(
        [
            FundReportSeries(cnpj="1", reports=first),
            FundReportSeries(cnpj="2", reports=second),
        ]
    )

    assert result.cnpj == "2"
    assert [item.nav_per_share for item in result.reports] == [
        Decimal("20"),
        Decimal("21"),
    ]


@pytest.mark.asyncio
async def test_provider_loads_fi_infra_archives_once_and_handles_missing_months() -> None:
    calls: list[str] = []
    payload = _zip(
        {
            "inf_diario_fi_202607.csv": (
                "CNPJ_FUNDO_CLASSE;DT_COMPTC;VL_QUOTA\n42.730.834/0001-00;2026-07-24;99.75\n"
            )
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("202607.zip"):
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    provider = CvmFundReportProvider(Settings(), httpx.MockTransport(handler))
    instrument = InstrumentMetadata(
        ticker="JURO11",
        name="SPARTA INFRA",
        instrument_type=InstrumentType.fi_infra,
    )

    first = await provider.reports(
        instrument,
        cnpj="42.730.834/0001-00",
        today=date(2026, 7, 27),
    )
    second = await provider.reports(
        instrument,
        cnpj="42.730.834/0001-00",
        today=date(2026, 7, 27),
    )

    assert first == second
    assert first is second
    assert first.reports[0].nav_per_share == Decimal("99.75")
    assert len(calls) == 18


@pytest.mark.asyncio
async def test_report_cache_is_bounded_lru_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scrapers.cvm_fund_reports as reports_module

    now = [100.0]
    calls: list[date] = []
    provider = CvmFundReportProvider(
        Settings(cvm_report_cache_max_entries=2, instrument_data_ttl_seconds=10)
    )

    async def listed_reports(
        _instrument: InstrumentMetadata,
        reference: date,
    ) -> FundReportSeries:
        calls.append(reference)
        return FundReportSeries(cnpj=reference.isoformat())

    monkeypatch.setattr(provider, "_listed_fund_reports", listed_reports)
    monkeypatch.setattr(reports_module, "monotonic", lambda: now[0])
    instrument = _instrument()
    first_date = date(2026, 6, 1)
    second_date = date(2026, 6, 2)
    third_date = date(2026, 6, 3)

    first = await provider.reports(instrument, today=first_date)
    await provider.reports(instrument, today=second_date)
    assert await provider.reports(instrument, today=first_date) is first

    third = await provider.reports(instrument, today=third_date)

    assert third.cnpj == third_date.isoformat()
    assert calls == [first_date, second_date, third_date]
    assert [key[-1] for key in provider._reports] == [first_date, third_date]

    now[0] = 111.0
    refreshed = await provider.reports(instrument, today=first_date)

    assert refreshed.cnpj == first_date.isoformat()
    assert refreshed is not first
    assert calls == [first_date, second_date, third_date, first_date]
    assert len(provider._reports) == 2


@pytest.mark.asyncio
async def test_archive_cache_is_bounded_lru_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scrapers.cvm_fund_reports as reports_module

    now = [100.0]
    calls: list[str] = []
    provider = CvmFundReportProvider(
        Settings(
            cvm_archive_cache_max_entries=2,
            cvm_archive_cache_max_bytes=16,
            instrument_data_ttl_seconds=10,
        )
    )

    async def request(path: str) -> bytes:
        calls.append(path)
        return path.encode().ljust(6, b"x")

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(reports_module, "monotonic", lambda: now[0])

    assert await provider._download("/one") == b"/onexx"
    assert await provider._download("/two") == b"/twoxx"
    assert await provider._download("/one") == b"/onexx"
    assert await provider._download("/three") == b"/three"

    assert calls == ["/one", "/two", "/three"]
    assert list(provider._archives) == ["/one", "/three"]
    assert provider._archive_cache_bytes == 12

    now[0] = 111.0
    assert await provider._download("/one") == b"/onexx"

    assert calls == ["/one", "/two", "/three", "/one"]
    assert list(provider._archives) == ["/one"]
    assert provider._archive_cache_bytes == 6


@pytest.mark.asyncio
async def test_archive_cache_skips_oversized_payloads_and_purges_expired_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scrapers.cvm_fund_reports as reports_module

    now = [100.0]
    calls: list[str] = []
    provider = CvmFundReportProvider(
        Settings(
            cvm_archive_cache_max_entries=3,
            cvm_archive_cache_max_bytes=10,
            instrument_data_ttl_seconds=10,
        )
    )
    payloads = {
        "/expired": b"123456",
        "/kept": b"1234",
        "/huge": b"12345678901",
        "/new": b"123456",
    }

    async def request(path: str) -> bytes:
        calls.append(path)
        return payloads[path]

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(reports_module, "monotonic", lambda: now[0])

    assert await provider._download("/expired") == b"123456"
    now[0] = 105.0
    assert await provider._download("/kept") == b"1234"
    assert list(provider._archives) == ["/expired", "/kept"]
    assert provider._archive_cache_bytes == 10

    now[0] = 111.0
    assert await provider._download("/huge") == b"12345678901"
    assert list(provider._archives) == ["/kept"]
    assert provider._archive_cache_bytes == 4

    assert await provider._download("/huge") == b"12345678901"
    assert calls == ["/expired", "/kept", "/huge", "/huge"]
    assert list(provider._archives) == ["/kept"]
    assert provider._archive_cache_bytes == 4

    now[0] = 116.0
    assert await provider._download("/new") == b"123456"
    assert list(provider._archives) == ["/new"]
    assert provider._archive_cache_bytes == 6


@pytest.mark.asyncio
async def test_concurrent_archive_requests_share_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    provider = CvmFundReportProvider(Settings())

    async def request(_path: str) -> bytes | None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"archive"

    monkeypatch.setattr(provider, "_request", request)
    first = asyncio.create_task(provider._download("/archive.zip"))
    await started.wait()
    second = asyncio.create_task(provider._download("/archive.zip"))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == b"archive"
    assert second_result == b"archive"
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_archive_request_is_removed_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    provider = CvmFundReportProvider(Settings())
    started = asyncio.Event()

    async def request(_path: str) -> bytes | None:
        nonlocal calls
        calls += 1
        started.set()
        if calls == 1:
            await asyncio.Future()
        return b"archive"

    monkeypatch.setattr(provider, "_request", request)
    download = asyncio.create_task(provider._download("/archive.zip"))
    await started.wait()
    download.cancel()

    with pytest.raises(asyncio.CancelledError):
        await download

    shared = provider._archive_tasks["/archive.zip"]
    shared.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shared

    assert "/archive.zip" not in provider._archive_tasks
    assert await provider._download("/archive.zip") == b"archive"
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelled_archive_waiter_keeps_shared_download_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    provider = CvmFundReportProvider(Settings())

    async def request(_path: str) -> bytes:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"archive"

    monkeypatch.setattr(provider, "_request", request)
    first = asyncio.create_task(provider._download("/archive.zip"))
    await started.wait()
    second = asyncio.create_task(provider._download("/archive.zip"))
    await asyncio.sleep(0)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    release.set()
    assert await second == b"archive"
    assert await provider._download("/archive.zip") == b"archive"
    assert calls == 1


@pytest.mark.asyncio
async def test_unexpected_archive_failure_is_removed_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    provider = CvmFundReportProvider(Settings())

    async def request(_path: str) -> bytes | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected parser failure")
        return b"archive"

    monkeypatch.setattr(provider, "_request", request)

    with pytest.raises(RuntimeError, match="unexpected parser failure"):
        await provider._download("/archive.zip")

    assert "/archive.zip" not in provider._archive_tasks
    assert await provider._download("/archive.zip") == b"archive"
    assert calls == 2


@pytest.mark.asyncio
async def test_provider_combines_legacy_and_current_listed_fund_reports() -> None:
    fii_payload = _zip(
        {
            "inf_mensal_fii_geral_2026.csv": (
                "CNPJ_Fundo_Classe;Nome_Fundo_Classe;Codigo_ISIN\n"
                "28.757.546/0001-00;XP MALLS FII;BRXPMLCTF000\n"
            ),
            "inf_mensal_fii_complemento_2026.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Valor_Patrimonial_Cotas\n"
                "28.757.546/0001-00;2026-05-01;102.50\n"
            ),
        }
    )
    fiagro_payload = _zip(
        {
            "inf_mensal_fiagro_202606.csv": (
                "CNPJ_Classe;Nome_Classe;Data_Referencia;Codigo_ISIN;"
                "Valor_Patrimonial_Cotas;Dividend_Yield_Mes\n"
                "41745701000137;KINEA CREDITO AGRO;2026-06-01;"
                "BRKNCACTF014;100.59;1.12\n"
            ),
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("inf_mensal_fii_2026.zip"):
            return httpx.Response(200, content=fii_payload)
        if path.endswith("inf_mensal_fiagro_202606.zip"):
            return httpx.Response(200, content=fiagro_payload)
        return httpx.Response(404)

    provider = CvmFundReportProvider(Settings(), httpx.MockTransport(handler))
    fii = await provider.reports(_instrument(), today=date(2026, 6, 30))
    fiagro = await provider.reports(
        InstrumentMetadata(
            ticker="KNCA11",
            name="KINEA CRÉDITO AGRO",
            instrument_type=InstrumentType.fiagro,
            isin="BRKNCACTF006",
        ),
        today=date(2026, 6, 30),
    )

    assert fii.reports[0].nav_per_share == Decimal("102.50")
    assert fiagro.reports[0].nav_per_share == Decimal("100.59")
    assert fiagro.reports[0].monthly_distribution_yield == Decimal("0.0112")


@pytest.mark.asyncio
async def test_provider_ignores_unsupported_or_unidentified_instruments() -> None:
    provider = CvmFundReportProvider(Settings(), httpx.MockTransport(lambda _: httpx.Response(500)))

    assert await provider.reports(None) == FundReportSeries()
    assert (
        await provider.reports(
            InstrumentMetadata(ticker="TEST3", instrument_type=InstrumentType.stock)
        )
        == FundReportSeries()
    )
    assert (
        await provider.reports(
            InstrumentMetadata(ticker="JURO11", instrument_type=InstrumentType.fi_infra)
        )
        == FundReportSeries()
    )

    failing = CvmFundReportProvider(
        Settings(),
        httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    with pytest.raises(ProviderUnavailableError):
        await failing.reports(_instrument(), today=date(2026, 6, 30))


@pytest.mark.asyncio
async def test_provider_surfaces_fiagro_and_fi_infra_archive_failures() -> None:
    def fiagro_handler(request: httpx.Request) -> httpx.Response:
        if "inf_mensal_fiagro_" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(404)

    fiagro_provider = CvmFundReportProvider(
        Settings(),
        httpx.MockTransport(fiagro_handler),
    )
    with pytest.raises(ProviderUnavailableError):
        await fiagro_provider.reports(
            _instrument(InstrumentType.fiagro),
            today=date(2026, 6, 30),
        )

    fi_infra_provider = CvmFundReportProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    with pytest.raises(ProviderUnavailableError):
        await fi_infra_provider.reports(
            InstrumentMetadata(
                ticker="JURO11",
                instrument_type=InstrumentType.fi_infra,
            ),
            cnpj="42.730.834/0001-00",
            today=date(2026, 6, 30),
        )


@pytest.mark.asyncio
async def test_fiagro_failure_is_not_hidden_by_empty_fii_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scrapers.cvm_fund_reports as reports_module

    provider = CvmFundReportProvider(Settings())
    calls = 0

    async def download_many(
        paths: list[str],
    ) -> tuple[list[bytes], list[tuple[str, APIError]]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [b"valid but unrelated FII archive"], []
        return [], [(paths[0], ProviderUnavailableError())]

    monkeypatch.setattr(provider, "_download_many", download_many)
    monkeypatch.setattr(
        reports_module,
        "parse_fii_reports",
        lambda _payload, _instrument: FundReportSeries(),
    )

    with pytest.raises(ProviderUnavailableError):
        await provider.reports(
            _instrument(InstrumentType.fiagro),
            today=date(2026, 6, 30),
        )


@pytest.mark.asyncio
async def test_provider_exposes_partial_archive_failures_with_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _zip(
        {
            "geral.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Nome_Fundo_Classe;Codigo_ISIN\n"
                "28.757.546/0001-00;2026-06-01;XP MALLS FII;BRXPMLCTF000\n"
            ),
            "complemento.csv": (
                "CNPJ_Fundo_Classe;Data_Referencia;Valor_Patrimonial_Cotas\n"
                "28.757.546/0001-00;2026-06-01;102.50\n"
            ),
        }
    )
    provider = CvmFundReportProvider(Settings())

    async def partial_download(paths: list[str]) -> tuple[list[bytes], list[tuple[str, APIError]]]:
        return [payload], [(paths[-1], ProviderUnavailableError())]

    monkeypatch.setattr(provider, "_download_many", partial_download)

    result = await provider.reports(_instrument(), today=date(2026, 6, 30))

    assert result.reports[0].nav_per_share == Decimal("102.50")
    assert result.archive_failures[-1].path.endswith("inf_mensal_fii_2026.zip")
    assert result.archive_failures[-1].code == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_provider_preserves_unexpected_download_failures_and_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CvmFundReportProvider(Settings())

    async def broken_download(_path: str) -> bytes:
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(provider, "_download", broken_download)
    with pytest.raises(RuntimeError, match="unexpected parser failure"):
        await provider._download_many(["/one.zip"])

    def request_failure(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=_request)

    failing = CvmFundReportProvider(Settings(), httpx.MockTransport(request_failure))
    with pytest.raises(ProviderUnavailableError):
        await failing._request("/archive.zip")
