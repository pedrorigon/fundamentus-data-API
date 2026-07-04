from datetime import date
from decimal import Decimal

import pytest

from app.parsers.normalizers import normalize_key, normalize_ticker, parse_br_date, parse_br_decimal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.268.860.000", Decimal("1268860000")),
        ("42,74", Decimal("42.74")),
        ("-3,80%", Decimal("-3.80")),
        ("R$ 0,1044", Decimal("0.1044")),
        ("-", None),
        ("", None),
    ],
)
def test_parse_br_decimal(raw: str, expected: Decimal | None) -> None:
    assert parse_br_decimal(raw) == expected


def test_parse_br_date() -> None:
    assert parse_br_date("03/07/2026") == date(2026, 7, 3)
    assert parse_br_date("-") is None


def test_normalize_key_and_ticker() -> None:
    assert normalize_key("Últ balanço processado") == "ult_balanco_processado"
    assert normalize_ticker(" itub4 ") == "ITUB4"
    with pytest.raises(ValueError):
        normalize_ticker("bad ticker")
