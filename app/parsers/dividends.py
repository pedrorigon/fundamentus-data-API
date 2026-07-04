from datetime import date

from selectolax.parser import HTMLParser

from app.core.errors import UpstreamInvalidResponseError
from app.models import Dividend, DividendPeriod
from app.parsers.normalizers import clean_text, parse_br_date, parse_br_decimal


def parse_dividends(html: str, ticker: str) -> list[Dividend]:
    if not html or "proventos" not in html.lower():
        raise UpstreamInvalidResponseError(ticker=ticker)

    tree = HTMLParser(html)
    table = tree.css_first("table#resultado")
    if table is None:
        return []

    dividends: list[Dividend] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) < 4:
            continue
        raw_date = clean_text(cells[0].text(separator=" "))
        raw_value = clean_text(cells[1].text(separator=" "))
        raw_type = clean_text(cells[2].text(separator=" "))
        raw_payment = clean_text(cells[3].text(separator=" "))
        raw_ratio = clean_text(cells[4].text(separator=" ")) if len(cells) > 4 else ""
        dividends.append(
            Dividend(
                ex_date=parse_br_date(raw_date),
                payment_date=parse_br_date(raw_payment),
                value=parse_br_decimal(raw_value),
                type=raw_type or None,
                shares_ratio=parse_br_decimal(raw_ratio),
                is_future_payment=False,
                is_future_ex_date=False,
                raw={
                    "date": raw_date or None,
                    "value": raw_value or None,
                    "payment_date": raw_payment or None,
                    "type": raw_type or None,
                    "shares_ratio": raw_ratio or None,
                },
            )
        )
    return dividends


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
