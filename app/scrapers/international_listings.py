"""Public indicators for internationally listed companies, REITs and ETFs.

Brazilian issuers file with the CVM, which is the source of every fundamental
this API publishes for them. Foreign issuers have no equivalent bulk filing
source, so their indicators are read from the public pages that already publish
them, in the same way the Fundamentus and Status Invest pages are read for
Brazilian tickers. No API key is involved, which keeps a self-hosted deployment
working without per-user registration.

Only values the page actually states are mapped. An indicator the page omits
stays absent so the consumer can redistribute its weight instead of scoring a
guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from selectolax.parser import HTMLParser

from app.config import Settings

SOURCE_INVESTIDOR10 = "investidor10"

# Section of the site that publishes each kind of listing.
_STOCK_PATH = "stocks"
_REIT_PATH = "reits"
_ETF_PATH = "etfs-global"

# Multipliers written next to a magnitude on the page.
_MAGNITUDES: dict[str, Decimal] = {
    "mil": Decimal("1000"),
    "milhao": Decimal("1000000"),
    "milhoes": Decimal("1000000"),
    "bilhao": Decimal("1000000000"),
    "bilhoes": Decimal("1000000000"),
    "trilhao": Decimal("1000000000000"),
    "trilhoes": Decimal("1000000000000"),
}

# Headline cards, keyed by the label the page prints above the value.
_CARD_FIELDS: dict[str, str] = {
    "p/l": "price_to_earnings",
    "p/vp": "price_to_book",
    "dividend yield": "dividend_yield",
    "dy": "dividend_yield",
}

# Rows of the indicators table.
_CELL_FIELDS: dict[str, str] = {
    "valor de mercado": "market_capitalization",
    "capitalizacao": "market_capitalization",
    "patrimonio liquido": "equity",
    "ativos": "total_assets",
    "no total de papeis": "shares_outstanding",
    "volume medio de negociacoes diaria": "average_daily_traded_value",
}


@dataclass(frozen=True)
class InternationalListing:
    """Indicators published for one foreign listing."""

    ticker: str
    currency: str | None = None
    price: Decimal | None = None
    price_to_earnings: Decimal | None = None
    price_to_book: Decimal | None = None
    dividend_yield: Decimal | None = None
    market_capitalization: Decimal | None = None
    equity: Decimal | None = None
    total_assets: Decimal | None = None
    shares_outstanding: Decimal | None = None
    average_daily_traded_value: Decimal | None = None
    sector: str | None = None
    peers: tuple[str, ...] = field(default_factory=tuple)
    source: str = SOURCE_INVESTIDOR10


class InternationalListingProvider:
    """Reads the public indicator page of a foreign listing."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def listing(self, ticker: str) -> InternationalListing | None:
        """Indicators for one ticker, or ``None`` when no page publishes it.

        A symbol can be listed as a company, a REIT or an ETF, and the page
        that describes it differs, so each section is tried in turn.
        """
        for path in (_STOCK_PATH, _REIT_PATH, _ETF_PATH):
            document = await self._page(path, ticker)
            if document is None:
                continue
            listing = parse_international_listing(ticker, document)
            if listing is not None:
                return listing
        return None

    async def _page(self, path: str, ticker: str) -> str | None:
        async with httpx.AsyncClient(
            base_url=self.settings.investidor10_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
        ) as client:
            response = await client.get(f"/{path}/{ticker.lower()}/")
        if response.status_code != 200:
            return None
        return response.text


def parse_international_listing(ticker: str, html: str) -> InternationalListing | None:
    """Read the indicators a listing page states.

    A page that states no usable indicator is reported as absent rather than as
    a listing with every field empty.
    """
    document = HTMLParser(html)
    values: dict[str, Decimal] = {}
    values.update(_card_values(document))
    values.update(_cell_values(document))
    price, currency = _quotation(document)
    if not values and price is None:
        return None
    return InternationalListing(
        ticker=ticker.upper(),
        currency=currency,
        price=price,
        price_to_earnings=values.get("price_to_earnings"),
        price_to_book=values.get("price_to_book"),
        dividend_yield=values.get("dividend_yield"),
        market_capitalization=values.get("market_capitalization"),
        equity=values.get("equity"),
        total_assets=values.get("total_assets"),
        shares_outstanding=values.get("shares_outstanding"),
        average_daily_traded_value=values.get("average_daily_traded_value"),
        sector=_sector(document),
        peers=_peers(document, ticker),
    )


