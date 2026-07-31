"""Indicators for foreign listings must be read exactly as the page states them."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.scrapers.international_listings import (
    SOURCE_INVESTIDOR10,
    InternationalListingProvider,
    parse_international_listing,
)


def _card(label: str, value: str) -> str:
    return (
        f'<div class="_card"><div class="_card-header">{label}</div>'
        f'<div class="_card-body">{value}</div></div>'
    )


def _cell(label: str, value: str) -> str:
    return (
        f'<div class="cell"><span class="name">{label}</span>'
        f'<div class="value"><span>{value}</span></div></div>'
    )


def _page(*fragments: str) -> str:
    return f"<html><body>{''.join(fragments)}</body></html>"


def test_headline_multiples_are_read_from_their_cards() -> None:
    html = _page(_card("p/l", "39,84"), _card("p/vp", "45,86"))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.price_to_earnings == Decimal("39.84")
    assert listing.price_to_book == Decimal("45.86")


def test_the_ticker_is_normalized() -> None:
    listing = parse_international_listing("aapl", _page(_card("p/l", "10")))

    assert listing is not None
    assert listing.ticker == "AAPL"
    assert listing.source == SOURCE_INVESTIDOR10


def test_a_percentage_is_stored_as_a_ratio() -> None:
    listing = parse_international_listing("o", _page(_card("DIVIDEND YIELD", "4,94%")))

    assert listing is not None
    assert listing.dividend_yield == Decimal("0.0494")


def test_the_local_price_is_preferred_over_the_converted_one() -> None:
    """The page prints the listing currency first and a BRL conversion after."""
    html = _page(_card("Cotação", "US$ 331,09 R$ 1.678,63"))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.price == Decimal("331.09")
    assert listing.currency == "USD"


def test_an_exact_figure_wins_over_its_abbreviation() -> None:
    """The page states the same amount twice; the exact one carries precision."""
    html = _page(_cell("Nº total de papeis", "14,67 Bilhões 14.673.000.000"))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.shares_outstanding == Decimal("14673000000")


def test_a_magnitude_word_is_matched_at_its_full_length() -> None:
    """Regression: "mil" is a prefix of "milhoes" and scaled by a thousand."""
    html = _page(_cell("Nº total de papeis", "920,00 Milhões 920.000.000"))

    listing = parse_international_listing("o", html)

    assert listing is not None
    assert listing.shares_outstanding == Decimal("920000000")


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("5,00 Mil", "5000"),
        ("2,50 Milhões", "2500000"),
        ("4,88 Trilhões", "4880000000000"),
        ("1,20 Bilhões", "1200000000"),
    ],
)
def test_each_magnitude_scales_its_amount(written: str, expected: str) -> None:
    html = _page(_cell("Patrimônio Líquido", written))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.equity == Decimal(expected)


def test_balance_sheet_totals_are_read_from_their_rows() -> None:
    html = _page(
        _cell("Patrimônio Líquido", "$ 106,49 Bilhões R$ 106.491.000.000"),
        _cell("Ativos", "$ 371,08 Bilhões R$ 371.082.000.000"),
    )

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.equity == Decimal("106491000000")
    assert listing.total_assets == Decimal("371082000000")


def test_a_page_without_indicators_is_reported_as_absent() -> None:
    """An empty listing would claim the company publishes nothing."""
    assert parse_international_listing("nope", _page("<div>unrelated</div>")) is None


def test_an_unparsable_value_leaves_its_field_absent() -> None:
    html = _page(_card("p/l", "-"), _card("p/vp", "1,50"))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.price_to_earnings is None
    assert listing.price_to_book == Decimal("1.50")


def test_peers_exclude_the_listing_itself() -> None:
    html = _page(
        _card("p/l", "10"),
        '<a href="/stocks/msft/">Microsoft</a>',
        '<a href="/stocks/aapl/">Apple</a>',
        '<a href="/stocks/msft/">Microsoft again</a>',
    )

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.peers == ("MSFT",)


def test_the_sector_is_read_when_the_page_states_it() -> None:
    html = _page(_card("p/l", "10"), _cell("Setor", "Tecnologia"))

    listing = parse_international_listing("aapl", html)

    assert listing is not None
    assert listing.sector == "Tecnologia"


async def test_provider_tries_each_listing_section() -> None:
    """A symbol may be published as a company, a REIT or an ETF."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if "reits" not in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, text=_page(_card("p/vp", "1,51")))

    provider = InternationalListingProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    listing = await provider.listing("O")

    assert listing is not None
    assert listing.price_to_book == Decimal("1.51")
    assert any("stocks" in path for path in requested)


async def test_provider_reports_nothing_when_no_section_publishes_the_symbol() -> None:
    provider = InternationalListingProvider(
        Settings(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )

    assert await provider.listing("NOPE") is None


async def test_provider_reports_nothing_for_a_page_without_indicators() -> None:
    provider = InternationalListingProvider(
        Settings(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=_page("<div>empty</div>"))
        ),
    )

    assert await provider.listing("NOPE") is None
