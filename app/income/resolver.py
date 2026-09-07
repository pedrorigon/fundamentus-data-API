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
GENERIC_AMOUNT_TOLERANCE = Decimal("0.00005")
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
    typed: list[tuple[IncomeEventObservation, str]] = []
    for item in candidates:
        assert item.ex_date is not None
        event_type = canonical_event_type(item.event_type)
        if event_type == "Provento":
            generic.append(item)
            continue
        typed.append((item, event_type))
    for item, event_type in sorted(typed, key=_typed_observation_order):
        grouped[_typed_group_key(grouped, item, event_type)].append(item)
    for item in generic:
        assert item.ex_date is not None
        base = (item.ticker.upper(), item.ex_date, _amount_bucket(item.unit_price))
        compatible = [
            key
            for key in grouped
            if (key[0], key[2]) == base[:2]
            and _amounts_compatible(item.unit_price, Decimal(key[3]))
        ]
        key = compatible[0] if len(compatible) == 1 else (base[0], "Provento", base[1], base[2])
        grouped[key].append(item)
    resolved = [
        event for key, group in grouped.items() for event in _resolve_occurrences(key, group)
    ]
    return sorted(
        resolved,
        key=lambda item: (item.ticker, item.payment_date, item.event_type, item.event_id),
    )


def _typed_observation_order(
    entry: tuple[IncomeEventObservation, str],
) -> tuple[str, date, str, int, int, str, str]:
    item, event_type = entry
    assert item.ex_date is not None
    return (
        item.ticker.upper(),
        item.ex_date,
        event_type,
        -item.authority,
        -item.source_version,
        item.lineage,
        item.source_event_id,
    )


def _typed_group_key(
    grouped: dict[tuple[str, str, date, str], list[IncomeEventObservation]],
    item: IncomeEventObservation,
    event_type: str,
) -> tuple[str, str, date, str]:
    assert item.ex_date is not None
    exact = (item.ticker.upper(), event_type, item.ex_date, _amount_bucket(item.unit_price))
    if exact in grouped:
        return exact
    compatible = [
        key
        for key, members in grouped.items()
        if key[:3] == exact[:3]
        and _amounts_compatible(item.unit_price, Decimal(key[3]))
        and all(member.lineage != item.lineage for member in members)
    ]
    return compatible[0] if len(compatible) == 1 else exact


def _resolve_occurrences(
    key: tuple[str, str, date, str],
    observations: list[IncomeEventObservation],
) -> list[CanonicalIncomeEvent]:
    observations, unresolved_revision = _reconcile_payment_revisions(observations)
    if unresolved_revision:
        return [_resolve_group(key, observations)]
    dates_by_lineage: dict[str, set[date]] = defaultdict(set)
    for item in observations:
        assert item.payment_date is not None
        dates_by_lineage[item.lineage].add(item.payment_date)
    if not any(len(payment_dates) > 1 for payment_dates in dates_by_lineage.values()):
        return [_resolve_group(key, observations)]

    occurrences: dict[date, list[IncomeEventObservation]] = defaultdict(list)
    for item in observations:
        assert item.payment_date is not None
        occurrences[item.payment_date].append(item)
    return [
        _resolve_group(key, occurrence) for _payment_date, occurrence in sorted(occurrences.items())
    ]


