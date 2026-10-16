"""Deterministic reconciliation of observations from independent sources.

The resolver intentionally works on already-normalized observations.  Scrapers
remain responsible for parsing and identifying their own data; this module only
decides whether comparable observations support a value.  Keeping that rule
pure makes it safe to reuse in HTTP requests, scheduled jobs and tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_UNIT_ALIASES = {
    "r$": "BRL",
    "brl": "BRL",
    "real": "BRL",
    "reais": "BRL",
    "usd": "USD",
    "us$": "USD",
    "%": "percent",
    "pct": "percent",
    "percentage": "percent",
    "percent": "percent",
    "multiple": "multiple",
    "shares": "shares",
}


def normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().split()).casefold()
    if not normalized:
        return None
    return _UNIT_ALIASES.get(normalized, normalized)


class ConsensusStatus(StrEnum):
    """Outcome of reconciling observations for one normalized field."""

    consensus = "consensus"
    single_source = "single_source"
    conflict = "conflict"
    missing_data = "missing_data"
    invalid_data = "invalid_data"


class SourceObservation(BaseModel):
    """One normalized value obtained from one source.

    ``source`` identifies the adapter shown to callers.  ``source_lineage``
    records upstream dependencies, while ``independent_origin`` identifies the
    root that counts as one vote.  This prevents mirrors of the same provider
    from inflating a consensus.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal
    source: str = Field(min_length=1)
    as_of: date | None = None
    unit: str | None = None
    source_lineage: tuple[str, ...] = ()
    independent_origin: str | None = None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("observation value must be finite")
        return value

    @field_validator("source_lineage")
    @classmethod
    def clean_lineage(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.strip() for item in value if item and item.strip())

    @field_validator("source")
    @classmethod
    def clean_required_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source must not be empty")
        return normalized

    @field_validator("independent_origin")
    @classmethod
    def clean_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("unit")
    @classmethod
    def canonical_unit(cls, value: str | None) -> str | None:
        return normalize_unit(value)

    @property
    def origin(self) -> str:
        """Return the independent vote identity for this observation."""

        if self.independent_origin:
            return self.independent_origin
        if self.source_lineage:
            return self.source_lineage[0]
        return self.source

    @property
    def lineage(self) -> tuple[str, ...]:
        """Compatibility alias for callers that use the shorter name."""

        return self.source_lineage


class ConsensusResult(BaseModel):
    """Auditable result of the source reconciliation rule."""

    model_config = ConfigDict(frozen=True)

    value: Decimal | None = None
    as_of: date | None = None
    sources: tuple[str, ...] = ()
    independent_sources: tuple[str, ...] = ()
    source_lineage: tuple[str, ...] = ()
    status: ConsensusStatus = ConsensusStatus.missing_data
    reason: str
    confidence: Decimal = Decimal("0")
    observations: tuple[SourceObservation, ...] = ()
    rejected_observations: tuple[SourceObservation, ...] = ()