def _card_values(document: HTMLParser) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for card in document.css("div._card"):
        header = card.css_first("div._card-header")
        body = card.css_first("div._card-body")
        if header is None or body is None:
            continue
        field_name = _CARD_FIELDS.get(_key(header.text()))
        parsed = _decimal(body.text())
        if field_name is not None and parsed is not None:
            values[field_name] = _as_ratio(field_name, parsed)
    return values


def _cell_values(document: HTMLParser) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for cell in document.css("div.cell"):
        name = cell.css_first("span.d-flex, span.name, .title")
        value = cell.css_first("div.value span, span.value, .detail-value")
        if name is None or value is None:
            continue
        field_name = _CELL_FIELDS.get(_key(name.text()))
        parsed = _magnitude_decimal(value.text())
        if field_name is not None and parsed is not None:
            values[field_name] = parsed
    return values


def _quotation(document: HTMLParser) -> tuple[Decimal | None, str | None]:
    """Traded price and the currency it is quoted in.

    The page prints the local price first and a converted one after it, so the
    first amount is the one stated in the listing's own currency.
    """
    for card in document.css("div._card"):
        header = card.css_first("div._card-header")
        body = card.css_first("div._card-body")
        if header is None or body is None:
            continue
        if _key(header.text()) not in {"cotacao", "valor atual"}:
            continue
        text = " ".join(body.text().split())
        currency = "USD" if "US$" in text else None
        return _decimal(text.split("R$")[0]), currency
    return None, None


def _sector(document: HTMLParser) -> str | None:
    for cell in document.css("div.cell"):
        name = cell.css_first("span.d-flex, span.name, .title")
        value = cell.css_first("div.value span, span.value, .detail-value")
        if name is None or value is None or _key(name.text()) != "setor":
            continue
        sector = " ".join(value.text().split())
        return sector or None
    return None


def _peers(document: HTMLParser, ticker: str) -> tuple[str, ...]:
    """Comparable listings the page names, excluding the listing itself."""
    symbols = []
    for link in document.css("a[href*='/stocks/'], a[href*='/reits/']"):
        href = link.attributes.get("href") or ""
        candidate = href.rstrip("/").rsplit("/", 1)[-1].upper()
        if candidate and candidate != ticker.upper() and candidate.isalnum():
            symbols.append(candidate)
    return tuple(dict.fromkeys(symbols))


def _as_ratio(field_name: str, value: Decimal) -> Decimal:
    """Percentages are printed as whole numbers and stored as ratios."""
    return value / Decimal("100") if field_name == "dividend_yield" else value


def _key(label: str) -> str:
    """Normalize a printed label so accents and spacing do not matter."""
    folded = unicodedata.normalize("NFKD", " ".join(label.split()))
    return folded.encode("ascii", "ignore").decode("ascii").strip().lower()


def _decimal(text: str) -> Decimal | None:
    """First number in a Brazilian-formatted string."""
    match = re.search(r"-?\d[\d.]*(?:,\d+)?", text or "")
    if match is None:
        return None
    try:
        return Decimal(match.group().replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _magnitude_decimal(text: str) -> Decimal | None:
    """Amount written as an abbreviation, an exact figure, or both.

    The page prints "920,00 Milhões 920.000.000", stating the same amount twice.
    The exact figure carries the full precision, so it wins when both appear and
    they describe the same amount; the abbreviation is scaled only when it is
    the only form given.
    """
    normalized = " ".join((text or "").split())
    numbers = re.findall(r"-?\d[\d.]*(?:,\d+)?", normalized)
    if not numbers:
        return None
    scaled = _scaled_first(numbers[0], normalized)
    for candidate in numbers[1:]:
        exact = _as_decimal(candidate)
        if exact is not None and scaled is not None and _same_amount(exact, scaled):
            return exact
    return scaled


def _scaled_first(number: str, normalized: str) -> Decimal | None:
    base = _as_decimal(number)
    if base is None:
        return None
    key = _key(normalized)
    # "mil" is a prefix of "milhoes", so the longest match is the intended one.
    for word in sorted(_MAGNITUDES, key=len, reverse=True):
        if word in key:
            return base * _MAGNITUDES[word]
    return base


def _same_amount(left: Decimal, right: Decimal) -> bool:
    """Whether two readings of the same cell agree within rounding."""
    if right == 0:
        return left == 0
    return abs(left - right) / abs(right) < Decimal("0.01")


def _as_decimal(number: str) -> Decimal | None:
    try:
        return Decimal(number.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None
