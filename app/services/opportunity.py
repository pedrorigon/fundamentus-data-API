from __future__ import annotations

import asyncio
import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from time import monotonic

import httpx
from selectolax.parser import HTMLParser

from app.config import Settings
from app.core.errors import (
    APIError,
    InvalidTickerError,
    ProviderInvalidResponseError,
    ProviderUnavailableError,
)
from app.domain.evidence import (
    ConsensusResult,
    ConsensusStatus,
    SourceObservation,
    resolve_consensus,
)
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
    OpportunityObservation,
    OpportunityResponse,
)
from app.models.assets import FundDistributionEvidence
from app.parsers.normalizers import clean_text, normalize_ticker, parse_br_decimal
from app.parsers.status_invest import status_invest_cnpj
from app.scrapers.cvm_fund_reports import (
    CvmFundReportProvider,
)
from app.scrapers.cvm_fund_reports import (
    FundReportSeries as CvmReportSeries,
)
from app.services.assets import AssetService
from app.services.bounded_cache import BoundedTTLCache
from app.services.market_routing import should_query_b3

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


@dataclass(frozen=True)
class _ReconciledDistribution:
    """One dated distribution after source reconciliation.

    The public ``FundDistribution`` contract predates source voting and only
    carries one value/source pair.  Keep the full consensus result internally
    so derived metrics never consume an observation that lost the vote (for
    example, a newest but conflicting event).
    """

    ex_date: date
    consensus: ConsensusResult


