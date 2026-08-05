"""Small, deterministic adapter for the public SEC EDGAR CompanyFacts API.

CompanyFacts is the official SEC's normalized XBRL feed.  The adapter keeps
the network boundary deliberately narrow: one public ticker directory request
resolves a ticker to a zero-padded CIK and one CompanyFacts request supplies
the financial history.  No API key is accepted or required.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.scrapers.international_statements import (
    AnnualFigures,
    InternationalStatements,
)

SOURCE_SEC = "sec_edgar_companyfacts"
SEC_TICKER_DIRECTORY = "sec_edgar_tickers"
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_B3_PATTERN = re.compile(r"^[A-Z]{4}\d{1,2}$")
_CIK_PATTERN = re.compile(r"^(?:CIK)?(\d{1,10})$", re.IGNORECASE)
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
_MAX_YEARS = 5
_DAY_TOLERANCE = 20
_DURATION_FIELDS = frozenset(
    {
        "revenue",
        "gross_profit",
        "ebit",
        "net_income",
        "operating_cash_flow",
        "capex",
        "depreciation",
        "earnings_per_share",
    }
)

_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "Revenue",
    ),
    "gross_profit": ("GrossProfit",),
    "ebit": ("OperatingIncomeLoss", "OperatingIncomeOrLoss"),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLossAttributableToOwnersOfParent",
    ),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",
    ),
    "total_assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "cash_and_equivalents": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndDueFromBanks",
        "CashAndCashEquivalents",
    ),
    "current_debt": (
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
        "DebtCurrent",
    ),
    "noncurrent_debt": (
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "DebtNoncurrent",
    ),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "depreciation": (
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
    "earnings_per_share": (
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ),
}


@dataclass(frozen=True)
class _Observation:
    value: Decimal
    end: date
    start: date | None
    filed: date
    form: str
    accn: str


@dataclass(frozen=True)
class _CachedValue:
    expires_at: float
    value: Any


def normalize_sec_ticker(value: str) -> str:
    """Normalize a SEC ticker and reject B3 symbols or malformed input."""
    ticker = value.strip().upper()
    if not ticker or _B3_PATTERN.fullmatch(ticker) or _TICKER_PATTERN.fullmatch(ticker) is None:
        raise ValueError(f"Invalid SEC ticker: {value!r}")
    return ticker


def normalize_cik(value: str | int) -> str:
    """Return a SEC CIK as its canonical ten-digit representation."""
    match = _CIK_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"Invalid SEC CIK: {value!r}")
    return match.group(1).zfill(10)


class SecCompanyFactsProvider:
    """Resolve SEC-covered issuers with bounded retries and local caching."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: dict[str, _CachedValue] = {}
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def statements(self, ticker: str) -> InternationalStatements | None:
        normalized = normalize_sec_ticker(ticker)
        cik = await self._ticker_to_cik(normalized)
        if cik is None:
            return None
        cache_key = f"facts:{cik}"
        payload = await self._cached_json(
            cache_key,
            lambda: self._fetch_json(
                self.settings.sec_edgar_base_url,
                f"/api/xbrl/companyfacts/CIK{cik}.json",
            ),
            self.settings.sec_companyfacts_ttl_seconds,
        )
        if not isinstance(payload, dict):
            return None
        source_url = (
            f"{self.settings.sec_edgar_base_url.rstrip('/')}/api/xbrl/companyfacts/CIK{cik}.json"
        )
        return parse_company_facts(payload, normalized, cik, source_url)

    async def _ticker_to_cik(self, ticker: str) -> str | None:
        payload = await self._cached_json(
            "ticker-directory",
            lambda: self._fetch_json_url(self.settings.sec_company_tickers_url),
            self.settings.sec_ticker_map_ttl_seconds,
        )
        if not isinstance(payload, (dict, list)):
            return None
        values = payload.values() if isinstance(payload, dict) else payload
        for item in values:
            if not isinstance(item, dict):
                continue
            raw_ticker = item.get("ticker")
            if not isinstance(raw_ticker, str):
                continue
            try:
                candidate = normalize_sec_ticker(raw_ticker)
            except ValueError:
                continue
            if candidate != ticker:
                continue
            try:
                return normalize_cik(item["cik_str"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    async def _cached_json(
        self,
        key: str,
        loader: Any,
        ttl_seconds: int,
    ) -> Any | None:
        now = asyncio.get_running_loop().time()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.value
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                self._inflight[key] = task
        try:
            value = await task
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
        self._cache[key] = _CachedValue(now + ttl_seconds, value)
        return value

    async def _fetch_json_url(self, url: str) -> Any | None:
        return await self._fetch_json(url, None)

    async def _fetch_json(self, base_url: str, path: str | None) -> Any | None:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.settings.sec_user_agent or self.settings.user_agent,
        }
        timeout = httpx.Timeout(self.settings.sec_request_timeout_seconds)
        attempts = max(1, min(self.settings.retry_attempts, 3))
        for attempt in range(attempts):
            try:
                client_kwargs: dict[str, Any] = {
                    "timeout": timeout,
                    "transport": self.transport,
                    "follow_redirects": True,
                    "headers": headers,
                }
                if path is not None:
                    client_kwargs["base_url"] = base_url
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.get(path or base_url)
                if response.status_code == 404:
                    return None
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(self.settings.retry_backoff_seconds * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, (dict, list)) else None
            except (httpx.HTTPError, ValueError):
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.settings.retry_backoff_seconds * (attempt + 1))
                    continue
                return None
        return None