def _reconcile_payment_revisions(
    observations: list[IncomeEventObservation],
) -> tuple[list[IncomeEventObservation], bool]:
    """Discard superseded dates only when an independent lineage confirms the revision."""
    active = [
        item
        for item in observations
        if item.source_status.lower() not in {"cancelled", "canceled"}
        and item.payment_date is not None
    ]
    support: dict[date, set[str]] = defaultdict(set)
    by_lineage: dict[str, list[IncomeEventObservation]] = defaultdict(list)
    for item in active:
        assert item.payment_date is not None
        support[item.payment_date].add(item.lineage)
        by_lineage[item.lineage].append(item)

    superseded_ids: set[int] = set()
    unresolved_revision = False
    for lineage, lineage_items in by_lineage.items():
        versions = {item.source_version for item in lineage_items}
        if len(versions) < 2:
            continue
        latest_version = max(versions)
        latest_dates = {
            item.payment_date
            for item in lineage_items
            if item.source_version == latest_version and item.payment_date is not None
        }
        revised_items = [
            item
            for item in lineage_items
            if item.source_version < latest_version and item.payment_date not in latest_dates
        ]
        for item in revised_items:
            assert item.payment_date is not None
            revised_date_confirmed = any(
                any(other != lineage for other in support[payment_date])
                for payment_date in latest_dates
            )
            old_date_confirmed = any(other != lineage for other in support[item.payment_date])
            if revised_date_confirmed and not old_date_confirmed:
                superseded_ids.add(id(item))
            elif not old_date_confirmed:
                unresolved_revision = True

    reconciled = [item for item in observations if id(item) not in superseded_ids]
    return reconciled, unresolved_revision


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
    authoritative = [item for item in active if item.authority >= OFFICIAL_AUTHORITY]
    payment = _best_payment_date(ordered, authoritative)
    amount = _best_value(ordered, "unit_price")
    assert isinstance(payment, date)
    assert isinstance(amount, Decimal)
    isin = _best_value(ordered, "isin")
    reference_period = _best_value(ordered, "reference_period")
    sources = sorted({item.source for item in observations})
    lineages = {item.lineage for item in active}
    status = _status(observations, authoritative, lineages)
    confidence = _confidence(status)
    field_source = _field_sources(ordered)
    event_type_source = _event_type_source(ordered, event_type)
    if event_type_source is not None:
        field_source["event_type"] = event_type_source
    field_confidence = {field: confidence for field in field_source}
    field_confidence["event_type"] = _event_type_confidence(ordered, event_type)
    identity = "|".join(
        (ticker, event_type, ex_date.isoformat(), payment.isoformat(), _amount_bucket(amount))
    )
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
        field_confidence=field_confidence,
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
        return (
            IncomeEventStatus.corroborated
            if _secondary_payment_consensus(observations) is not None
            else IncomeEventStatus.conflicted
        )
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


def _best_payment_date(
    observations: list[IncomeEventObservation],
    authoritative: list[IncomeEventObservation],
) -> date | None:
    if not authoritative and (consensus := _secondary_payment_consensus(observations)) is not None:
        return consensus
    value = _best_value(observations, "payment_date")
    return value if isinstance(value, date) else None


def _secondary_payment_consensus(
    observations: list[IncomeEventObservation],
) -> date | None:
    support: dict[date, set[str]] = defaultdict(set)
    for item in observations:
        if item.payment_date is not None and item.source_status.lower() not in {
            "cancelled",
            "canceled",
        }:
            support[item.payment_date].add(item.lineage)
    if not support:
        return None
    best = max(len(lineages) for lineages in support.values())
    winners = [payment for payment, lineages in support.items() if len(lineages) == best]
    return winners[0] if best >= 2 and len(winners) == 1 else None


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


def _event_type_source(
    observations: list[IncomeEventObservation],
    event_type: str,
) -> str | None:
    return next(
        (
            item.source
            for item in observations
            if canonical_event_type(item.event_type) == event_type
        ),
        None,
    )


def _event_type_confidence(
    observations: list[IncomeEventObservation],
    event_type: str,
) -> IncomeFieldConfidence:
    supporting = [
        item for item in observations if canonical_event_type(item.event_type) == event_type
    ]
    if any(item.authority >= OFFICIAL_AUTHORITY for item in supporting):
        return IncomeFieldConfidence.authoritative
    if len({item.lineage for item in supporting}) >= 2:
        return IncomeFieldConfidence.corroborated
    return IncomeFieldConfidence.tentative


def _amounts_compatible(first: Decimal | None, second: Decimal | None) -> bool:
    return (
        first is not None
        and second is not None
        and abs(first - second) <= GENERIC_AMOUNT_TOLERANCE
    )


def _amount_bucket(value: Decimal | None) -> str:
    return str((value or Decimal("0")).quantize(Decimal("0.00000001")))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", ascii_value).strip().upper()


__all__ = ["canonical_event_type", "resolve_income_events"]
