from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from app.parsers.cvm_statements import ACCOUNT_EBIT, ACCOUNT_NET_INCOME, ACCOUNT_REVENUE
from app.tests.test_cvm_statements import CNPJ_DIGITS, build_archive, statement_row

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_company_years.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_company_years", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = (
    "CNPJ_CIA;DENOM_SOCIAL;DENOM_COMERC;CD_CVM;SETOR_ATIV;SIT\n"
    "11.111.111/0001-11;EMPRESA TESTE S.A.;TESTE;001;Energia Elétrica;ATIVO\n"
).encode("latin-1")


def _archive() -> bytes:
    return build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "1000", label="Receita"),
            statement_row(ACCOUNT_EBIT, "300", label="EBIT"),
            statement_row(ACCOUNT_NET_INCOME, "200", label="Lucro/Prejuízo Consolidado do Período"),
            statement_row("6.03.01", "-80", label="Dividendos Pagos"),
            statement_row("6.03.02", "-20", label="Juros sobre o Capital Próprio Pagos"),
            statement_row("6.03.03", "-15", label="Aquisição de Ações em Tesouraria"),
            statement_row("6.03.04", "50", label="Aumento de Capital"),
            statement_row("6.03.05", "-70", label="Amortização de Empréstimos"),
            statement_row("6.03.01.01", "-999", label="Dividendos Pagos a Controladores"),
            statement_row(ACCOUNT_REVENUE, "900", label="Receita", order="PENÚLTIMO"),
        ],
        filename="dfp_cia_aberta_DFC_MI_con_2024.csv",
    )


def test_exports_annual_fundamentals_with_cash_returned_to_shareholders() -> None:
    records = _module().company_years([(2024, _archive())], REGISTRY)

    assert len(records) == 1
    record = records[0]
    assert record["cnpj"] == CNPJ_DIGITS
    assert record["fiscal_year"] == 2024
    assert record["sector"] == "Energia Elétrica"
    assert Decimal(str(record["revenue"])) == Decimal("1000000")
    assert Decimal(str(record["ebit"])) == Decimal("300000")
    # Only direct children of 6.03 count, in thousands scaled to units.
    assert Decimal(str(record["dividends_paid"])) == Decimal("100000")
    assert Decimal(str(record["buybacks"])) == Decimal("15000")
    assert Decimal(str(record["issuance"])) == Decimal("50000")
    assert record["published_at"] == "2025-03-26"
    assert "shares_outstanding" not in record


def test_skips_periods_of_another_fiscal_year_and_filings_without_payouts() -> None:
    archive = build_archive([statement_row(ACCOUNT_REVENUE, "1000", label="Receita")])

    assert _module().company_years([(2023, archive)], REGISTRY) == []
    record = _module().company_years([(2024, archive)], REGISTRY)[0]
    assert "dividends_paid" not in record


def test_main_writes_the_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dfp").mkdir()
    (tmp_path / "cvm").mkdir()
    (tmp_path / "dfp" / "dfp_cia_aberta_2024.zip").write_bytes(_archive())
    (tmp_path / "cvm" / "cad_cia_aberta.csv").write_bytes(REGISTRY)
    monkeypatch.setattr(sys, "argv", ["export_company_years.py", "--data-dir", str(tmp_path)])

    assert _module().main() == 0

    exported = json.loads((tmp_path / "company_years.json").read_text())
    assert [record["cnpj"] for record in exported] == [CNPJ_DIGITS]
