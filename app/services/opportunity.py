from __future__ import annotations

import base64
import json
import math
import re
import unicodedata
from dataclasses import dataclass
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
    FundDistribution,
    FundMonthlyReport,
    FundReportSeries,
    InstrumentMetadata,
    InstrumentType,
    OpportunityMetric,
    OpportunityMetrics,
    OpportunityResponse,
)
from app.parsers.normalizers import clean_text, normalize_ticker, parse_br_decimal
from app.scrapers.cvm_fund_reports import (
    CvmFundReportProvider,
)
from app.scrapers.cvm_fund_reports import (
    FundReportSeries as CvmReportSeries,
)
from app.services.assets import AssetService

SOURCE_FUNDAMENTUS = "fundamentus"
SOURCE_STATUS_INVEST = "status_invest"
SOURCE_B3 = "b3"
SOURCE_CVM = "cvm"

# B3's public instrument files do not consistently carry an underlying symbol
# for older BDR records.  These aliases are intentionally small and explicit;
# an unknown BDR is left unresolved instead of guessing from its local code.
BDR_UNDERLYING_ALIASES: dict[str, str] = {
    "AAPL34": "AAPL",
    "ABUD34": "BIDU",
    "A1AP34": "AAPL",
    "AMZO34": "AMZN",
    "BABA34": "BABA",
    "BIDU34": "BIDU",
    "DISB34": "DIS",
    "GOGL34": "GOOGL",
    "M1TA34": "META",
    "MELI34": "MELI",
    "MSFT34": "MSFT",
    "N1DA34": "NVDA",
    "NFLX34": "NFLX",
    "NVDC34": "NVDA",
    "P2LT34": "PLTR",
    "TSLA34": "TSLA",
}