def resolve_consensus(
    observations: Iterable[SourceObservation],
    *,
    expected_unit: str | None = None,
    relative_tolerance: Decimal = Decimal("0.02"),
    absolute_tolerance: Decimal = Decimal("0"),
    max_date_gap_days: int = 31,
    valid_range: tuple[Decimal, Decimal] | None = None,
    minimum_independent_sources: int = 2,
) -> ConsensusResult:
    """Resolve a field using bounded, non-transitive concordance clusters.

    Values are sorted before constructing the compatibility graph.  Every
    maximal complete-link group is enumerated exactly, so ``100`` ~ ``104``
    and ``104`` ~ ``108`` cannot manufacture a ``100`` ~ ``108`` vote, and
    equally supported overlapping groups cannot be hidden by input order.  The
    winning group must contain the configured number of distinct independent
    origins.  A lone, compatible source is returned explicitly as
    ``single_source``; competing groups are reported as ``conflict``.  An
    origin whose own observations are materially incompatible is excluded from
    voting entirely, so one unstable adapter cannot choose the duplicate that
    happens to fit the other sources best.
    Clusters are ranked only by independent-origin count. Repeated observations
    from one origin are retained for auditability, but they never add votes or
    break a tie between equally supported source groups.
    """

    if relative_tolerance < 0 or absolute_tolerance < 0:
        raise ValueError("tolerances must be non-negative")
    if max_date_gap_days < 0:
        raise ValueError("max_date_gap_days must be non-negative")
    if minimum_independent_sources < 1:
        raise ValueError("minimum_independent_sources must be positive")

    # Keep a deterministic copy of the complete input set before applying
    # eligibility filters.  A rejected provider value is still useful evidence
    # when callers investigate why a vote was not used.
    candidates = sorted(observations, key=_observation_sort_key)
    expected_unit = normalize_unit(expected_unit)
    valid: list[SourceObservation] = []
    pre_rejected: list[SourceObservation] = []
    invalid_count = 0
    unit_mismatch_count = 0
    range_mismatch_count = 0
    for observation in candidates:
        if not observation.value.is_finite():
            invalid_count += 1
            pre_rejected.append(observation)
            continue
        if expected_unit is not None and observation.unit != expected_unit:
            unit_mismatch_count += 1
            pre_rejected.append(observation)
            continue
        if valid_range is not None:
            lower, upper = valid_range
            if observation.value < lower or observation.value > upper:
                range_mismatch_count += 1
                pre_rejected.append(observation)
                continue
        valid.append(observation)

    if not valid:
        if range_mismatch_count:
            reason = "All observations were outside the plausible range"
            status = ConsensusStatus.invalid_data
        elif unit_mismatch_count:
            reason = "No observations used the expected unit"
            status = ConsensusStatus.invalid_data
        elif invalid_count or candidates:
            reason = "No finite observations were available"
            status = ConsensusStatus.invalid_data
        else:
            reason = "No observations were available"
            status = ConsensusStatus.missing_data
        # No value can be projected.  Expose all candidates both as the raw
        # evidence set and as rejected observations so the public contract
        # remains explicit even when every provider response is unusable.
        return ConsensusResult(
            status=status,
            reason=reason,
            observations=tuple(candidates),
            rejected_observations=tuple(candidates),
        )

    valid.sort(key=_observation_sort_key)
    conflicted_origins = _internally_conflicted_origins(
        valid,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
        max_date_gap_days=max_date_gap_days,
    )
    voting_observations = [item for item in valid if item.origin not in conflicted_origins]
    if not voting_observations:
        return ConsensusResult(
            status=ConsensusStatus.conflict,
            reason="Independent source observations conflict internally",
            sources=tuple(sorted({item.source for item in valid})),
            independent_sources=tuple(sorted({item.origin for item in valid})),
            source_lineage=tuple(sorted({line for item in valid for line in item.source_lineage})),
            observations=tuple(candidates),
            rejected_observations=tuple(pre_rejected),
        )

    clusters = _maximal_compatible_groups(
        voting_observations,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
        max_date_gap_days=max_date_gap_days,
    )
    ranked = sorted(clusters, key=_cluster_sort_key)
    winner = ranked[0]
    winner_origins = {item.origin for item in winner}
    winner_source_count = len(winner_origins)
    top_rank = _rank(winner)
    top_signatures = {
        _cluster_support_signature(cluster) for cluster in ranked if _rank(cluster) == top_rank
    }
    other_top_tie = len(top_signatures) > 1
    all_sources = tuple(sorted({item.source for item in winner}))
    independent_sources = tuple(sorted(winner_origins))
    lineage = tuple(sorted({line for item in winner for line in item.source_lineage}))

    if other_top_tie or (len(ranked) > 1 and winner_source_count < minimum_independent_sources):
        return ConsensusResult(
            status=ConsensusStatus.conflict,
            reason="Independent sources disagree without a clear majority",
            sources=tuple(sorted({item.source for item in valid})),
            independent_sources=tuple(sorted({item.origin for item in voting_observations})),
            source_lineage=tuple(sorted({line for item in valid for line in item.source_lineage})),
            observations=tuple(candidates),
            rejected_observations=tuple(pre_rejected),
        )

    if winner_source_count < minimum_independent_sources:
        return ConsensusResult(
            value=_representative(winner),
            as_of=_latest_date(winner),
            sources=all_sources,
            independent_sources=independent_sources,
            source_lineage=lineage,
            status=ConsensusStatus.single_source,
            reason="Only one independent source was available",
            confidence=Decimal("0.55"),
            observations=tuple(candidates),
            rejected_observations=_rejected_observations(
                valid,
                winner,
                conflicted_origins,
                pre_rejected=pre_rejected,
            ),
        )

    return ConsensusResult(
        value=_representative(winner),
        as_of=_latest_date(winner),
        sources=all_sources,
        independent_sources=independent_sources,
        source_lineage=lineage,
        status=ConsensusStatus.consensus,
        reason="Independent sources agree",
        confidence=min(Decimal("1"), Decimal(winner_source_count) / Decimal("3")),
        observations=tuple(candidates),
        rejected_observations=_rejected_observations(
            valid,
            winner,
            conflicted_origins,
            pre_rejected=pre_rejected,
        ),
    )


def _compatible(
    left: SourceObservation,
    right: SourceObservation,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
    max_date_gap_days: int,
) -> bool:
    if left.unit != right.unit:
        return False
    if left.as_of is not None and right.as_of is not None:
        if abs(left.as_of - right.as_of) > timedelta(days=max_date_gap_days):
            return False
    tolerance = max(
        absolute_tolerance,
        max(abs(left.value), abs(right.value)) * relative_tolerance,
    )
    return abs(left.value - right.value) <= tolerance


def _observation_sort_key(
    observation: SourceObservation,
) -> tuple[int, Decimal | str, str, str, date, str, tuple[str, ...], str]:
    """Return a stable ordering for graph nodes and serialized results."""

    # ``model_construct`` is intentionally supported by the resolver's
    # defensive path, so a malformed non-finite Decimal must not make sorting
    # raise ``InvalidOperation``.  Finite values retain numeric ordering for
    # consensus ranking; invalid values are grouped deterministically by text.
    value = observation.value
    value_key: tuple[int, Decimal | str] = (0, value) if value.is_finite() else (1, str(value))
    return (
        *value_key,
        observation.source,
        observation.origin,
        observation.as_of or date.min,
        observation.unit or "",
        observation.source_lineage,
        observation.independent_origin or "",
    )


