"""Export annual company fundamentals from CVM DFP archives for offline calibration.

The records are derived by the same parser and accounting rules that serve the
fundamentals endpoint, so a calibrated coefficient is estimated on exactly the
quantities production later observes.  Cash returned to shareholders comes from
the direct children of the financing section (``6.03``) of the cash-flow
statement, identified by their standardized labels.

Usage::

    python scripts/export_company_years.py --data-dir ~/.cache/smartinvest-assessment-v4
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.parsers.cvm_statements import (
    StatementPeriod,
    parse_company_registry,
    parse_statement_archive,
)
from app.services.fundamentals_math import build_period

_FINANCING_PREFIX = "6.03."
_DIVIDEND_TOKENS = ("DIVIDEND", "JUROS SOBRE", "JCP")
_BUYBACK_TOKENS = ("TESOURARIA", "RECOMPRA", "AQUISICAO DE ACOES", "ACOES PROPRIAS")
_ISSUANCE_TOKENS = ("AUMENTO DE CAPITAL", "EMISSAO DE ACOES", "INTEGRALIZACAO", "SUBSCRICAO")


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def financing_flows(statement: StatementPeriod) -> dict[str, Decimal] | None:
    """Dividends and interest on capital paid, buybacks and share issuance.

    Only direct children of ``6.03`` are read so a subtotal is never added to
    its own components.  ``None`` means the filing has none of these lines.
    """

    flows = {"dividends_paid": Decimal("0"), "buybacks": Decimal("0"), "issuance": Decimal("0")}
    found = False
    for code, label in statement.account_labels.items():
        value = statement.accounts.get(code)
        if value is None or not code.startswith(_FINANCING_PREFIX) or code.count(".") != 2:
            continue
        folded = _fold(label)
        if any(token in folded for token in _DIVIDEND_TOKENS):
            flows["dividends_paid"] += max(-value, Decimal("0"))
        elif any(token in folded for token in _BUYBACK_TOKENS):
            flows["buybacks"] += max(-value, Decimal("0"))
        elif any(token in folded for token in _ISSUANCE_TOKENS):
            flows["issuance"] += max(value, Decimal("0"))
        else:
            continue
        found = True
    return flows if found else None


def company_years(
    archives: Iterable[tuple[int, bytes]],
    registry_payload: bytes,
) -> list[dict[str, object]]:
    """Annual records, one per company and fiscal year."""

    registry = parse_company_registry(registry_payload)
    records: list[dict[str, object]] = []
    for fiscal_year, payload in archives:
        for cnpj, statements in parse_statement_archive(payload).items():
            registration = registry.get(cnpj)
            sector = registration.sector if registration else None
            for statement in statements:
                if statement.period_end.year != fiscal_year:
                    continue
                period = build_period(statement, sector=sector)
                if not period.annual:
                    continue
                record = {
                    key: _json_value(value)
                    for key, value in period.model_dump(
                        exclude={"source", "source_url", "shares_outstanding"}
                    ).items()
                }
                flows = financing_flows(statement) or {}
                record.update(
                    cnpj=cnpj,
                    cvm_code=statement.cvm_code,
                    company=statement.company_name,
                    sector=sector,
                    fiscal_year=fiscal_year,
                    **{key: str(value) for key, value in flows.items()},
                )
                records.append(record)
    return records


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser()
    archives = sorted((data_dir / "dfp").glob("dfp_cia_aberta_*.zip"))
    records = company_years(
        ((int(path.stem.rsplit("_", 1)[1]), path.read_bytes()) for path in archives),
        (data_dir / "cvm" / "cad_cia_aberta.csv").read_bytes(),
    )
    output = args.output or data_dir / "company_years.json"
    output.write_text(json.dumps(records), encoding="utf-8")
    print(f"{len(records)} company-years written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
