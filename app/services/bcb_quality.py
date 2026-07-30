from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from urllib.parse import quote

import httpx

from app.config import Settings

_IPCA_SERIES = 433
_SELIC_MONTHLY_SERIES = 4189
_BANK_CAPITAL_REPORT = "5"
_BANK_CREDIT_RISK_REPORT = "8"
_BASEL_ACCOUNT = "79664"
_CORE_CAPITAL_ACCOUNT = "79659"
_LEVERAGE_ACCOUNT = "79661"
_LEGAL_TOKENS = {
    "CIA",
    "COMPANHIA",
    "DA",
    "DE",
    "DO",
    "HOLDING",
    "S",
    "SA",
    "SOCIEDADE",
}


@dataclass(frozen=True)
class MacroQualitySnapshot:
    inflation_by_year: dict[int, Decimal]
    selic_by_year: dict[int, Decimal]
    as_of: date | None


@dataclass(frozen=True)
class BankQualitySnapshot:
    basel_ratio: Decimal | None = None
    core_capital_ratio: Decimal | None = None
    leverage_ratio: Decimal | None = None
    high_risk_credit_ratio: Decimal | None = None
    capital_as_of: date | None = None
    credit_as_of: date | None = None
    institution_name: str | None = None


class BcbMacroProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: MacroQualitySnapshot | None = None

    async def snapshot(self, reference: date | None = None) -> MacroQualitySnapshot:
        if self._cache is not None:
            return self._cache
        end = reference or datetime.now(UTC).date()
        start = end.replace(year=end.year - self.settings.fundamentals_history_years + 1)
        inflation, selic = await asyncio.gather(
            self._series(_IPCA_SERIES, start, end),
            self._series(_SELIC_MONTHLY_SERIES, start, end),
        )
        self._cache = MacroQualitySnapshot(
            inflation_by_year=_compound_monthly(inflation),
            selic_by_year=_average_monthly(selic),
            as_of=max(
                (observed_at for observed_at, _value in (*inflation, *selic)),
                default=None,
            ),
        )
        return self._cache

    async def _series(
        self,
        series: int,
        start: date,
        end: date,
    ) -> tuple[tuple[date, Decimal], ...]:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.bcb_sgs_base_url,
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                transport=self.transport,
                follow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
            ) as client:
                response = await client.get(
                    f"/dados/serie/bcdata.sgs.{series}/dados",
                    params={
                        "formato": "json",
                        "dataInicial": start.strftime("%d/%m/%Y"),
                        "dataFinal": end.strftime("%d/%m/%Y"),
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return ()
        return _series_values(payload)


class BcbBankProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._registrations: dict[int, tuple[dict[str, object], ...]] = {}
        self._cache: dict[str, BankQualitySnapshot] = {}
        self._registration_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(2)

    async def snapshot(
        self,
        company_name: str,
        reference: date | None = None,
    ) -> BankQualitySnapshot:
        key = _fold(company_name)
        if key in self._cache:
            return self._cache[key]
        capital_period = _latest_disclosed_quarter(reference or datetime.now(UTC).date())
        registrations = await self._registration(capital_period)
        institution = _match_institution(company_name, registrations)
        if institution is None:
            return BankQualitySnapshot()
        prudential_code = _text(institution.get("CodConglomeradoPrudencial"))
        financial_code = _financial_conglomerate_code(institution, registrations)
        capital_rows, credit_result = await asyncio.gather(
            self._values(capital_period, 1, _BANK_CAPITAL_REPORT, prudential_code),
            self._latest_credit_values(financial_code, capital_period),
        )
        credit_period, credit_rows = credit_result
        result = BankQualitySnapshot(
            basel_ratio=_account_value(capital_rows, _BASEL_ACCOUNT),
            core_capital_ratio=_account_value(capital_rows, _CORE_CAPITAL_ACCOUNT),
            leverage_ratio=_account_value(capital_rows, _LEVERAGE_ACCOUNT),
            high_risk_credit_ratio=_high_risk_credit_ratio(credit_rows),
            capital_as_of=_period_date(capital_period) if capital_rows else None,
            credit_as_of=_period_date(credit_period) if credit_rows else None,
            institution_name=_text(institution.get("NomeInstituicao")),
        )
        if result.basel_ratio is not None:
            self._cache[key] = result
        return result

    async def _registration(self, period: int) -> tuple[dict[str, object], ...]:
        if period in self._registrations:
            return self._registrations[period]
        async with self._registration_lock:
            if period in self._registrations:
                return self._registrations[period]
            values = await self._request(f"IfDataCadastro(AnoMes={period})")
            if values:
                self._registrations[period] = values
            return values

    async def _values(
        self,
        period: int,
        institution_type: int,
        report: str,
        code: str | None,
    ) -> tuple[dict[str, object], ...]:
        if not code:
            return ()
        return await self._request(
            (
                f"IfDataValores(AnoMes={period},TipoInstituicao={institution_type},"
                f"Relatorio='{report}')"
            ),
            filter_code=code,
        )

    async def _latest_credit_values(
        self,
        code: str | None,
        capital_period: int,
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        start = min(capital_period, 202412)
        for period in _previous_quarters(start, 8):
            values = await self._values(period, 2, _BANK_CREDIT_RISK_REPORT, code)
            if values:
                return period, values
        return start, ()

    async def _request(
        self,
        path: str,
        *,
        filter_code: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        query = "?%24format=json"
        if filter_code:
            query += f"&%24filter=CodInst%20eq%20%27{quote(filter_code, safe='')}%27"
        payload = await self._request_payload(f"{path}{query}")
        values = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return ()
        return tuple(item for item in values if isinstance(item, dict))

    async def _request_payload(self, path: str) -> object:
        async with self._request_semaphore:
            for attempt in range(self.settings.retry_attempts):
                try:
                    async with httpx.AsyncClient(
                        base_url=self.settings.bcb_ifdata_base_url,
                        timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                        transport=self.transport,
                        follow_redirects=True,
                        headers={"User-Agent": self.settings.user_agent},
                    ) as client:
                        response = await client.get(path)
                        response.raise_for_status()
                        return response.json()
                except (httpx.HTTPError, ValueError):
                    if attempt + 1 < self.settings.retry_attempts:
                        await asyncio.sleep(self.settings.retry_backoff_seconds * (2**attempt))
        return None


def _series_values(payload: object) -> tuple[tuple[date, Decimal], ...]:
    if not isinstance(payload, list):
        return ()
    values = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            observed_at = datetime.strptime(str(row.get("data")), "%d/%m/%Y").date()
            value = Decimal(str(row.get("valor")).replace(",", ".")) / Decimal("100")
        except (InvalidOperation, ValueError):
            continue
        values.append((observed_at, value))
    return tuple(values)


def _compound_monthly(
    values: tuple[tuple[date, Decimal], ...],
) -> dict[int, Decimal]:
    compounded: dict[int, Decimal] = {}
    for observed_at, value in values:
        compounded[observed_at.year] = compounded.get(observed_at.year, Decimal("1")) * (
            Decimal("1") + value
        )
    return {year: value - Decimal("1") for year, value in compounded.items()}


def _average_monthly(
    values: tuple[tuple[date, Decimal], ...],
) -> dict[int, Decimal]:
    grouped: dict[int, list[Decimal]] = {}
    for observed_at, value in values:
        grouped.setdefault(observed_at.year, []).append(value)
    return {
        year: sum(samples, Decimal("0")) / Decimal(len(samples))
        for year, samples in grouped.items()
        if samples
    }


def _latest_disclosed_quarter(reference: date) -> int:
    candidates = (
        (3, date(reference.year, 5, 31)),
        (6, date(reference.year, 8, 31)),
        (9, date(reference.year, 11, 30)),
        (12, date(reference.year + 1, 3, 31)),
    )
    available = [
        reference.year * 100 + month
        for month, disclosed_at in candidates
        if reference >= disclosed_at
    ]
    return max(available, default=(reference.year - 1) * 100 + 12)


def _previous_quarters(period: int, count: int) -> tuple[int, ...]:
    year, month = divmod(period, 100)
    result = []
    for _index in range(count):
        result.append(year * 100 + month)
        month -= 3
        if month <= 0:
            year -= 1
            month += 12
    return tuple(result)


def _period_date(period: int) -> date:
    year, month = divmod(period, 100)
    return date(year, month, 1)


def _match_institution(
    company_name: str,
    registrations: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    target = _fold(company_name)
    candidates = [
        item
        for item in registrations
        if _text(item.get("NomeInstituicao")) and _text(item.get("CodConglomeradoPrudencial"))
    ]
    if not candidates:
        return None
    result = max(
        candidates,
        key=lambda item: _name_score(target, _fold(str(item["NomeInstituicao"]))),
    )
    return (
        result
        if _name_score(target, _fold(str(result["NomeInstituicao"]))) >= Decimal("0.30")
        else None
    )


def _name_score(first: str, second: str) -> Decimal:
    first_tokens = set(first.split()) - _LEGAL_TOKENS
    second_tokens = set(second.split()) - _LEGAL_TOKENS
    overlap = Decimal(len(first_tokens & second_tokens)) / Decimal(
        max(1, min(len(first_tokens), len(second_tokens)))
    )
    similarity = Decimal(str(SequenceMatcher(None, first, second).ratio()))
    return overlap * Decimal("0.65") + similarity * Decimal("0.35")


def _financial_conglomerate_code(
    institution: dict[str, object],
    registrations: tuple[dict[str, object], ...],
) -> str | None:
    prudential = _text(institution.get("CodConglomeradoPrudencial"))
    matches = [
        item
        for item in registrations
        if _text(item.get("CodConglomeradoPrudencial")) == prudential
        and _text(item.get("Td")) == "C"
        and "PRUDENCIAL" not in _fold(str(item.get("NomeInstituicao") or ""))
    ]
    return _text(matches[0].get("CodInst")) if matches else _text(institution.get("CodInst"))


def _account_value(
    rows: tuple[dict[str, object], ...],
    account: str,
) -> Decimal | None:
    row = next((item for item in rows if str(item.get("Conta")) == account), None)
    return _decimal(row.get("Saldo")) if row else None


def _high_risk_credit_ratio(rows: tuple[dict[str, object], ...]) -> Decimal | None:
    total = next(
        (
            _decimal(item.get("Saldo"))
            for item in rows
            if str(item.get("NomeColuna")) == "Total Geral"
        ),
        None,
    )
    if total is None or total <= 0:
        return None
    high_risk = sum(
        (
            _decimal(item.get("Saldo")) or Decimal("0")
            for item in rows
            if str(item.get("NomeColuna")) in {"E", "F", "G", "H"}
        ),
        Decimal("0"),
    )
    return high_risk / total


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    tokens = normalized.encode("ascii", "ignore").decode("ascii").upper().split()
    return " ".join("BANCO" if token == "BCO" else token for token in tokens)
