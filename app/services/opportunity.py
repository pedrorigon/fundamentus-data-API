from __future__ import annotations

import base64
import math
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import monotonic

import httpx
from selectolax.parser import HTMLParser

from app.config import Settings
from app.core.errors import APIError, InvalidTickerError
from app.models import (
    AssetDetails,
    Dividend,
    InstrumentMetadata,
    InstrumentType,
    OpportunityMetric,
    OpportunityMetrics,
    OpportunityResponse,
)
from app.parsers.normalizers import clean_text, normalize_ticker, parse_br_decimal
from app.services.assets import AssetService

SOURCE_FUNDAMENTUS = "fundamentus"
SOURCE_STATUS_INVEST = "status_invest"
SOURCE_B3 = "b3"


def _fold(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def _metric(
    value: Decimal | None,
    *,
    as_of: date | None,
    sources: list[str],
    reason: str,
) -> OpportunityMetric:
    return OpportunityMetric(
        value=value,
        as_of=as_of,
        sources=sources if value is not None else [],
        unavailable_reason=None if value is not None else reason,
    )


class B3InstrumentProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: dict[str, tuple[float, InstrumentMetadata | None]] = {}

    async def get(self, ticker: str) -> InstrumentMetadata | None:
        normalized = _normalized_ticker(ticker)
        cached = self._cache.get(normalized)
        if cached and cached[0] > monotonic():
            return cached[1]
        encoded = base64.b64encode(normalized.encode()).decode()
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.settings.b3_bdi_base_url,
            timeout=timeout,
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            for days_ago in range(1, 8):
                reference = datetime.now(UTC).date() - timedelta(days=days_ago)
                try:
                    response = await client.post(
                        f"/table/InstrumentsEquities/{reference}/{reference}/1/20",
                        params={"filter": encoded},
                        json={},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                result = _instrument_from_b3(payload, normalized)
                if result is not None:
                    self._cache[normalized] = (
                        monotonic() + self.settings.opportunity_cache_ttl_seconds,
                        result,
                    )
                    return result
        self._cache[normalized] = (
            monotonic() + self.settings.opportunity_cache_ttl_seconds,
            None,
        )
        return None


class StatusInvestProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: dict[tuple[str, InstrumentType | None], tuple[float, dict[str, Decimal]]] = {}

    async def get(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> dict[str, Decimal]:
        normalized = _normalized_ticker(ticker).lower()
        cache_key = (normalized, instrument_type)
        cached = self._cache.get(cache_key)
        if cached and cached[0] > monotonic():
            return dict(cached[1])
        paths = _status_paths(instrument_type)
        async with httpx.AsyncClient(
            base_url=self.settings.status_invest_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            follow_redirects=True,
            headers={
                "Accept": "text/html",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "User-Agent": "Mozilla/5.0",
            },
        ) as client:
            for path in paths:
                try:
                    response = await client.get(f"/{path}/{normalized}")
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                values = parse_status_invest_snapshot(response.text)
                if values:
                    self._cache[cache_key] = (
                        monotonic() + self.settings.opportunity_cache_ttl_seconds,
                        values,
                    )
                    return values
        self._cache[cache_key] = (
            monotonic() + self.settings.opportunity_cache_ttl_seconds,
            {},
        )
        return {}


class OpportunityService:
    def __init__(
        self,
        asset_service: AssetService,
        settings: Settings,
        *,
        b3_provider: B3InstrumentProvider | None = None,
        status_provider: StatusInvestProvider | None = None,
    ) -> None:
        self.asset_service = asset_service
        self.settings = settings
        self.b3 = b3_provider or B3InstrumentProvider(settings)
        self.status = status_provider or StatusInvestProvider(settings)

    async def instrument(self, ticker: str) -> InstrumentMetadata | None:
        return await self.b3.get(ticker)

    async def opportunity(self, ticker: str) -> OpportunityResponse:
        normalized = _normalized_ticker(ticker)
        instrument = await self.b3.get(normalized)
        details: AssetDetails | None = None
        dividends = []
        try:
            asset = await self.asset_service.get_asset(normalized)
            details = asset.details
            dividends = asset.dividends or []
        except APIError:
            pass

        status_values = await self.status.get(
            normalized,
            instrument.instrument_type if instrument else None,
        )
        metrics = _opportunity_metrics(
            details,
            dividends,
            status_values,
            self.settings.bazin_minimum_yield_percent,
        )
        return OpportunityResponse(
            ticker=normalized,
            instrument=instrument,
            metrics=metrics,
            refreshed_at=datetime.now(UTC),
        )


def _normalized_ticker(ticker: str) -> str:
    try:
        return normalize_ticker(ticker)
    except ValueError as exc:
        raise InvalidTickerError(ticker=ticker) from exc


def _instrument_from_b3(payload: object, ticker: str) -> InstrumentMetadata | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("table"), dict):
        return None
    table = payload["table"]
    columns = table.get("columns")
    values = table.get("values")
    if not isinstance(columns, list) or not isinstance(values, list):
        return None
    names = [column.get("name") for column in columns if isinstance(column, dict)]
    if not names:
        return None
    for raw_row in values:
        if not isinstance(raw_row, list):
            continue
        row = dict(zip(names, raw_row, strict=False))
        if row.get("TckrSymb") != ticker or row.get("SgmtNm") != "CASH":
            continue
        description = clean_text(str(row.get("CrpnNm") or row.get("AsstDesc") or ""))
        category = clean_text(str(row.get("SctyCtgyNm") or "")) or None
        return InstrumentMetadata(
            ticker=ticker,
            name=description or None,
            instrument_type=_instrument_type(category, description, str(row.get("AsstDesc") or "")),
            category=category,
            cfi_code=_optional(row.get("CFICd")),
            isin=_optional(row.get("ISIN")),
            currency=_optional(row.get("TradgCcy")),
            reference_date=_iso_date(row.get("RptDt")),
        )
    return None


def _instrument_type(
    category: str | None,
    description: str,
    asset_description: str,
) -> InstrumentType:
    text = _fold(f"{category or ''} {description} {asset_description}")
    if "FI INFRA" in text or "FI-INFRA" in text:
        return InstrumentType.fi_infra
    if "FIAGRO" in text or "FI AGRO" in text:
        return InstrumentType.fiagro
    if "FUNDO DE INDICE" in text or " ETF " in f" {text} ":
        return InstrumentType.etf
    if " BDR" in f" {text}" or " DRN" in f" {text}" or " DRE" in f" {text}":
        return InstrumentType.bdr
    if "FUNDS" in text or "FII" in text:
        return InstrumentType.fii if "IMOB" in text or " FII" in f" {text}" else InstrumentType.fund
    if " UNT" in f" {text}" or "UNIT" in text:
        return InstrumentType.unit
    if any(token in text for token in ("COMMON EQUITIES", "PREFERRED EQUITIES", " ON", " PN")):
        return InstrumentType.stock
    return InstrumentType.unknown


def _status_paths(instrument_type: InstrumentType | None) -> tuple[str, ...]:
    mapping: dict[InstrumentType, tuple[str, ...]] = {
        InstrumentType.fi_infra: ("fiinfras",),
        InstrumentType.fiagro: ("fiagros",),
        InstrumentType.fii: ("fundos-imobiliarios",),
        InstrumentType.stock: ("acoes",),
        InstrumentType.unit: ("acoes",),
    }
    if instrument_type is not None and instrument_type in mapping:
        return mapping[instrument_type]
    return ("acoes", "fundos-imobiliarios", "fiagros", "fiinfras")


def parse_status_invest_snapshot(html: str) -> dict[str, Decimal]:
    tree = HTMLParser(html)
    values: dict[str, Decimal] = {}
    titles = {
        "VALOR ATUAL DO ATIVO": "current_price",
        "VALOR MINIMO DAS ULTIMAS 52 SEMANAS": "min_52_weeks",
        "VALOR MAXIMO DAS ULTIMAS 52 SEMANAS": "max_52_weeks",
        "DIVIDEND YIELD COM BASE NOS ULTIMOS 12 MESES": "dividend_yield_12m",
        "SOMA TOTAL DE PROVENTOS DISTRIBUIDOS NOS ULTIMOS 12 MESES": "dividends_12m",
    }
    for node in tree.css("[title]"):
        key = titles.get(_fold(node.attributes.get("title")))
        if key is None:
            continue
        value_node = node.css_first("strong.value") or node.css_first("span.sub-value")
        value = parse_br_decimal(value_node.text() if value_node else None)
        if value is not None:
            values[key] = value
    return values


def _opportunity_metrics(
    details: AssetDetails | None,
    dividends: list[Dividend],
    status: dict[str, Decimal],
    bazin_yield: Decimal,
) -> OpportunityMetrics:
    as_of = details.quote_date if details and details.quote_date else datetime.now(UTC).date()
    fields = _detail_fields(details)
    current_price, price_source = _prefer(
        details.quote if details else None,
        status.get("current_price"),
    )
    book_value = details.book_value_per_share if details else None
    earnings = details.earnings_per_share if details else None
    price_to_book = fields.get("p_vp")
    if (
        price_to_book is None
        and current_price is not None
        and book_value is not None
        and book_value != 0
    ):
        price_to_book = current_price / book_value
    price_to_earnings = fields.get("p_l")
    if (
        price_to_earnings is None
        and current_price is not None
        and earnings is not None
        and earnings != 0
    ):
        price_to_earnings = current_price / earnings

    cutoff = as_of - timedelta(days=365)
    dividend_total = Decimal("0")
    for item in dividends:
        event_date = item.ex_date or item.payment_date
        if event_date is not None and event_date >= cutoff:
            dividend_total += item.value or Decimal("0")
    dividend_source = SOURCE_FUNDAMENTUS if dividend_total > 0 else SOURCE_STATUS_INVEST
    if dividend_total <= 0:
        dividend_total = status.get("dividends_12m") or Decimal("0")
    dividend_total_value = dividend_total if dividend_total > 0 else None
    dividend_yield = fields.get("div_yield")
    if dividend_yield is None:
        dividend_yield = status.get("dividend_yield_12m")
    if dividend_yield is None and dividend_total_value is not None and current_price:
        dividend_yield = dividend_total_value / current_price * Decimal("100")

    graham = None
    if earnings is not None and earnings > 0 and book_value is not None and book_value > 0:
        graham = Decimal(str(math.sqrt(float(Decimal("22.5") * earnings * book_value))))
    bazin = (
        dividend_total_value / (bazin_yield / Decimal("100"))
        if dividend_total_value is not None and bazin_yield > 0
        else None
    )
    min_52, min_source = _prefer(
        details.min_52_weeks if details else None,
        status.get("min_52_weeks"),
    )
    max_52, max_source = _prefer(
        details.max_52_weeks if details else None,
        status.get("max_52_weeks"),
    )
    fundamental_source = [SOURCE_FUNDAMENTUS]
    return OpportunityMetrics(
        current_price=_metric(
            current_price,
            as_of=as_of,
            sources=[price_source] if price_source else [],
            reason="Current price unavailable",
        ),
        price_to_book=_metric(
            price_to_book,
            as_of=as_of,
            sources=fundamental_source,
            reason="Book value per share unavailable",
        ),
        price_to_earnings=_metric(
            price_to_earnings,
            as_of=as_of,
            sources=fundamental_source,
            reason="Earnings per share unavailable",
        ),
        dividend_yield_12m=_metric(
            dividend_yield,
            as_of=as_of,
            sources=[dividend_source],
            reason="Trailing dividends unavailable",
        ),
        dividends_12m=_metric(
            dividend_total_value,
            as_of=as_of,
            sources=[dividend_source],
            reason="Trailing dividends unavailable",
        ),
        graham_price=_metric(
            graham,
            as_of=as_of,
            sources=fundamental_source,
            reason="Positive earnings and book value are required",
        ),
        bazin_price=_metric(
            bazin,
            as_of=as_of,
            sources=[dividend_source],
            reason="Trailing dividends unavailable",
        ),
        min_52_weeks=_metric(
            min_52,
            as_of=as_of,
            sources=[min_source] if min_source else [],
            reason="52-week minimum unavailable",
        ),
        max_52_weeks=_metric(
            max_52,
            as_of=as_of,
            sources=[max_source] if max_source else [],
            reason="52-week maximum unavailable",
        ),
    )


def _detail_fields(details: AssetDetails | None) -> dict[str, Decimal]:
    if details is None:
        return {}
    result: dict[str, Decimal] = {}
    for section in details.sections:
        for field in section.fields:
            if isinstance(field.value, Decimal):
                result.setdefault(field.key_normalized, field.value)
    return result


def _prefer(primary: Decimal | None, fallback: Decimal | None) -> tuple[Decimal | None, str | None]:
    if primary is not None:
        return primary, SOURCE_FUNDAMENTUS
    if fallback is not None:
        return fallback, SOURCE_STATUS_INVEST
    return None, None


def _optional(value: object) -> str | None:
    text = clean_text(str(value)) if value is not None else ""
    return text or None


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
