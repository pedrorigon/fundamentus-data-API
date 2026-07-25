from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

from app.parsers.cvm_statements import (
    ACCOUNT_CASH,
    ACCOUNT_CURRENT_DEBT,
    ACCOUNT_EBIT,
    ACCOUNT_EQUITY,
    ACCOUNT_LONG_TERM_DEBT,
    ACCOUNT_NET_INCOME,
    ACCOUNT_OPERATING_CASH_FLOW,
    ACCOUNT_REVENUE,
    parse_company_registry,
    parse_share_capital,
    parse_statement_archive,
)

STATEMENT_HEADER = (
    "CNPJ_CIA;DT_REFER;DT_RECEB;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA;"
    "ORDEM_EXERC;DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA"
)
CNPJ = "11.111.111/0001-11"
CNPJ_DIGITS = "11111111000111"


def statement_row(
    code: str,
    value: str,
    *,
    label: str = "Conta",
    order: str = "ÚLTIMO",
    scale: str = "MIL",
    start: str = "2024-01-01",
    end: str = "2024-12-31",
    cnpj: str = CNPJ,
    name: str = "EMPRESA TESTE S.A.",
    received: str = "2025-03-26",
) -> str:
    return (
        f"{cnpj};2024-12-31;{received};1;{name};001;DF Consolidado;REAL;{scale};"
        f"{order};{start};{end};{code};{label};{value};S"
    )


def build_archive(
    rows: list[str],
    *,
    filename: str = "dfp_cia_aberta_DRE_con_2024.csv",
    extra: dict[str, list[str]] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            filename,
            "\n".join([STATEMENT_HEADER, *rows]).encode("latin-1"),
        )
        for name, extra_rows in (extra or {}).items():
            archive.writestr(name, "\n".join(extra_rows).encode("latin-1"))
    return buffer.getvalue()


def test_parses_accounts_and_applies_thousand_scale() -> None:
    payload = build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "1000.0000000000", label="Receita Líquida"),
            statement_row(ACCOUNT_EBIT, "250.0000000000"),
        ]
    )

    periods = parse_statement_archive(payload)

    assert list(periods) == [CNPJ_DIGITS]
    period = periods[CNPJ_DIGITS][0]
    assert period.account(ACCOUNT_REVENUE) == Decimal("1000000")
    assert period.account_by_label("Receita Líquida") == Decimal("1000000")
    assert period.account(ACCOUNT_EBIT) == Decimal("250000")
    assert period.period_end == date(2024, 12, 31)
    assert period.published_at == date(2025, 3, 26)
    assert period.consolidated is True


def test_joins_publication_date_from_document_metadata() -> None:
    metadata_header = "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;DT_RECEB"
    payload = build_archive(
        [statement_row(ACCOUNT_REVENUE, "1000", received="")],
        extra={
            "dfp_cia_aberta_2024.csv": [
                metadata_header,
                f"{CNPJ};2024-12-31;1;EMPRESA TESTE S.A.;001;2025-03-28",
            ]
        },
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.published_at == date(2025, 3, 28)


def test_period_start_is_completed_by_flow_statement_rows() -> None:
    payload = build_archive(
        [statement_row(ACCOUNT_EQUITY, "900", start="", end="2024-03-31")],
        filename="itr_cia_aberta_BPA_con_2024.csv",
        extra={
            "itr_cia_aberta_DRE_con_2024.csv": [
                STATEMENT_HEADER,
                statement_row(ACCOUNT_REVENUE, "250", start="2024-01-01", end="2024-03-31"),
            ]
        },
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.period_start == date(2024, 1, 1)


def test_unit_scale_is_not_multiplied() -> None:
    payload = build_archive([statement_row(ACCOUNT_REVENUE, "1500.00", scale="UNIDADE")])

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_REVENUE) == Decimal("1500.00")


def test_per_share_accounts_use_the_cvm_thousandths_convention() -> None:
    payload = build_archive(
        [
            statement_row("3.99.01.01", "2780.0000000000", label="ON"),
            statement_row("3.99.01.02", "2.7800000000", label="PN"),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account("3.99.01.01") == Decimal("2.7800000000")
    assert period.account("3.99.01.02") == Decimal("2.7800000000")


def test_ignores_previous_period_rows() -> None:
    payload = build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "999.0000000000", order="PENÚLTIMO"),
            statement_row(ACCOUNT_REVENUE, "1000.0000000000"),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_REVENUE) == Decimal("1000000")


def test_filters_by_requested_cnpj() -> None:
    payload = build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "1000.0000000000"),
            statement_row(ACCOUNT_REVENUE, "2000.0000000000", cnpj="22.222.222/0001-22"),
        ]
    )

    periods = parse_statement_archive(payload, cnpjs={CNPJ_DIGITS})

    assert list(periods) == [CNPJ_DIGITS]