@dataclass(frozen=True)
class StatusInvestProfile:
    values: dict[str, Decimal]
    cnpj: str | None = None
    distributions: tuple[FundDistribution, ...] = ()


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
        self._cache: dict[
            tuple[str, InstrumentType | None],
            tuple[float, StatusInvestProfile],
        ] = {}

    async def get(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> dict[str, Decimal]:
        return dict((await self.profile(ticker, instrument_type)).values)

    async def profile(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> StatusInvestProfile:
        normalized = _normalized_ticker(ticker).lower()
        cache_key = (normalized, instrument_type)
        cached = self._cache.get(cache_key)
        if cached and cached[0] > monotonic():
            return cached[1]
        paths = _status_paths(instrument_type)
        async with httpx.AsyncClient(
            base_url=self.settings.status_invest_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            follow_redirects=True,
            headers={
                "Accept": "text/html",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Referer": f"{self.settings.status_invest_base_url.rstrip('/')}/",
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
                profile = parse_status_invest_profile(response.text)
                if profile.values or profile.cnpj or profile.distributions:
                    self._cache[cache_key] = (
                        monotonic() + self.settings.opportunity_cache_ttl_seconds,
                        profile,
                    )
                    return profile
        self._cache[cache_key] = (
            monotonic() + self.settings.opportunity_cache_ttl_seconds,
            StatusInvestProfile(values={}),
        )
        return StatusInvestProfile(values={})


class OpportunityService:
    def __init__(
        self,
        asset_service: AssetService,
        settings: Settings,
        *,
        b3_provider: B3InstrumentProvider | None = None,
        status_provider: StatusInvestProvider | None = None,
        cvm_provider: CvmFundReportProvider | None = None,
    ) -> None:
        self.asset_service = asset_service
        self.settings = settings
        self.b3 = b3_provider or B3InstrumentProvider(settings)
        self.status = status_provider or StatusInvestProvider(settings)
        self.cvm = cvm_provider or CvmFundReportProvider(settings)

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

        status_profile = await self.status.profile(
            normalized,
            instrument.instrument_type if instrument else None,
        )
        metrics = _opportunity_metrics(
            details,
            dividends,
            status_profile.values,
            self.settings.bazin_minimum_yield_percent,
        )
        report_series = await self.cvm.reports(
            instrument,
            cnpj=status_profile.cnpj,
        )
        metrics = _merge_official_fund_metrics(metrics, report_series)
        distributions = _merge_fund_distributions(dividends, status_profile.distributions)
        metrics = _add_distribution_metrics(metrics, distributions)
        return OpportunityResponse(
            ticker=normalized,
            instrument=instrument,
            metrics=metrics,
            fund_reports=_report_series(report_series),
            fund_distributions=list(distributions),
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
        instrument_type = _instrument_type(category, description, str(row.get("AsstDesc") or ""))
        underlying = _resolve_underlying(row, ticker, instrument_type)
        identifiers = _identifiers(row)
        return InstrumentMetadata(
            ticker=ticker,
            name=description or None,
            instrument_type=instrument_type,
            category=category,
            cfi_code=_optional(row.get("CFICd")),
            isin=_optional(row.get("ISIN")),
            identifiers=identifiers,
            currency=_optional(row.get("TradgCcy")),
            exchange=_optional(row.get("MktNm") or row.get("Xchg")),
            country=_optional(row.get("CntryNm") or row.get("Country")),
            underlying_ticker=underlying[0],
            underlying_name=underlying[1],
            underlying_exchange=_optional(row.get("UnderlyingExchange") or row.get("UndrlyngXchg")),
            underlying_country=_optional(row.get("UnderlyingCountry") or row.get("UndrlyngCntry")),
            underlying_identifiers=_underlying_identifiers(row),
            underlying_source=underlying[2],
            underlying_unavailable_reason=(
                None
                if underlying[0] is not None
                else (
                    "B3 did not publish an authoritative underlying ticker and no safe alias exists"
                )
                if instrument_type is InstrumentType.bdr
                else None
            ),
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


def resolve_bdr_underlying(ticker: str) -> str | None:
    """Resolve only exact, reviewed aliases for BDRs without metadata."""
    return BDR_UNDERLYING_ALIASES.get(ticker.strip().upper())


def _resolve_underlying(
    row: dict[object, object],
    ticker: str,
    instrument_type: InstrumentType,
) -> tuple[str | None, str | None, str | None]:
    if instrument_type is not InstrumentType.bdr:
        return None, None, None
    for key in (
        "UnderlyingTicker",
        "UnderlyingTckrSymb",
        "UndrlyngTckrSymb",
        "UnderlyingSymbol",
        "ReferenceTicker",
        "ReferenceSymbol",
    ):
        candidate = _underlying_ticker(row.get(key))
        if candidate:
            return candidate, _optional(row.get("UnderlyingName") or row.get("UndrlyngNm")), "b3"
    for row_key, value in row.items():
        key_text = str(row_key).lower()
        if ("underly" in key_text or "reference" in key_text) and (
            "ticker" in key_text or "symbol" in key_text or "tckr" in key_text
        ):
            candidate = _underlying_ticker(value)
            if candidate:
                return (
                    candidate,
                    _optional(row.get("UnderlyingName") or row.get("UndrlyngNm")),
                    "b3",
                )
    alias = resolve_bdr_underlying(ticker)
    return alias, None, "b3_alias" if alias else None


def _underlying_ticker(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if not candidate or len(candidate) > 10:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", candidate):
        return None
    if re.fullmatch(r"[A-Z]{4}\d{1,2}", candidate):
        return None
    return candidate


def _identifiers(row: dict[object, object]) -> dict[str, str]:
    values = {
        "isin": row.get("ISIN"),
        "security_id": row.get("SctyId") or row.get("SecurityID"),
    }
    return {key: str(value).strip() for key, value in values.items() if value not in {None, ""}}


def _underlying_identifiers(row: dict[object, object]) -> dict[str, str]:
    values = {
        "isin": row.get("UnderlyingISIN") or row.get("UndrlyngISIN"),
        "cusip": row.get("UnderlyingCUSIP") or row.get("UndrlyngCUSIP"),
    }
    return {key: str(value).strip() for key, value in values.items() if value not in {None, ""}}


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
    return parse_status_invest_profile(html).values


def parse_status_invest_profile(html: str) -> StatusInvestProfile:
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
    indicator_keys = {
        "p_l": "price_to_earnings",
        "p_vp": "price_to_book",
        "lpa": "earnings_per_share",
        "vpa": "book_value_per_share",
    }
    for node in tree.css("[data-key]"):
        key = indicator_keys.get((node.attributes.get("data-key") or "").lower())
        container = node.parent.parent if node.parent is not None else None
        value_node = container.css_first("strong.value") if container is not None else None
        value = parse_br_decimal(value_node.text() if value_node else None)
        if key is not None and value is not None:
            values.setdefault(key, value)
    cnpj = _status_cnpj(tree)
    distributions = _status_distributions(tree)
    return StatusInvestProfile(
        values=values,
        cnpj=cnpj,
        distributions=distributions,
    )


def _status_cnpj(tree: HTMLParser) -> str | None:
    for node in tree.css("h3.title, strong"):
        if _fold(node.text()) != "CNPJ":
            continue
        container = node.parent
        value_node = (
            container.css_first("strong.value, .span-item") if container is not None else None
        )
        digits = (
            "".join(character for character in value_node.text() if character.isdigit())
            if value_node
            else ""
        )
        if len(digits) == 14:
            return digits
    return None


def _status_distributions(tree: HTMLParser) -> tuple[FundDistribution, ...]:
    node = tree.css_first("#earning-section input#results")
    raw = node.attributes.get("value") if node is not None else None
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    distributions = []
    for item in payload:
        if not isinstance(item, dict) or _fold(str(item.get("et") or "")) != "RENDIMENTO":
            continue
        ex_date = _br_date(item.get("ed"))
        value = _decimal_value(item.get("v"))
        if ex_date is not None and value is not None and value >= 0:
            distributions.append(
                FundDistribution(
                    ex_date=ex_date,
                    value=value,
                    source=SOURCE_STATUS_INVEST,
                )
            )
    return tuple(sorted(distributions, key=lambda item: item.ex_date, reverse=True))


def _br_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def _decimal_value(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (ValueError, ArithmeticError):
        return None


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
    book_value, book_value_source = _prefer(
        details.book_value_per_share if details else None,
        status.get("book_value_per_share"),
    )
    earnings, earnings_source = _prefer(
        details.earnings_per_share if details else None,
        status.get("earnings_per_share"),
    )
    price_to_book, price_to_book_source = _prefer(
        fields.get("p_vp"),
        status.get("price_to_book"),
    )
    if (
        price_to_book is None
        and current_price is not None
        and book_value is not None
        and book_value != 0
    ):
        price_to_book = current_price / book_value
        price_to_book_source = price_source or book_value_source
    price_to_earnings, price_to_earnings_source = _prefer(
        fields.get("p_l"),
        status.get("price_to_earnings"),
    )
    if (
        price_to_earnings is None
        and current_price is not None
        and earnings is not None
        and earnings != 0
    ):
        price_to_earnings = current_price / earnings
        price_to_earnings_source = price_source or earnings_source

    cutoff = as_of - timedelta(days=365)
    dividend_total = Decimal("0")
    for item in dividends:
        event_date = item.ex_date or item.payment_date
        if event_date is not None and event_date >= cutoff:
            dividend_total += item.value or Decimal("0")
    dividend_source = SOURCE_FUNDAMENTUS
    if dividend_total <= 0:
        status_dividends = status.get("dividends_12m")
        if status_dividends is not None:
            dividend_total = status_dividends
            dividend_source = SOURCE_STATUS_INVEST
    dividend_total_value = (
        dividend_total
        if dividend_total > 0 or details is not None or "dividends_12m" in status
        else None
    )
    reported_dividend_yield = fields.get("div_yield") or status.get("dividend_yield_12m")
    dividend_yield = reported_dividend_yield
    if dividend_total_value is not None and current_price:
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
        shares_outstanding=_metric(
            details.shares_count if details else None,
            as_of=as_of,
            sources=[SOURCE_FUNDAMENTUS],
            reason="Outstanding shares unavailable",
        ),
        earnings_per_share=_metric(
            earnings,
            as_of=as_of,
            sources=[earnings_source] if earnings_source else [],
            reason="Earnings per share unavailable",
        ),
        book_value_per_share=_metric(
            book_value,
            as_of=as_of,
            sources=[book_value_source] if book_value_source else [],
            reason="Book value per share unavailable",
        ),
        price_to_book=_metric(
            price_to_book,
            as_of=as_of,
            sources=[price_to_book_source] if price_to_book_source else [],
            reason="Book value per share unavailable",
        ),
        price_to_earnings=_metric(
            price_to_earnings,
            as_of=as_of,
            sources=[price_to_earnings_source] if price_to_earnings_source else [],
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
        average_daily_traded_value=_metric(
            details.average_daily_volume_2m if details else None,
            as_of=as_of,
            sources=[SOURCE_FUNDAMENTUS],
            reason="Average daily traded value unavailable",
        ),
        market_capitalization=_metric(
            details.market_value if details else None,
            as_of=as_of,
            sources=[SOURCE_FUNDAMENTUS],
            reason="Market capitalization unavailable",
        ),
    )


def _merge_official_fund_metrics(
    metrics: OpportunityMetrics,
    series: CvmReportSeries,
) -> OpportunityMetrics:
    if not series.reports:
        return metrics
    latest = max(series.reports, key=lambda item: item.as_of)
    current_price = metrics.current_price.value
    price_to_book = (
        current_price / latest.nav_per_share
        if current_price is not None and current_price > 0
        else None
    )
    return metrics.model_copy(
        update={
            "book_value_per_share": _metric(
                latest.nav_per_share,
                as_of=latest.as_of,
                sources=[SOURCE_CVM],
                reason="Book value per share unavailable",
            ),
            "price_to_book": _metric(
                price_to_book,
                as_of=latest.as_of,
                sources=[SOURCE_CVM, *metrics.current_price.sources],
                reason="Current price unavailable",
            ),
        }
    )


def _merge_fund_distributions(
    dividends: list[Dividend],
    status_distributions: tuple[FundDistribution, ...],
) -> tuple[FundDistribution, ...]:
    by_date: dict[date, FundDistribution] = {}
    for distribution in status_distributions:
        by_date[distribution.ex_date] = distribution
    for dividend in dividends:
        event_date = dividend.ex_date or dividend.payment_date
        if (
            event_date is None
            or dividend.value is None
            or dividend.value < 0
            or "AMORT" in _fold(dividend.type)
        ):
            continue
        by_date[event_date] = FundDistribution(
            ex_date=event_date,
            value=dividend.value,
            source=SOURCE_FUNDAMENTUS,
        )
    return tuple(by_date[key] for key in sorted(by_date, reverse=True))


def _add_distribution_metrics(
    metrics: OpportunityMetrics,
    distributions: tuple[FundDistribution, ...],
) -> OpportunityMetrics:
    if not distributions:
        return metrics
    latest = distributions[0]
    return metrics.model_copy(
        update={
            "latest_distribution": _metric(
                latest.value,
                as_of=latest.ex_date,
                sources=[latest.source],
                reason="Latest distribution unavailable",
            ),
            "median_distribution_3m": _distribution_median(distributions, 3),
            "median_distribution_6m": _distribution_median(distributions, 6),
        }
    )


def _distribution_median(
    distributions: tuple[FundDistribution, ...],
    months: int,
) -> OpportunityMetric:
    selected = distributions[:months]
    value = _median(tuple(item.value for item in selected)) if len(selected) >= months else None
    return _metric(
        value,
        as_of=max((item.ex_date for item in selected), default=None),
        sources=sorted({item.source for item in selected}),
        reason=f"At least {months} monthly distributions are required",
    )


def _median(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _report_series(series: CvmReportSeries) -> FundReportSeries | None:
    if not series.reports:
        return None
    return FundReportSeries(
        cnpj=series.cnpj,
        reports=[
            FundMonthlyReport(
                as_of=item.as_of,
                nav_per_share=item.nav_per_share,
                monthly_distribution_yield=item.monthly_distribution_yield,
                monthly_nav_return=item.monthly_nav_return,
                monthly_effective_return=item.monthly_effective_return,
                net_assets=item.net_assets,
                issued_shares=item.issued_shares,
                shareholder_count=item.shareholder_count,
                administration_fee_ratio=item.administration_fee_ratio,
                total_assets=item.total_assets,
                total_liabilities=item.total_liabilities,
                property_assets=item.property_assets,
                credit_assets=item.credit_assets,
                liquid_assets=item.liquid_assets,
                inception_date=item.inception_date,
                segment=item.segment,
                administrator=item.administrator,
            )
            for item in series.reports
        ],
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