def parse_company_facts(
    payload: dict[str, Any],
    ticker: str,
    cik: str,
    source_url: str,
) -> InternationalStatements | None:
    """Map annual US-GAAP/IFRS-like observations into statement periods."""
    try:
        normalized_ticker = normalize_sec_ticker(ticker)
        normalized_cik = normalize_cik(cik)
    except ValueError:
        return None
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return None
    collected: dict[str, list[_Observation]] = {}
    for field, tags in _TAGS.items():
        observations = _find_observations(facts, tags, field)
        if observations:
            collected[field] = observations
    if not collected:
        return None
    period_ends = sorted(
        {
            item.end
            for field, values in collected.items()
            if field in _DURATION_FIELDS
            for item in values
            if item.start is not None and _is_annual_form(item.form, item.start, item.end)
        }
    )
    if not period_ends:
        return None
    period_ends = period_ends[-_MAX_YEARS:]
    annuals: list[AnnualFigures] = []
    for period_end in period_ends:
        selected = {
            field: _select_for_end(values, period_end) for field, values in collected.items()
        }
        annuals.append(
            AnnualFigures(
                period_end=period_end,
                revenue=_value(selected.get("revenue")),
                gross_profit=_value(selected.get("gross_profit")),
                ebit=_value(selected.get("ebit")),
                net_income=_value(selected.get("net_income")),
                earnings_per_share=_value(selected.get("earnings_per_share")),
                operating_cash_flow=_value(selected.get("operating_cash_flow")),
                capex=_value(selected.get("capex")),
                depreciation=_value(selected.get("depreciation")),
                free_cash_flow=_free_cash_flow(
                    _value(selected.get("operating_cash_flow")),
                    _value(selected.get("capex")),
                ),
                ebitda=_sum_optional(
                    _value(selected.get("ebit")),
                    _value(selected.get("depreciation")),
                ),
            )
        )
    latest = annuals[-1]
    annual_endpoint = latest.period_end
    equity = _latest_value(collected.get("equity", []), as_of=annual_endpoint)
    total_assets = _latest_value(collected.get("total_assets", []), as_of=annual_endpoint)
    current_assets = _latest_value(collected.get("current_assets", []), as_of=annual_endpoint)
    current_liabilities = _latest_value(
        collected.get("current_liabilities", []), as_of=annual_endpoint
    )
    cash = _latest_value(collected.get("cash_and_equivalents", []), as_of=annual_endpoint)
    current_debt = _latest_value(collected.get("current_debt", []), as_of=annual_endpoint)
    noncurrent_debt = _latest_value(collected.get("noncurrent_debt", []), as_of=annual_endpoint)
    gross_debt = _sum_optional(current_debt, noncurrent_debt)
    net_debt = gross_debt - cash if gross_debt is not None and cash is not None else None
    shares = _latest_value(collected.get("shares_outstanding", []), as_of=annual_endpoint)
    if shares is None:
        shares = _quotient(latest.net_income, latest.earnings_per_share)
    currency = _currency(facts, _TAGS)
    return InternationalStatements(
        ticker=normalized_ticker,
        company_name=str(payload.get("entityName") or "") or None,
        years=tuple(annuals),
        equity=equity,
        total_assets=total_assets,
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        gross_debt=gross_debt,
        short_term_debt=current_debt,
        long_term_debt=noncurrent_debt,
        net_debt=net_debt,
        cash_and_equivalents=cash,
        currency=currency,
        source=SOURCE_SEC,
        source_url=source_url,
        identifiers={"cik": normalized_cik},
        shares_outstanding=shares,
    )