def test_prefers_consolidated_over_individual_statements() -> None:
    consolidated = build_archive(
        [statement_row(ACCOUNT_REVENUE, "1000.0000000000")],
        extra={
            "dfp_cia_aberta_DRE_ind_2024.csv": [
                STATEMENT_HEADER,
                statement_row(ACCOUNT_REVENUE, "400.0000000000"),
            ]
        },
    )

    period = parse_statement_archive(consolidated)[CNPJ_DIGITS][0]

    assert period.consolidated is True
    assert period.account(ACCOUNT_REVENUE) == Decimal("1000000")


def test_falls_back_to_individual_when_consolidated_absent() -> None:
    payload = build_archive(
        [statement_row(ACCOUNT_REVENUE, "400.0000000000")],
        filename="dfp_cia_aberta_DRE_ind_2024.csv",
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.consolidated is False
    assert period.account(ACCOUNT_REVENUE) == Decimal("400000")


def test_captures_depreciation_by_label() -> None:
    payload = build_archive(
        [
            statement_row(
                "6.01.01.02",
                "-75.0000000000",
                label="Depreciação e Amortização",
            )
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.depreciation == Decimal("75000")


def test_ignores_depreciation_outside_cash_flow_statement() -> None:
    payload = build_archive([statement_row("3.02.01", "-75.0000000000", label="Depreciação")])

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.depreciation is None


def test_skips_malformed_and_unparsable_rows() -> None:
    payload = build_archive(
        [
            "broken;row",
            statement_row(ACCOUNT_REVENUE, "not-a-number"),
            statement_row("", "100.00"),
            statement_row(ACCOUNT_EQUITY, "500.0000000000"),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_EQUITY) == Decimal("500000")
    assert period.account(ACCOUNT_REVENUE) is None


def test_returns_empty_for_invalid_archive() -> None:
    assert parse_statement_archive(b"not a zip") == {}
    assert parse_share_capital(b"not a zip") == {}


def test_parses_share_capital_and_excludes_treasury_shares() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_ORDIN_TESOURO;"
        "QT_ACAO_PREF_TESOURO;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2024.csv": [
                header,
                f"{CNPJ};2024-12-31;1;EMPRESA TESTE S.A.;700;300;1000;0;0;100",
            ]
        },
    )

    shares = parse_share_capital(payload)

    assert shares[CNPJ_DIGITS].total_shares == Decimal("1000")
    assert shares[CNPJ_DIGITS].outstanding_shares == Decimal("900")


def test_share_capital_uses_reported_total_when_classes_are_absent() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2019.csv": [
                header,
                f"{CNPJ};2019-12-31;1;EMPRESA TESTE S.A.;;;1250;50",
            ]
        },
    )

    shares = parse_share_capital(payload)[CNPJ_DIGITS]

    assert shares.total_shares == Decimal("1250")
    assert shares.outstanding_shares == Decimal("1200")


def test_share_capital_keeps_most_recent_reference() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_ORDIN_TESOURO;"
        "QT_ACAO_PREF_TESOURO;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2024.csv": [
                header,
                f"{CNPJ};2023-12-31;1;EMPRESA TESTE S.A.;500;0;500;0;0;0",
                f"{CNPJ};2024-12-31;1;EMPRESA TESTE S.A.;800;0;800;0;0;0",
            ]
        },
    )

    shares = parse_share_capital(payload)

    assert shares[CNPJ_DIGITS].reference_date == date(2024, 12, 31)
    assert shares[CNPJ_DIGITS].total_shares == Decimal("800")


def test_treasury_shares_above_issued_falls_back_to_total() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_ORDIN_TESOURO;"
        "QT_ACAO_PREF_TESOURO;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2024.csv": [
                header,
                f"{CNPJ};2024-12-31;1;EMPRESA TESTE S.A.;100;0;100;0;0;500",
            ]
        },
    )

    assert parse_share_capital(payload)[CNPJ_DIGITS].outstanding_shares == Decimal("100")


def test_parses_company_registry() -> None:
    payload = (
        "CNPJ_CIA;DENOM_SOCIAL;DENOM_COMERC;CD_CVM;SETOR_ATIV;SIT\n"
        f"{CNPJ};EMPRESA TESTE S.A.;TESTE;001;Comércio;ATIVO\n"
        ";;;;;\n"
    ).encode("latin-1")

    registry = parse_company_registry(payload)

    assert registry[CNPJ_DIGITS].sector == "Comércio"
    assert registry[CNPJ_DIGITS].status == "ATIVO"