def _maximal_compatible_groups(
    observations: list[SourceObservation],
    *,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
    max_date_gap_days: int,
) -> list[list[SourceObservation]]:
    """Enumerate every maximal pairwise-compatible group deterministically.

    Each observation is a vertex and each compatible pair is an edge.  A
    complete-link cluster is therefore a clique in this graph.  The iterative
    Bron–Kerbosch traversal enumerates maximal cliques exactly, while keeping
    recursion depth and source-order effects out of this financial rule.
    Provider field responses contain only a small number of observations, so
    exact enumeration is preferable to a greedy approximation here.
    """

    if not observations:
        return []

    neighbors = tuple(
        frozenset(
            other_index
            for other_index, other in enumerate(observations)
            if observation_index != other_index
            and _compatible(
                observation,
                other,
                absolute_tolerance,
                relative_tolerance,
                max_date_gap_days,
            )
        )
        for observation_index, observation in enumerate(observations)
    )
    pending: list[tuple[frozenset[int], frozenset[int], frozenset[int]]] = [
        (frozenset(), frozenset(range(len(observations))), frozenset())
    ]
    cliques: set[frozenset[int]] = set()

    while pending:
        clique, candidates, excluded = pending.pop()
        if not candidates:
            if not excluded:
                cliques.add(clique)
            continue

        pivot_pool = candidates | excluded
        pivot = min(
            pivot_pool,
            key=lambda index: (-len(candidates & neighbors[index]), index),
        )
        branch = sorted(candidates - neighbors[pivot], reverse=True)
        remaining_candidates = candidates
        remaining_excluded = excluded
        for vertex in branch:
            pending.append(
                (
                    clique | {vertex},
                    remaining_candidates & neighbors[vertex],
                    remaining_excluded & neighbors[vertex],
                )
            )
            remaining_candidates = remaining_candidates - {vertex}
            remaining_excluded = remaining_excluded | {vertex}

    return [
        [observations[index] for index in sorted(clique)]
        for clique in sorted(cliques, key=lambda item: tuple(sorted(item)))
    ]


def _internally_conflicted_origins(
    observations: list[SourceObservation],
    *,
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
    max_date_gap_days: int,
) -> set[str]:
    """Return origins whose observations cannot form one compatible group."""

    by_origin: dict[str, list[SourceObservation]] = {}
    for observation in observations:
        by_origin.setdefault(observation.origin, []).append(observation)

    conflicted: set[str] = set()
    for origin, source_observations in by_origin.items():
        if any(
            not _compatible(
                left,
                right,
                absolute_tolerance,
                relative_tolerance,
                max_date_gap_days,
            )
            for index, left in enumerate(source_observations)
            for right in source_observations[index + 1 :]
        ):
            conflicted.add(origin)
    return conflicted


def _rejected_observations(
    observations: list[SourceObservation],
    winner: list[SourceObservation],
    conflicted_origins: set[str],
    *,
    pre_rejected: Iterable[SourceObservation] = (),
) -> tuple[SourceObservation, ...]:
    rejected = [
        *pre_rejected,
        *(item for item in observations if item.origin in conflicted_origins or item not in winner),
    ]
    return tuple(
        sorted(
            rejected,
            key=_observation_sort_key,
        )
    )


def _cluster_sort_key(
    cluster: list[SourceObservation],
) -> tuple[
    int,
    Decimal,
    tuple[tuple[int, Decimal | str, str, str, date, str, tuple[str, ...], str], ...],
]:
    return (
        -_rank(cluster),
        _representative(cluster),
        tuple(_observation_sort_key(item) for item in cluster),
    )


def _cluster_support_signature(cluster: list[SourceObservation]) -> tuple[tuple[str, ...], Decimal]:
    """Describe the effective vote group, ignoring repeated adapter samples."""

    return tuple(sorted({item.origin for item in cluster})), _representative(cluster)


def _rank(cluster: list[SourceObservation]) -> int:
    """Count independent source votes without rewarding repeated samples."""

    return len({item.origin for item in cluster})


def _representative(cluster: list[SourceObservation]) -> Decimal:
    """Return a median where every independent origin has equal weight.

    Adapters may emit multiple observations for the same upstream origin.  A
    median over raw observations would allow that origin to move the result by
    repeating its value.  Collapse each origin to its own deterministic median
    first, then calculate the cluster median over those one-vote values.
    """

    values_by_origin: dict[str, list[Decimal]] = {}
    for item in cluster:
        values_by_origin.setdefault(item.origin, []).append(item.value)
    origin_values = [_median(sorted(values)) for _, values in sorted(values_by_origin.items())]
    return _median(sorted(origin_values))


def _median(values: list[Decimal]) -> Decimal:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / Decimal("2")


def _latest_date(cluster: list[SourceObservation]) -> date | None:
    return max((item.as_of for item in cluster if item.as_of is not None), default=None)