def _find_observations(
    facts: dict[str, Any],
    tags: tuple[str, ...],
    field: str,
) -> list[_Observation]:
    for namespace in ("us-gaap", "ifrs-full", "dei"):
        values = facts.get(namespace)
        if not isinstance(values, dict):
            continue
        for tag in tags:
            raw_tag = values.get(tag)
            parsed = _parse_tag(raw_tag, field)
            if parsed:
                return parsed
    return []


def _parse_tag(raw_tag: Any, field: str) -> list[_Observation]:
    if not isinstance(raw_tag, dict) or not isinstance(raw_tag.get("units"), dict):
        return []
    units = raw_tag["units"]
    unit_name, entries = _choose_unit(units, field)
    if unit_name is None or not isinstance(entries, list):
        return []
    parsed: list[_Observation] = []
    for entry in entries:
        if not isinstance(entry, dict) or "val" not in entry:
            continue
        end = _as_date(entry.get("end"))
        if end is None:
            continue
        start = _as_date(entry.get("start"))
        form = str(entry.get("form") or "")
        if field not in {"shares_outstanding"} and not _is_annual_form(form, start, end):
            continue
        try:
            value = Decimal(str(entry["val"]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        filed = _as_date(entry.get("filed")) or date.min
        parsed.append(
            _Observation(
                value=value,
                end=end,
                start=start,
                filed=filed,
                form=form,
                accn=str(entry.get("accn") or ""),
            )
        )
    return parsed


def _choose_unit(units: dict[str, Any], field: str) -> tuple[str | None, Any]:
    candidates = [(str(name), values) for name, values in units.items() if isinstance(values, list)]
    if not candidates:
        return None, []
    if field == "shares_outstanding":
        candidates.sort(key=lambda item: ("shares" not in item[0].lower(), item[0]))
    elif field == "earnings_per_share":
        candidates.sort(key=lambda item: ("/" not in item[0], item[0]))
    else:
        candidates.sort(key=lambda item: (item[0] not in {"USD", "EUR", "GBP"}, item[0]))
    return candidates[0]


def _is_annual_form(form: str, start: date | None, end: date) -> bool:
    if form in _ANNUAL_FORMS:
        return True
    if start is None:
        return False
    return (end - start).days >= 300 - _DAY_TOLERANCE


def _select_for_end(
    observations: list[_Observation],
    period_end: date,
) -> _Observation | None:
    matching = [item for item in observations if item.end == period_end]
    if not matching:
        return None
    # A later filed amendment is authoritative.  Accession number is a stable
    # tie-breaker for fixture data where filed dates are equal.
    return max(matching, key=lambda item: (item.filed, item.form.endswith("/A"), item.accn))


def _latest_value(
    observations: list[_Observation],
    *,
    as_of: date | None = None,
) -> Decimal | None:
    if not observations:
        return None
    if as_of is not None:
        observations = [item for item in observations if item.end <= as_of]
        if not observations:
            return None
    latest = max(
        observations,
        key=lambda item: (item.end, item.filed, item.form.endswith("/A"), item.accn),
    )
    return latest.value


def _value(observation: _Observation | None) -> Decimal | None:
    return observation.value if observation is not None else None


def _quotient(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _sum_optional(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _free_cash_flow(
    operating_cash_flow: Decimal | None,
    capex: Decimal | None,
) -> Decimal | None:
    if operating_cash_flow is None or capex is None:
        return None
    return operating_cash_flow - abs(capex)


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _currency(facts: dict[str, Any], tags: dict[str, tuple[str, ...]]) -> str:
    for namespace in ("us-gaap", "ifrs-full"):
        values = facts.get(namespace)
        if not isinstance(values, dict):
            continue
        for tag in tags["revenue"]:
            raw = values.get(tag)
            if not isinstance(raw, dict) or not isinstance(raw.get("units"), dict):
                continue
            units = [str(unit) for unit in raw["units"]]
            for preferred in ("USD", "EUR", "GBP", "CAD", "AUD"):
                if preferred in units:
                    return preferred
    return "USD"