def test_full_period_exposes_every_mapped_account() -> None:
    payload = build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "1000.0000000000"),
            statement_row(ACCOUNT_EBIT, "250.0000000000"),
            statement_row(ACCOUNT_NET_INCOME, "150.0000000000"),
            statement_row(ACCOUNT_EQUITY, "900.0000000000"),
            statement_row(ACCOUNT_CASH, "80.0000000000"),
            statement_row(ACCOUNT_CURRENT_DEBT, "60.0000000000"),
            statement_row(ACCOUNT_LONG_TERM_DEBT, "140.0000000000"),
            statement_row(ACCOUNT_OPERATING_CASH_FLOW, "300.0000000000"),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_NET_INCOME) == Decimal("150000")
    assert period.account(ACCOUNT_CURRENT_DEBT) == Decimal("60000")
    assert period.account(ACCOUNT_OPERATING_CASH_FLOW) == Decimal("300000")


def test_skips_auditor_opinion_and_capital_files_when_collecting_accounts() -> None:
    payload = build_archive(
        [statement_row(ACCOUNT_REVENUE, "1000.0000000000")],
        extra={
            "dfp_cia_aberta_parecer_2024.csv": [
                STATEMENT_HEADER,
                statement_row(ACCOUNT_REVENUE, "9999.0000000000"),
            ]
        },
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_REVENUE) == Decimal("1000000")


def test_ignores_files_that_are_neither_consolidated_nor_individual() -> None:
    payload = build_archive(
        [statement_row(ACCOUNT_REVENUE, "1000.0000000000")],
        filename="dfp_cia_aberta_2024.csv",
    )

    assert parse_statement_archive(payload) == {}


def test_skips_rows_with_unparsable_dates() -> None:
    payload = build_archive(
        [
            statement_row(ACCOUNT_REVENUE, "1000.0000000000", end="31/12/2024"),
            statement_row(ACCOUNT_EQUITY, "500.0000000000"),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account(ACCOUNT_EQUITY) == Decimal("500000")


def test_share_capital_ignores_rows_without_reference_date() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_ORDIN_TESOURO;"
        "QT_ACAO_PREF_TESOURO;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2024.csv": [
                header,
                f"{CNPJ};invalid;1;EMPRESA TESTE S.A.;700;300;1000;0;0;0",
            ]
        },
    )

    assert parse_share_capital(payload) == {}


def test_share_capital_filters_by_requested_cnpj() -> None:
    header = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;QT_ACAO_ORDIN_CAP_INTEGR;"
        "QT_ACAO_PREF_CAP_INTEGR;QT_ACAO_TOTAL_CAP_INTEGR;QT_ACAO_ORDIN_TESOURO;"
        "QT_ACAO_PREF_TESOURO;QT_ACAO_TOTAL_TESOURO"
    )
    payload = build_archive(
        [],
        extra={
            "dfp_cia_aberta_composicao_capital_2024.csv": [
                header,
                "22.222.222/0001-22;2024-12-31;1;OUTRA;700;300;1000;0;0;0",
            ]
        },
    )

    assert parse_share_capital(payload, cnpjs={CNPJ_DIGITS}) == {}


def test_empty_csv_file_yields_no_rows() -> None:
    payload = build_archive([], extra={"dfp_cia_aberta_DRE_con_2023.csv": []})

    assert parse_statement_archive(payload) == {}


def test_registry_ignores_empty_payload() -> None:
    assert parse_company_registry(b"") == {}


def test_a_label_repeated_in_another_statement_does_not_answer_for_it() -> None:
    """The value added statement repeats income statement wording.

    Without confining the lookup to one statement group, the profit attributed
    to controlling shareholders can be read from the wrong statement.
    """
    payload = build_archive(
        [
            statement_row(
                "3.11.01",
                "10301606.0000000000",
                label="Atribuído a Sócios da Empresa Controladora",
            ),
            statement_row(
                "4.03.01",
                "0.0000000000",
                label="Atribuído a Sócios da Empresa Controladora",
            ),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account_by_label(
        "Atribuído a Sócios da Empresa Controladora",
        prefix="3.",
    ) == Decimal("10301606000")


def test_labels_are_tried_in_the_order_they_are_given() -> None:
    payload = build_archive(
        [
            statement_row("3.11.02", "0.0000000000", label="Atribuído a Sócios Não Controladores"),
            statement_row(
                "3.11.01", "500.0000000000", label="Atribuído a Sócios da Empresa Controladora"
            ),
        ]
    )

    period = parse_statement_archive(payload)[CNPJ_DIGITS][0]

    assert period.account_by_label(
        "Atribuído a Sócios da Empresa Controladora",
        "Atribuído a Sócios Não Controladores",
    ) == Decimal("500000")