def _fold(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def _metric(
    value: Decimal | None,
    *,
    as_of: date | None,
    sources: list[str],
    reason: str,
    unit: str | None = None,
    consensus: ConsensusResult | None = None,
) -> OpportunityMetric:
    if consensus is not None:
        return OpportunityMetric(
            value=consensus.value,
            as_of=consensus.as_of,
            unit=unit,
            sources=list(consensus.sources),
            independent_sources=list(consensus.independent_sources),
            source_lineage=list(consensus.source_lineage),
            observations=_public_observations(consensus.observations),
            rejected_observations=_public_observations(consensus.rejected_observations),
            consensus_status=consensus.status.value,
            confidence=consensus.confidence,
            unavailable_reason=(
                None if consensus.value is not None else consensus.reason or reason
            ),
        )
    return OpportunityMetric(
        value=value,
        as_of=as_of,
        unit=unit,
        sources=sources if value is not None else [],
        consensus_status=(
            ConsensusStatus.single_source.value
            if value is not None
            else ConsensusStatus.missing_data.value
        ),
        confidence=Decimal("0.55") if value is not None else Decimal("0"),
        unavailable_reason=None if value is not None else reason,
    )


def _public_observations(
    observations: tuple[SourceObservation, ...],
) -> list[OpportunityObservation]:
    """Serialize normalized evidence without exposing provider payloads."""

    return [
        OpportunityObservation(
            value=observation.value,
            source=observation.source,
            as_of=observation.as_of,
            unit=observation.unit,
            source_lineage=list(observation.source_lineage),
            independent_origin=observation.independent_origin,
        )
        for observation in observations
    ]


class B3InstrumentProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: BoundedTTLCache[str, InstrumentMetadata | None] = BoundedTTLCache(
            settings.ticker_cache_max_entries
        )

    async def get(self, ticker: str) -> InstrumentMetadata | None:
        normalized = _normalized_ticker(ticker)
        found, cached = self._cache.get(normalized, monotonic())
        if found:
            return cached
        if not should_query_b3(normalized):
            # A foreign symbol can never appear in B3's bulletin, and the
            # session walk below would spend seven sequential requests proving
            # it. Cache the negative answer and route elsewhere.
            self._cache.set(
                normalized,
                monotonic() + self.settings.opportunity_cache_ttl_seconds,
                None,
            )
            return None
        encoded = base64.b64encode(normalized.encode()).decode()
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.settings.b3_bdi_base_url,
            timeout=timeout,
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            invalid_response = False
            unavailable = False
            for days_ago in range(1, 8):
                reference = datetime.now(UTC).date() - timedelta(days=days_ago)
                try:
                    response = await client.post(
                        f"/table/InstrumentsEquities/{reference}/{reference}/1/20",
                        params={"filter": encoded},
                        json={},
                    )
                except httpx.RequestError:
                    unavailable = True
                    continue
                if response.status_code == 404:
                    continue
                if not 200 <= response.status_code < 300:
                    unavailable = True
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    invalid_response = True
                    continue
                if not _is_valid_b3_payload(payload):
                    invalid_response = True
                    continue
                result = _instrument_from_b3(payload, normalized)
                if result is not None:
                    self._cache.set(
                        normalized,
                        monotonic() + self.settings.opportunity_cache_ttl_seconds,
                        result,
                    )
                    return result
            # A negative result is cacheable only when every attempted source
            # path completed successfully.  A valid empty bulletin followed by
            # a transient failure is not evidence that the instrument is
            # absent; surface the retryable error instead of suppressing future
            # lookups behind the negative cache.
            if unavailable:
                raise ProviderUnavailableError(ticker=normalized)
            if invalid_response:
                raise ProviderInvalidResponseError(ticker=normalized)
        self._cache.set(
            normalized,
            monotonic() + self.settings.opportunity_cache_ttl_seconds,
            None,
        )
        return None

    def cached(self, tickers: list[str]) -> list[InstrumentMetadata]:
        """Return resolved metadata without putting upstream I/O on an import path."""
        now = monotonic()
        instruments: list[InstrumentMetadata] = []
        for ticker in tickers:
            normalized = _normalized_ticker(ticker)
            found, cached = self._cache.get(normalized, now)
            if found and cached is not None:
                instruments.append(cached)
        return instruments


class StatusInvestProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: BoundedTTLCache[tuple[str, InstrumentType | None], StatusInvestProfile] = (
            BoundedTTLCache(settings.ticker_cache_max_entries)
        )

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
        found, cached = self._cache.get(cache_key, monotonic())
        if found and cached is not None:
            return cached
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
            invalid_response = False
            unavailable = False
            for path in paths:
                try:
                    response = await client.get(f"/{path}/{normalized}")
                except httpx.RequestError:
                    unavailable = True
                    continue
                if response.status_code == 404:
                    continue
                if not 200 <= response.status_code < 300:
                    unavailable = True
                    continue
                if not response.text.strip():
                    invalid_response = True
                    continue
                profile = parse_status_invest_profile(response.text)
                if profile.values or profile.cnpj or profile.distributions:
                    self._cache.set(
                        cache_key,
                        monotonic() + self.settings.opportunity_cache_ttl_seconds,
                        profile,
                    )
                    return profile
            # Do not persist an empty aggregate while any alternate path was
            # unavailable or malformed.  The next request must be able to
            # retry those paths and recover a profile that was not visible in
            # this partial response set.
            if unavailable:
                raise ProviderUnavailableError(ticker=normalized)
            if invalid_response:
                raise ProviderInvalidResponseError(ticker=normalized)
        self._cache.set(
            cache_key,
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

    async def instruments(self, tickers: list[str]) -> list[InstrumentMetadata]:
        """Resolve a batch from the bounded cache without delaying file imports."""
        return self.b3.cached(tickers)

    async def opportunity(self, ticker: str) -> OpportunityResponse:
        normalized = _normalized_ticker(ticker)
        source_failures: dict[str, str] = {}
        b3_task = asyncio.create_task(self.b3.get(normalized))
        asset_task = asyncio.create_task(self.asset_service.get_asset(normalized))
        source_tasks = (b3_task, asset_task)
        try:
            instrument_result, asset_result = await asyncio.gather(
                *source_tasks,
                return_exceptions=True,
            )
        finally:
            for task in source_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*source_tasks, return_exceptions=True)

        instrument: InstrumentMetadata | None
        if isinstance(instrument_result, APIError):
            error = instrument_result
            if isinstance(error, InvalidTickerError):
                raise error
            # B3 identity enriches the response but is not the only source of
            # valuation evidence. Keep Fundamentus/StatusInvest observations
            # usable when the identity bulletin is temporarily unavailable.
            instrument = None
            source_failures[SOURCE_B3] = error.code
        elif isinstance(instrument_result, BaseException):
            raise instrument_result
        else:
            instrument = instrument_result

        details: AssetDetails | None
        dividends: list[Dividend]
        if isinstance(asset_result, APIError):
            details = None
            dividends = []
            source_failures[SOURCE_FUNDAMENTUS] = asset_result.code
        elif isinstance(asset_result, BaseException):
            raise asset_result
        else:
            asset = asset_result
            details = asset.details
            dividends = asset.dividends or []

        try:
            status_profile = await self.status.profile(
                normalized,
                instrument.instrument_type if instrument else None,
            )
        except APIError as error:
            # StatusInvest is an optional observation source.  Its failure
            # must not prevent the Fundamentus/CVM observations from being
            # returned; the metric resolver records the remaining source
            # count explicitly.
            status_profile = StatusInvestProfile(values={})
            source_failures[SOURCE_STATUS_INVEST] = error.code
        metrics = _opportunity_metrics(
            details,
            dividends,
            status_profile.values,
            self.settings.bazin_minimum_yield_percent,
        )
        try:
            report_series = await self.cvm.reports(
                instrument,
                cnpj=status_profile.cnpj,
            )
        except APIError as error:
            report_series = CvmReportSeries()
            source_failures[SOURCE_CVM] = error.code
        else:
            # A fund report can be partially populated when one or more CVM
            # archives fail while another archive succeeds. Preserve each
            # bounded path/code pair so callers can distinguish degraded
            # provenance from a complete CVM outage.
            source_failures.update(
                {
                    f"{SOURCE_CVM}:{failure.path}": failure.code
                    for failure in report_series.archive_failures
                }
            )
        metrics = _merge_official_fund_metrics(metrics, report_series)
        reconciled_distributions = _reconcile_fund_distributions(
            dividends,
            status_profile.distributions,
        )
        distributions = _public_fund_distributions(reconciled_distributions)
        distribution_evidence = _public_fund_distribution_evidence(reconciled_distributions)
        metrics = _add_distribution_metrics(metrics, reconciled_distributions)
        return OpportunityResponse(
            ticker=normalized,
            instrument=instrument,
            metrics=metrics,
            fund_reports=_report_series(report_series),
            fund_distributions=list(distributions),
            fund_distribution_evidence=list(distribution_evidence),
            source_failures=source_failures,
            refreshed_at=datetime.now(UTC),
        )


def _normalized_ticker(ticker: str) -> str:
    try:
        return normalize_ticker(ticker)
    except ValueError as exc:
        raise InvalidTickerError(ticker=ticker) from exc


def _is_valid_b3_payload(payload: object) -> bool:
    """Return whether a B3 bulletin has the tabular envelope we consume."""

    # The bulletin occasionally answers with an empty JSON object when the
    # requested session has no rows. Treat that as a confirmed empty result;
    # malformed non-empty envelopes remain typed schema failures.
    if payload == {}:
        return True
    if not isinstance(payload, dict) or not isinstance(payload.get("table"), dict):
        return False
    table = payload["table"]
    columns = table.get("columns")
    values = table.get("values")
    if not isinstance(columns, list) or not isinstance(values, list):
        return False
    return bool(columns) and all(
        isinstance(column, dict) and isinstance(column.get("name"), str) and column["name"]
        for column in columns
    )


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
    cnpj = status_invest_cnpj(tree)
    distributions = _status_distributions(tree)
    return StatusInvestProfile(
        values=values,
        cnpj=cnpj,
        distributions=distributions,
    )


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


def _observation(
    value: Decimal | None,
    *,
    source: str,
    as_of: date | None,
    unit: str,
    lineage: tuple[str, ...] = (),
    independent_origin: str | None = None,
) -> SourceObservation | None:
    if value is None or not value.is_finite():
        return None
    return SourceObservation(
        value=value,
        source=source,
        as_of=as_of,
        unit=unit,
        source_lineage=lineage,
        independent_origin=independent_origin,
    )


def _resolved_metric(
    observations: list[SourceObservation],
    *,
    as_of: date | None,
    unit: str,
    reason: str,
    valid_range: tuple[Decimal, Decimal] | None = None,
) -> OpportunityMetric:
    result = resolve_consensus(
        observations,
        expected_unit=unit,
        valid_range=valid_range,
    )
    if result.value is None and result.status is ConsensusStatus.missing_data:
        result = result.model_copy(update={"reason": reason})
    return _metric(
        result.value,
        as_of=result.as_of or as_of,
        sources=list(result.sources),
        reason=reason,
        unit=unit,
        consensus=result,
    )


def _observations_for(
    candidates: tuple[tuple[Decimal | None, str, date | None], ...],
    *,
    unit: str,
) -> list[SourceObservation]:
    return [
        observation
        for value, source, as_of in candidates
        if (observation := _observation(value, source=source, as_of=as_of, unit=unit)) is not None
    ]


def _merge_sources(*metrics: OpportunityMetric) -> list[str]:
    return sorted({source for metric in metrics for source in metric.sources})


def _derived_metric(
    value: Decimal | None,
    *,
    as_of: date | None,
    sources: list[str],
    unit: str,
    reason: str,
) -> OpportunityMetric:
    return OpportunityMetric(
        value=value,
        as_of=as_of if value is not None else None,
        unit=unit,
        sources=sorted(set(sources)) if value is not None else [],
        independent_sources=[],
        source_lineage=sorted(set(sources)) if value is not None else [],
        consensus_status="derived" if value is not None else ConsensusStatus.missing_data.value,
        confidence=Decimal("0.70") if value is not None else Decimal("0"),
        unavailable_reason=None if value is not None else reason,
    )


def _opportunity_metrics(
    details: AssetDetails | None,
    dividends: list[Dividend],
    status: dict[str, Decimal],
    bazin_yield: Decimal,
) -> OpportunityMetrics:
    as_of = details.quote_date if details and details.quote_date else datetime.now(UTC).date()
    detail_as_of = details.quote_date if details else None
    fundamental_as_of = (
        details.last_balance_date if details and details.last_balance_date else detail_as_of
    )
    fields = _detail_fields(details)
    # Keep every finite provider observation until ``resolve_consensus`` can
    # apply the field's plausible range.  Dropping non-positive prices here
    # would erase the evidence trail and make a malformed provider response
    # indistinguishable from a source that did not answer.
    price_candidates = _observations_for(
        (
            (details.quote if details else None, SOURCE_FUNDAMENTUS, detail_as_of),
            (status.get("current_price"), SOURCE_STATUS_INVEST, None),
        ),
        unit="BRL",
    )
    price_metric = _resolved_metric(
        price_candidates,
        as_of=as_of,
        unit="BRL",
        reason="Current price unavailable",
        # A traded price must be strictly positive.  Keep zero and negatives
        # in the resolver's evidence set so they are surfaced as rejected
        # observations instead of disappearing as if the provider were empty.
        valid_range=(Decimal("1E-100"), Decimal("1E100")),
    )
    book_metric = _resolved_metric(
        _observations_for(
            (
                (
                    details.book_value_per_share if details else None,
                    SOURCE_FUNDAMENTUS,
                    fundamental_as_of,
                ),
                (status.get("book_value_per_share"), SOURCE_STATUS_INVEST, None),
            ),
            unit="BRL",
        ),
        as_of=fundamental_as_of or as_of,
        unit="BRL",
        reason="Book value per share unavailable",
    )
    earnings_metric = _resolved_metric(
        _observations_for(
            (
                (
                    details.earnings_per_share if details else None,
                    SOURCE_FUNDAMENTUS,
                    fundamental_as_of,
                ),
                (status.get("earnings_per_share"), SOURCE_STATUS_INVEST, None),
            ),
            unit="BRL",
        ),
        as_of=fundamental_as_of or as_of,
        unit="BRL",
        reason="Earnings per share unavailable",
    )
    price_to_book_candidates = _observations_for(
        (
            (fields.get("p_vp"), SOURCE_FUNDAMENTUS, detail_as_of),
            (status.get("price_to_book"), SOURCE_STATUS_INVEST, None),
        ),
        unit="multiple",
    )
    price_to_book_metric = _resolved_metric(
        price_to_book_candidates,
        as_of=as_of,
        unit="multiple",
        reason="Book value per share unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    if not price_to_book_candidates and price_metric.value is not None and book_metric.value:
        price_to_book_metric = _derived_metric(
            price_metric.value / book_metric.value,
            as_of=as_of,
            sources=_merge_sources(price_metric, book_metric),
            unit="multiple",
            reason="Book value per share unavailable",
        )
    price_to_earnings_candidates = _observations_for(
        (
            (fields.get("p_l"), SOURCE_FUNDAMENTUS, detail_as_of),
            (status.get("price_to_earnings"), SOURCE_STATUS_INVEST, None),
        ),
        unit="multiple",
    )
    price_to_earnings_metric = _resolved_metric(
        price_to_earnings_candidates,
        as_of=as_of,
        unit="multiple",
        reason="Earnings per share unavailable",
    )
    if (
        not price_to_earnings_candidates
        and price_metric.value is not None
        and earnings_metric.value
    ):
        price_to_earnings_metric = _derived_metric(
            price_metric.value / earnings_metric.value,
            as_of=as_of,
            sources=_merge_sources(price_metric, earnings_metric),
            unit="multiple",
            reason="Earnings per share unavailable",
        )

    cutoff = as_of - timedelta(days=365)
    dividend_total = Decimal("0")
    for item in dividends:
        event_date = item.ex_date or item.payment_date
        if event_date is not None and event_date >= cutoff:
            dividend_total += item.value or Decimal("0")
    dividend_candidates: list[SourceObservation] = []
    # An empty dividend page is absence of an observation unless the detail
    # page explicitly reports a zero yield.  Treating every empty list as a
    # zero creates a false disagreement with another source that reports a
    # positive distribution and suppresses Bazin/yield metrics.
    confirmed_fundamentus_zero = fields.get("div_yield") == 0
    if dividends or confirmed_fundamentus_zero:
        detail_dividends = _observation(
            dividend_total,
            source=SOURCE_FUNDAMENTUS,
            as_of=detail_as_of,
            unit="BRL",
        )
        if detail_dividends is not None:
            dividend_candidates.append(detail_dividends)
    status_dividends = _observation(
        status.get("dividends_12m"),
        source=SOURCE_STATUS_INVEST,
        as_of=None,
        unit="BRL",
    )
    if status_dividends is not None:
        dividend_candidates.append(status_dividends)
    dividend_result = resolve_consensus(
        dividend_candidates,
        expected_unit="BRL",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    dividend_metric = _metric(
        dividend_result.value,
        as_of=dividend_result.as_of or as_of,
        sources=list(dividend_result.sources),
        reason="Trailing dividends unavailable",
        unit="BRL",
        consensus=dividend_result,
    )
    dividend_total_value = dividend_metric.value
    dividend_yield = (
        dividend_total_value / price_metric.value * Decimal("100")
        if dividend_total_value is not None and price_metric.value
        else None
    )
    dividend_yield_metric = _derived_metric(
        dividend_yield,
        as_of=as_of,
        sources=list(dividend_metric.sources) + list(price_metric.sources),
        unit="percent",
        reason="Trailing dividends unavailable",
    )

    graham = None
    if (
        earnings_metric.value is not None
        and earnings_metric.value > 0
        and book_metric.value is not None
        and book_metric.value > 0
    ):
        # Keep the Graham estimate decimal all the way through. The context
        # is local so a large/small financial value cannot silently round the
        # process-wide decimal precision or pass through binary float space.
        with localcontext() as context:
            context.prec = max(
                34,
                len(earnings_metric.value.as_tuple().digits)
                + len(book_metric.value.as_tuple().digits)
                + 16,
            )
            graham = (Decimal("22.5") * earnings_metric.value * book_metric.value).sqrt()
    bazin = (
        dividend_total_value / (bazin_yield / Decimal("100"))
        if dividend_total_value is not None and bazin_yield > 0
        else None
    )
    min_metric = _resolved_metric(
        _observations_for(
            (
                (details.min_52_weeks if details else None, SOURCE_FUNDAMENTUS, detail_as_of),
                (status.get("min_52_weeks"), SOURCE_STATUS_INVEST, None),
            ),
            unit="BRL",
        ),
        as_of=as_of,
        unit="BRL",
        reason="52-week minimum unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    max_metric = _resolved_metric(
        _observations_for(
            (
                (details.max_52_weeks if details else None, SOURCE_FUNDAMENTUS, detail_as_of),
                (status.get("max_52_weeks"), SOURCE_STATUS_INVEST, None),
            ),
            unit="BRL",
        ),
        as_of=as_of,
        unit="BRL",
        reason="52-week maximum unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    shares_metric = _resolved_metric(
        _observations_for(
            ((details.shares_count if details else None, SOURCE_FUNDAMENTUS, fundamental_as_of),),
            unit="shares",
        ),
        as_of=fundamental_as_of or as_of,
        unit="shares",
        reason="Outstanding shares unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    traded_value_metric = _resolved_metric(
        _observations_for(
            (
                (
                    details.average_daily_volume_2m if details else None,
                    SOURCE_FUNDAMENTUS,
                    detail_as_of,
                ),
            ),
            unit="BRL",
        ),
        as_of=as_of,
        unit="BRL",
        reason="Average daily traded value unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    market_cap_metric = _resolved_metric(
        _observations_for(
            ((details.market_value if details else None, SOURCE_FUNDAMENTUS, detail_as_of),),
            unit="BRL",
        ),
        as_of=as_of,
        unit="BRL",
        reason="Market capitalization unavailable",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    fundamental_sources = _merge_sources(earnings_metric, book_metric)
    return OpportunityMetrics(
        current_price=price_metric,
        shares_outstanding=shares_metric,
        earnings_per_share=earnings_metric,
        book_value_per_share=book_metric,
        price_to_book=price_to_book_metric,
        price_to_earnings=price_to_earnings_metric,
        dividend_yield_12m=dividend_yield_metric,
        dividends_12m=dividend_metric,
        graham_price=_derived_metric(
            graham,
            as_of=as_of,
            sources=fundamental_sources,
            unit="BRL",
            reason="Positive earnings and book value are required",
        ),
        bazin_price=_derived_metric(
            bazin,
            as_of=as_of,
            sources=list(dividend_metric.sources),
            unit="BRL",
            reason="Trailing dividends unavailable",
        ),
        min_52_weeks=min_metric,
        max_52_weeks=max_metric,
        average_daily_traded_value=traded_value_metric,
        market_capitalization=market_cap_metric,
    )


def _merge_official_fund_metrics(
    metrics: OpportunityMetrics,
    series: CvmReportSeries,
) -> OpportunityMetrics:
    if not series.reports:
        return metrics
    latest = max(series.reports, key=lambda item: item.as_of)
    previous_book = metrics.book_value_per_share
    candidates = [
        SourceObservation(
            value=item.value,
            source=item.source,
            as_of=item.as_of,
            unit=item.unit or "BRL",
            source_lineage=tuple(item.source_lineage),
            independent_origin=item.independent_origin,
        )
        for item in previous_book.observations
    ]
    candidates.append(
        SourceObservation(
            value=latest.nav_per_share,
            source=SOURCE_CVM,
            as_of=latest.as_of,
            unit="BRL",
        )
    )
    official_result = resolve_consensus(
        candidates,
        expected_unit="BRL",
        valid_range=(Decimal("0"), Decimal("1E100")),
    )
    # CVM's monthly report is the authoritative single-source policy for a
    # fund NAV.  It is used only when the independent observations have no
    # majority; this keeps ordinary equity fields on the vote-based rule.
    if official_result.status is ConsensusStatus.conflict:
        official_result = ConsensusResult(
            value=latest.nav_per_share,
            as_of=latest.as_of,
            sources=(SOURCE_CVM,),
            independent_sources=(SOURCE_CVM,),
            source_lineage=(SOURCE_CVM,),
            status=ConsensusStatus.single_source,
            reason="CVM monthly NAV is the authoritative fund source",
            confidence=Decimal("0.98"),
            observations=(candidates[-1],),
            rejected_observations=tuple(candidates[:-1]),
        )
    current_price = metrics.current_price.value
    selected_book = official_result.value
    price_to_book = (
        current_price / selected_book
        if current_price is not None and current_price > 0 and selected_book
        else None
    )
    selected_as_of = official_result.as_of or latest.as_of
    selected_sources = [*official_result.sources, *metrics.current_price.sources]
    return metrics.model_copy(
        update={
            "book_value_per_share": _metric(
                official_result.value,
                as_of=selected_as_of,
                sources=list(official_result.sources),
                unit="BRL",
                consensus=official_result,
                reason="Book value per share unavailable",
            ),
            "price_to_book": _derived_metric(
                price_to_book,
                as_of=selected_as_of,
                sources=selected_sources,
                unit="multiple",
                reason="Current price unavailable",
            ),
        }
    )


def _merge_fund_distributions(
    dividends: list[Dividend],
    status_distributions: tuple[FundDistribution, ...],
) -> tuple[FundDistribution, ...]:
    """Merge source histories while exposing only values with a valid vote.

    This compatibility wrapper retains the historical public return type.  The
    opportunity service uses :func:`_reconcile_fund_distributions` directly so
    it can retain conflict and missing statuses for derived metrics.
    """

    return _public_fund_distributions(
        _reconcile_fund_distributions(dividends, status_distributions)
    )


def _reconcile_fund_distributions(
    dividends: list[Dividend],
    status_distributions: tuple[FundDistribution, ...],
) -> tuple[_ReconciledDistribution, ...]:
    """Return deterministic, date-keyed consensus results for fund events.

    A distribution date is one event.  Each independent source contributes at
    most its observed value for that event, and ``resolve_consensus`` decides
    whether those values can be projected.  We intentionally do not apply
    source-order precedence: a conflicting newest event remains unresolved and
    is never silently replaced by an older value.
    """

    by_date: dict[date, list[SourceObservation]] = {}

    for distribution in status_distributions:
        if not distribution.value.is_finite():
            continue
        by_date.setdefault(distribution.ex_date, []).append(
            SourceObservation(
                value=distribution.value,
                source=distribution.source,
                as_of=distribution.ex_date,
                unit="BRL",
                source_lineage=(distribution.source,),
                independent_origin=distribution.source,
            )
        )

    for dividend in dividends:
        event_date = dividend.ex_date or dividend.payment_date
        if (
            event_date is None
            or dividend.value is None
            or not dividend.value.is_finite()
            or "AMORT" in _fold(dividend.type)
        ):
            continue
        by_date.setdefault(event_date, []).append(
            SourceObservation(
                value=dividend.value,
                source=SOURCE_FUNDAMENTUS,
                as_of=event_date,
                unit="BRL",
                source_lineage=(SOURCE_FUNDAMENTUS,),
                independent_origin=SOURCE_FUNDAMENTUS,
            )
        )

    reconciled: list[_ReconciledDistribution] = []
    for event_date in sorted(by_date, reverse=True):
        observations = tuple(
            sorted(
                by_date[event_date],
                key=lambda item: (item.source, item.origin, item.value),
            )
        )
        # Pass every observation through the shared resolver. It collapses
        # duplicate rows into one independent vote, excludes origins that
        # disagree with themselves, and still allows a majority of the other
        # independent origins to win.
        consensus = resolve_consensus(
            observations,
            expected_unit="BRL",
            valid_range=(Decimal("0"), Decimal("1E100")),
        )
        reconciled.append(
            _ReconciledDistribution(
                ex_date=event_date,
                consensus=consensus,
            )
        )
    return tuple(reconciled)


def _public_fund_distributions(
    distributions: tuple[_ReconciledDistribution, ...],
) -> tuple[FundDistribution, ...]:
    """Project only consensus values into the legacy response contract."""

    projected: list[FundDistribution] = []
    for distribution in distributions:
        result = distribution.consensus
        if result.value is None:
            # The public model cannot carry a conflict without inventing a
            # numeric value.  Leave it out; metrics retain the full status and
            # observations for callers that need to explain the omission.
            continue
        projected.append(
            FundDistribution(
                ex_date=distribution.ex_date,
                value=result.value,
                source="+".join(result.sources),
            )
        )
    return tuple(projected)


def _public_fund_distribution_evidence(
    distributions: tuple[_ReconciledDistribution, ...],
) -> tuple[FundDistributionEvidence, ...]:
    """Expose one deterministic, auditable record for every event date."""

    return tuple(
        FundDistributionEvidence(
            ex_date=distribution.ex_date,
            value=distribution.consensus.value,
            status=distribution.consensus.status.value,
            reason=distribution.consensus.reason,
            confidence=distribution.consensus.confidence,
            sources=list(distribution.consensus.sources),
            independent_sources=list(distribution.consensus.independent_sources),
            source_lineage=list(distribution.consensus.source_lineage),
            observations=_public_observations(distribution.consensus.observations),
            rejected_observations=_public_observations(
                distribution.consensus.rejected_observations
            ),
        )
        for distribution in distributions
    )


def _add_distribution_metrics(
    metrics: OpportunityMetrics,
    distributions: tuple[_ReconciledDistribution, ...] | tuple[FundDistribution, ...],
) -> OpportunityMetrics:
    if not distributions:
        return metrics
    reconciled = _coerce_reconciled_distributions(distributions)
    latest = reconciled[0]
    latest_result = latest.consensus
    return metrics.model_copy(
        update={
            "latest_distribution": _metric(
                latest_result.value,
                as_of=latest_result.as_of or latest.ex_date,
                sources=list(latest_result.sources),
                reason="Latest distribution unavailable",
                unit="BRL",
                consensus=latest_result,
            ),
            "median_distribution_3m": _distribution_median(reconciled, 3),
            "median_distribution_6m": _distribution_median(reconciled, 6),
        }
    )


def _distribution_median(
    distributions: tuple[_ReconciledDistribution, ...] | tuple[FundDistribution, ...],
    months: int,
) -> OpportunityMetric:
    reconciled = _coerce_reconciled_distributions(distributions)
    selected = reconciled[:months]
    reason = f"At least {months} monthly distributions are required"
    if len(selected) < months:
        unresolved = [item for item in selected if item.consensus.value is None]
        if unresolved:
            status = (
                ConsensusStatus.conflict
                if any(item.consensus.status is ConsensusStatus.conflict for item in unresolved)
                else ConsensusStatus.invalid_data
            )
            return _distribution_unavailable_metric(
                selected,
                status=status,
                reason="Distribution history contains unresolved source observations",
            )
        return _distribution_unavailable_metric(
            selected,
            status=ConsensusStatus.missing_data,
            reason=reason,
        )

    unresolved = [item for item in selected if item.consensus.value is None]
    if unresolved:
        status = (
            ConsensusStatus.conflict
            if any(item.consensus.status is ConsensusStatus.conflict for item in unresolved)
            else ConsensusStatus.invalid_data
        )
        return _distribution_unavailable_metric(
            selected,
            status=status,
            reason="Distribution history contains unresolved source observations",
        )

    values = tuple(item.consensus.value for item in selected)
    # ``unresolved`` above guarantees all values are non-null.  Keeping this
    # guard makes the invariant explicit to static type checkers and future
    # callers that may construct a malformed private value.
    if any(value is None for value in values):
        return _distribution_unavailable_metric(
            selected,
            status=ConsensusStatus.invalid_data,
            reason="Distribution history contains an invalid value",
        )
    return _distribution_derived_metric(
        _median(tuple(value for value in values if value is not None)),
        selected,
        reason=reason,
    )


def _coerce_reconciled_distributions(
    distributions: tuple[_ReconciledDistribution, ...] | tuple[FundDistribution, ...],
) -> tuple[_ReconciledDistribution, ...]:
    """Keep private helpers compatible with their pre-consensus input type."""

    if not distributions:
        return ()
    if isinstance(distributions[0], _ReconciledDistribution):
        return distributions
    return tuple(
        _ReconciledDistribution(
            ex_date=item.ex_date,
            consensus=ConsensusResult(
                value=item.value,
                as_of=item.ex_date,
                sources=(item.source,),
                independent_sources=(item.source,),
                source_lineage=(item.source,),
                status=ConsensusStatus.single_source,
                reason="Only one independent source was available",
                confidence=Decimal("0.55"),
                observations=(
                    SourceObservation(
                        value=item.value,
                        source=item.source,
                        as_of=item.ex_date,
                        unit="BRL",
                        source_lineage=(item.source,),
                        independent_origin=item.source,
                    ),
                ),
            ),
        )
        for item in distributions
    )


def _distribution_unavailable_metric(
    distributions: tuple[_ReconciledDistribution, ...],
    *,
    status: ConsensusStatus,
    reason: str,
) -> OpportunityMetric:
    observations = tuple(
        observation for item in distributions for observation in item.consensus.observations
    )
    rejected_observations = tuple(
        observation
        for item in distributions
        for observation in item.consensus.rejected_observations
    )
    evidence = (*observations, *rejected_observations)
    sources = tuple(sorted({observation.source for observation in evidence}))
    origins = tuple(sorted({observation.origin for observation in evidence}))
    lineage = tuple(sorted({line for observation in evidence for line in observation.lineage}))
    result = ConsensusResult(
        status=status,
        reason=reason,
        sources=sources,
        independent_sources=origins,
        source_lineage=lineage,
        observations=observations,
        rejected_observations=rejected_observations,
    )
    return _metric(
        None,
        as_of=None,
        sources=list(sources),
        reason=reason,
        unit="BRL",
        consensus=result,
    )


def _distribution_derived_metric(
    value: Decimal | None,
    distributions: tuple[_ReconciledDistribution, ...],
    *,
    reason: str,
) -> OpportunityMetric:
    observations = tuple(
        observation for item in distributions for observation in item.consensus.observations
    )
    rejected_observations = tuple(
        observation
        for item in distributions
        for observation in item.consensus.rejected_observations
    )
    # Keep provenance on a derived value limited to the observations that
    # actually contributed to its inputs.  Rejected candidates remain exposed
    # separately below and must not make the derived source set look selected.
    sources = sorted({source for item in distributions for source in item.consensus.sources})
    origins = sorted(
        {origin for item in distributions for origin in item.consensus.independent_sources}
    )
    lineage = sorted({line for item in distributions for line in item.consensus.source_lineage})
    return OpportunityMetric(
        value=value,
        as_of=max((item.ex_date for item in distributions), default=None),
        unit="BRL",
        sources=sources,
        independent_sources=origins,
        source_lineage=lineage,
        observations=[
            *(_public_observations(observations)),
        ],
        rejected_observations=_public_observations(rejected_observations),
        consensus_status="derived",
        confidence=min(
            (item.consensus.confidence for item in distributions),
            default=Decimal("0"),
        ),
        unavailable_reason=None if value is not None else reason,
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
