from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.services.bcb_quality import BcbBankProvider, BcbMacroProvider


@pytest.mark.asyncio
async def test_macro_provider_compounds_ipca_and_averages_annualized_selic() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "bcdata.sgs.433" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"data": "31/01/2025", "valor": "1,00"},
                    {"data": "28/02/2025", "valor": "2,00"},
                    {"data": "invalid", "valor": "3,00"},
                    {"data": "31/03/2025", "valor": "invalid"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"data": "31/01/2025", "valor": "10,00"},
                {"data": "28/02/2025", "valor": "12,00"},
            ],
        )

    provider = BcbMacroProvider(Settings(), httpx.MockTransport(handler))

    first = await provider.snapshot(date(2026, 7, 30))
    second = await provider.snapshot(date(2026, 7, 30))

    assert first is second
    assert first.inflation_by_year[2025] == Decimal("0.0302")
    assert first.selic_by_year[2025] == Decimal("0.11")
    assert first.as_of == date(2025, 2, 28)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_macro_provider_returns_empty_snapshot_for_invalid_upstreams() -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, content=b"not-json"),
        ]
    )
    provider = BcbMacroProvider(
        Settings(),
        httpx.MockTransport(lambda _request: next(responses)),
    )

    result = await provider.snapshot(date(2026, 7, 30))

    assert result.inflation_by_year == {}
    assert result.selic_by_year == {}
    assert result.as_of is None


@pytest.mark.asyncio
async def test_bank_provider_resolves_regulatory_capital_and_credit_risk() -> None:
    calls: list[httpx.Request] = []
    registrations = [
        {
            "NomeInstituicao": "ITAU UNIBANCO HOLDING S.A.",
            "CodConglomeradoPrudencial": "C0080099",
            "CodInst": "I123",
            "Td": "I",
        },
        {
            "NomeInstituicao": "CONGLOMERADO ITAU",
            "CodConglomeradoPrudencial": "C0080099",
            "CodInst": "C0010069",
            "Td": "C",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if "IfDataCadastro" in path:
            return httpx.Response(200, json={"value": registrations})
        if "Relatorio='5'" in path:
            assert "+" not in str(request.url)
            assert request.url.params["$filter"] == "CodInst eq 'C0080099'"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"Conta": "79664", "Saldo": "0.147697"},
                        {"Conta": "79659", "Saldo": "0.119663"},
                        {"Conta": "79661", "Saldo": "0.064760"},
                    ]
                },
            )
        if "AnoMes=202412" in path:
            assert request.url.params["$filter"] == "CodInst eq 'C0010069'"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"NomeColuna": "E", "Saldo": "10"},
                        {"NomeColuna": "F", "Saldo": "12"},
                        {"NomeColuna": "G", "Saldo": "6"},
                        {"NomeColuna": "H", "Saldo": "4"},
                        {"NomeColuna": "Total Geral", "Saldo": "1000"},
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})

    provider = BcbBankProvider(Settings(), httpx.MockTransport(handler))

    first = await provider.snapshot("Itaú Unibanco Holding S.A.", date(2026, 7, 30))
    second = await provider.snapshot("Itaú Unibanco Holding S.A.", date(2026, 7, 30))

    assert first is second
    assert first.institution_name == "ITAU UNIBANCO HOLDING S.A."
    assert first.basel_ratio == Decimal("0.147697")
    assert first.core_capital_ratio == Decimal("0.119663")
    assert first.leverage_ratio == Decimal("0.064760")
    assert first.high_risk_credit_ratio == Decimal("0.032")
    assert first.capital_as_of == date(2026, 3, 1)
    assert first.credit_as_of == date(2024, 12, 1)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_bank_provider_handles_missing_matches_and_malformed_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "IfDataCadastro" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "NomeInstituicao": "OUTRA INSTITUICAO",
                            "CodConglomeradoPrudencial": "C0000001",
                            "CodInst": "I1",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"unexpected": []})

    provider = BcbBankProvider(Settings(), httpx.MockTransport(handler))

    missing = await provider.snapshot("Completely Different Bank", date(2026, 1, 15))

    assert missing.basel_ratio is None
    assert missing.capital_as_of is None

    failing = BcbBankProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    assert (await failing.snapshot("Bank", date(2026, 7, 30))).institution_name is None


@pytest.mark.asyncio
async def test_bank_provider_falls_back_across_credit_quarters_and_rejects_bad_balances() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "IfDataCadastro" in path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "NomeInstituicao": "BANCO TESTE S.A.",
                            "CodConglomeradoPrudencial": "P1",
                            "CodInst": "F1",
                            "Td": "I",
                        }
                    ]
                },
            )
        if "Relatorio='5'" in path:
            return httpx.Response(
                200,
                json={"value": [{"Conta": "79664", "Saldo": "not-a-number"}]},
            )
        if "AnoMes=202412" in path:
            return httpx.Response(200, json={"value": []})
        if "AnoMes=202409" in path:
            return httpx.Response(
                200,
                json={"value": [{"NomeColuna": "Total Geral", "Saldo": "0"}]},
            )
        return httpx.Response(200, json={"value": []})

    result = await BcbBankProvider(
        Settings(),
        httpx.MockTransport(handler),
    ).snapshot("Banco Teste", date(2026, 7, 30))

    assert result.basel_ratio is None
    assert result.high_risk_credit_ratio is None
    assert result.credit_as_of == date(2024, 9, 1)


@pytest.mark.asyncio
async def test_bank_provider_expands_cvm_abbreviation_before_matching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "IfDataCadastro" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "NomeInstituicao": "BCO KDB BRASIL S.A.",
                            "CodConglomeradoPrudencial": "KDB",
                            "CodInst": "KDB1",
                            "Td": "I",
                        },
                        {
                            "NomeInstituicao": "BANCO DO BRASIL S.A.",
                            "CodConglomeradoPrudencial": "BB",
                            "CodInst": "BB1",
                            "Td": "I",
                        },
                    ]
                },
            )
        if "Relatorio='5'" in request.url.path:
            assert request.url.params["$filter"] == "CodInst eq 'BB'"
            return httpx.Response(
                200,
                json={"value": [{"Conta": "79664", "Saldo": "0.165"}]},
            )
        return httpx.Response(200, json={"value": []})

    result = await BcbBankProvider(
        Settings(),
        httpx.MockTransport(handler),
    ).snapshot("BCO BRASIL S.A.", date(2026, 7, 30))

    assert result.institution_name == "BANCO DO BRASIL S.A."
    assert result.basel_ratio == Decimal("0.165")


@pytest.mark.asyncio
async def test_bank_provider_retries_transient_capital_failures() -> None:
    capital_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal capital_attempts
        if "IfDataCadastro" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "NomeInstituicao": "BANCO RETRY S.A.",
                            "CodConglomeradoPrudencial": "RETRY",
                            "CodInst": "R1",
                            "Td": "I",
                        }
                    ]
                },
            )
        if "Relatorio='5'" in request.url.path:
            capital_attempts += 1
            if capital_attempts == 1:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={"value": [{"Conta": "79664", "Saldo": "0.18"}]},
            )
        return httpx.Response(200, json={"value": []})

    result = await BcbBankProvider(
        Settings(retry_backoff_seconds=0),
        httpx.MockTransport(handler),
    ).snapshot("Banco Retry", date(2026, 7, 30))

    assert capital_attempts == 2
    assert result.basel_ratio == Decimal("0.18")
