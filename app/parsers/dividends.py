from datetime import date

from selectolax.parser import HTMLParser, Node

from app.core.errors import UpstreamInvalidResponseError
from app.models import Dividend, DividendPeriod
from app.parsers.normalizers import clean_text, normalize_key, parse_br_date, parse_br_decimal

_HEADER_COLUMNS = {
    "data": "date",
    "ultima_data_com": "date",
    "valor": "value",
    "tipo": "type",
    "data_de_pagamento": "payment_date",
    "por_quantas_acoes": "shares_ratio",
}
_DEFAULT_COLUMNS = ("date", "value", "type", "payment_date", "shares_ratio")


def _table_columns(table: Node) -> tuple[str, ...]:
    """Resolve column order from the table header.

    The stock page (proventos.php) and the FII page (fii_proventos.php) list the
    same data in different column orders, so the header labels are the only
    reliable mapping. Falls back to the stock layout when headers are missing.
    """
    headers = [normalize_key(cell.text(separator=" ")) for cell in table.css("thead th")]
    columns = tuple(_HEADER_COLUMNS.get(header, "") for header in headers)
    if "date" in columns and "value" in columns:
        return columns
    return _DEFAULT_COLUMNS


def _dividend_from_row(columns: tuple[str, ...], cells: list[Node]) -> Dividend:
    raw: dict[str, str] = {}
    for column, cell in zip(columns, cells, strict=False):
        if column:
            raw[column] = clean_text(cell.text(separator=" "))
    return Dividend(
        ex_date=parse_br_date(raw.get("date")),
        payment_date=parse_br_date(raw.get("payment_date")),
        value=parse_br_decimal(raw.get("value")),
        type=raw.get("type") or None,
        shares_ratio=parse_br_decimal(raw.get("shares_ratio")),
        is_future_payment=False,
        is_future_ex_date=False,
        raw={
            "date": raw.get("date") or None,
            "value": raw.get("value") or None,
            "payment_date": raw.get("payment_date") or None,
            "type": raw.get("type") or None,
            "shares_ratio": raw.get("shares_ratio") or None,
        },
    )


def parse_dividends(html: str, ticker: str) -> list[Dividend]:
    if not html or "proventos" not in html.lower():
        raise UpstreamInvalidResponseError(ticker=ticker)

    tree = HTMLParser(html)
    table = tree.css_first("table#resultado")
    if table is None:
        return []

    columns = _table_columns(table)
    return [
        _dividend_from_row(columns, cells)
        for row in table.css("tbody tr")
        if len(cells := row.css("td")) >= 4
    ]


def classify_dividends(dividends: list[Dividend], as_of: date) -> list[Dividend]:
    classified: list[Dividend] = []
    for dividend in dividends:
        data = dividend.model_copy(
            update={
                "is_future_payment": bool(dividend.payment_date and dividend.payment_date > as_of),
                "is_future_ex_date": bool(dividend.ex_date and dividend.ex_date > as_of),
            }
        )
        classified.append(data)
    return classified


def filter_dividends(
    dividends: list[Dividend],
    period: DividendPeriod,
    as_of: date,
) -> list[Dividend]:
    classified = classify_dividends(dividends, as_of)
    if period == DividendPeriod.all:
        return classified
    if period == DividendPeriod.past:
        return [
            item
            for item in classified
            if item.payment_date is not None and item.payment_date <= as_of
        ]
    if period == DividendPeriod.future:
        return [item for item in classified if item.is_future_payment]
    if period == DividendPeriod.upcoming_ex_date:
        return [item for item in classified if item.is_future_ex_date]
    return classified
