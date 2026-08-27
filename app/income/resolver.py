from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, date
from decimal import Decimal
from typing import cast

from app.models.income_events import (
    CanonicalIncomeEvent,
    IncomeEventObservation,
    IncomeEventStatus,
    IncomeFieldConfidence,
)

OFFICIAL_AUTHORITY = 80
_TYPE_ALIASES = (
    (("AMORT",), "Amortização"),
    (("JRS CAP", "JUROS SOBRE CAPITAL", "JSCP", "JCP"), "Juros Sobre Capital Próprio"),
    (("REEMBOLSO",), "Reembolso"),
    (("REND",), "Rendimento"),
    (("DIVID",), "Dividendo"),
)


def canonical_event_type(value: str) -> str:
    folded = _fold(value)
    for terms, label in _TYPE_ALIASES:
        if any(term in folded for term in terms):
            return label
    return value.strip().title() or "Provento"


def resolve_income_events(
    observations: list[IncomeEventObservation],
) -> list[CanonicalIncomeEvent]:
    candidates = [item for item in observations if _complete(item)]
    grouped: dict[tuple[str, str, date, str], list[IncomeEventObservation]] = defaultdict(list)
    generic: list[IncomeEventObservation] = []
    for item in candidates:
        assert item.ex_date is not None
        event_type = canonical_event_type(item.event_type)
        if event_type == "Provento":
            generic.append(item)
            continue
        grouped[
            (item.ticker.upper(), event_type, item.ex_date, _amount_bucket(item.unit_price))
        ].append(item)
    for item in generic:
        assert item.ex_date is not None
        base = (item.ticker.upper(), item.ex_date, _amount_bucket(item.unit_price))
        compatible = [key for key in grouped if (key[0], key[2], key[3]) == base]
        key = compatible[0] if len(compatible) == 1 else (base[0], "Provento", base[1], base[2])
        grouped[key].append(item)
    return sorted(
        (_resolve_group(key, group) for key, group in grouped.items()),
        key=lambda item: (item.ticker, item.payment_date, item.event_type, item.event_id),
    )


def _resolve_group(
    key: tuple[str, str, date, str],
    observations: list[IncomeEventObservation],
) -> CanonicalIncomeEvent:
    ticker, event_type, ex_date, _bucket = key
    active = [
        item for item in observations if item.source_status.lower() not in {"cancelled", "canceled"}
    ]
    selected = active or observations
    ordered = sorted(selected, key=lambda item: (item.authority, item.source_version), reverse=True)
    payment = _best_value(ordered, "payment_date")
    amount = _best_value(ordered, "unit_price")
    assert isinstance(payment, date)
    assert isinstance(amount, Decimal)
    isin = _best_value(ordered, "isin")
    reference_period = _best_value(ordered, "reference_period")
    sources = sorted({item.source for item in observations})
    lineages = {item.lineage for item in active}
    authoritative = [item for item in active if item.authority >= OFFICIAL_AUTHORITY]
    status = _status(observations, authoritative, lineages)
    confidence = _confidence(status)
    field_source = _field_sources(ordered)
    identity = "|".join((ticker, event_type, ex_date.isoformat(), _amount_bucket(amount)))
    return CanonicalIncomeEvent(
        event_id=f"income:{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
        ticker=ticker,
        isin=str(isin) if isin else None,
        event_type=event_type,
        ex_date=ex_date,
        payment_date=payment,
        unit_price=amount,
        reference_period=str(reference_period) if reference_period else None,
        status=status,
        revision=max(item.source_version for item in observations),
        sources=sources,
        field_sources=field_source,
        field_confidence={field: confidence for field in field_source},
        updated_at=max(item.observed_at for item in observations).astimezone(UTC),
    )


def _complete(item: IncomeEventObservation) -> bool:
    return (
        item.ex_date is not None
        and item.payment_date is not None
        and item.unit_price is not None
        and item.unit_price > 0
    )


def _status(
    observations: list[IncomeEventObservation],
    authoritative: list[IncomeEventObservation],
    lineages: set[str],
) -> IncomeEventStatus:
    if not any(
        item.source_status.lower() not in {"cancelled", "canceled"} for item in observations
    ):
        return IncomeEventStatus.cancelled
    if _official_conflict(authoritative):
        return IncomeEventStatus.conflicted
    if authoritative:
        return IncomeEventStatus.verified
    if len(lineages) >= 2:
        return IncomeEventStatus.corroborated
    return IncomeEventStatus.tentative


def _official_conflict(observations: list[IncomeEventObservation]) -> bool:
    if len(observations) < 2:
        return False
    payments = {item.payment_date for item in observations}
    amounts = {_amount_bucket(item.unit_price) for item in observations}
    return len(payments) > 1 or len(amounts) > 1


def _confidence(status: IncomeEventStatus) -> IncomeFieldConfidence:
    if status is IncomeEventStatus.verified:
        return IncomeFieldConfidence.authoritative
    if status is IncomeEventStatus.corroborated:
        return IncomeFieldConfidence.corroborated
    return IncomeFieldConfidence.tentative


def _best_value(observations: list[IncomeEventObservation], field: str) -> object | None:
    for item in observations:
        value = getattr(item, field)
        if value not in {None, ""}:
            return cast(object, value)
    return None


def _field_sources(observations: list[IncomeEventObservation]) -> dict[str, str]:
    return {
        field: source
        for field in (
            "isin",
            "event_type",
            "ex_date",
            "payment_date",
            "unit_price",
            "reference_period",
        )
        if (source := _source_for_field(observations, field)) is not None
    }


def _source_for_field(observations: list[IncomeEventObservation], field: str) -> str | None:
    for item in observations:
        if getattr(item, field) not in {None, ""}:
            return item.source
    return None


def _amount_bucket(value: Decimal | None) -> str:
    return str((value or Decimal("0")).quantize(Decimal("0.00000001")))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_value).strip().upper()


__all__ = ["canonical_event_type", "resolve_income_events"]
