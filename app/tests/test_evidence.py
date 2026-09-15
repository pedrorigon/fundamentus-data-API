from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.evidence import (
    ConsensusStatus,
    SourceObservation,
    normalize_unit,
    resolve_consensus,
)


def observation(
    value: str,
    source: str,
    *,
    as_of: date | None = date(2026, 7, 30),
    unit: str = "BRL",
    origin: str | None = None,
) -> SourceObservation:
    return SourceObservation(
        value=Decimal(value),
        source=source,
        as_of=as_of,
        unit=unit,
        independent_origin=origin,
    )


def test_two_sources_outvote_one_outlier_without_transitive_chaining() -> None:
    result = resolve_consensus(
        [
            observation("100", "fundamentus"),
            observation("101", "status_invest"),
            observation("150", "cvm"),
        ],
        relative_tolerance=Decimal("0.02"),
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("100.5")
    assert result.independent_sources == ("fundamentus", "status_invest")
    assert {item.source for item in result.observations} == {
        "fundamentus",
        "status_invest",
        "cvm",
    }
    assert [item.source for item in result.rejected_observations] == ["cvm"]


def test_equally_supported_overlapping_groups_are_reported_as_conflict() -> None:
    observations = [
        observation("0", "source_a"),
        observation("3", "source_b"),
        observation("4", "source_c"),
        observation("7", "source_d"),
    ]
    result = resolve_consensus(
        observations,
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("4"),
    )
    reverse = resolve_consensus(
        reversed(observations),
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("4"),
    )

    assert result.status is ConsensusStatus.conflict
    assert result.value is None
    assert result.independent_sources == ("source_a", "source_b", "source_c", "source_d")
    assert result.model_dump() == reverse.model_dump()


def test_same_origin_duplicate_samples_are_deterministic_and_equal_weighted() -> None:
    observations = [
        observation("0", "adapter_a_first", origin="origin_a"),
        observation("0", "adapter_a_second", origin="origin_a"),
        observation("3", "source_b", origin="origin_b"),
        observation("3", "source_c", origin="origin_c"),
    ]

    forward = resolve_consensus(
        observations,
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("5"),
    )
    reverse = resolve_consensus(
        reversed(observations),
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("5"),
    )

    assert forward.status is ConsensusStatus.consensus
    assert forward.value == Decimal("3")
    assert forward.independent_sources == ("origin_a", "origin_b", "origin_c")
    assert forward.model_dump() == reverse.model_dump()


def test_same_origin_conflicting_samples_are_excluded_from_voting() -> None:
    result = resolve_consensus(
        [
            observation("0", "adapter_a_low", origin="origin_a"),
            observation("6", "adapter_a_high", origin="origin_a"),
            observation("3", "source_b", origin="origin_b"),
            observation("4", "source_c", origin="origin_c"),
        ],
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("5"),
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("3.5")
    assert result.independent_sources == ("origin_b", "origin_c")
    assert {item.origin for item in result.rejected_observations} == {"origin_a"}


def test_internally_conflicted_origin_cannot_choose_a_duplicate_branch() -> None:
    result = resolve_consensus(
        [
            observation("0", "adapter_a_low", origin="origin_a"),
            observation("10", "adapter_a_high", origin="origin_a"),
            observation("0", "source_b", origin="origin_b"),
            observation("0", "source_c", origin="origin_c"),
        ],
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("1"),
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("0")
    assert result.independent_sources == ("origin_b", "origin_c")
    assert {item.origin for item in result.rejected_observations} == {"origin_a"}


def test_internally_conflicted_origin_does_not_manufacture_two_source_consensus() -> None:
    result = resolve_consensus(
        [
            observation("0", "adapter_a_low", origin="origin_a"),
            observation("10", "adapter_a_high", origin="origin_a"),
            observation("0", "source_b", origin="origin_b"),
        ],
        relative_tolerance=Decimal("0"),
        absolute_tolerance=Decimal("1"),
    )

    assert result.status is ConsensusStatus.single_source
    assert result.value == Decimal("0")
    assert result.independent_sources == ("origin_b",)
    assert {item.origin for item in result.rejected_observations} == {"origin_a"}


def test_tied_clusters_are_not_presented_as_a_value() -> None:
    result = resolve_consensus(
        [
            observation("100", "fundamentus"),
            observation("101", "status_invest"),
            observation("120", "cvm"),
            observation("121", "b3"),
        ],
        relative_tolerance=Decimal("0.02"),
    )

    assert result.status is ConsensusStatus.conflict
    assert result.value is None


def test_single_source_is_explicit_and_missing_is_distinct() -> None:
    single = resolve_consensus([observation("10", "fundamentus")])
    missing = resolve_consensus([])

    assert single.status is ConsensusStatus.single_source
    assert single.value == Decimal("10")
    assert missing.status is ConsensusStatus.missing_data
    assert missing.value is None


def test_implausible_and_mismatched_dates_do_not_create_consensus() -> None:
    implausible = resolve_consensus(
        [observation("-1", "fundamentus"), observation("10", "status_invest")],
        valid_range=(Decimal("0"), Decimal("100")),
    )
    stale = resolve_consensus(
        [
            observation("10", "fundamentus", as_of=date(2026, 1, 1)),
            observation("10", "status_invest", as_of=date(2026, 7, 30)),
        ],
    )

    assert implausible.status is ConsensusStatus.single_source
    assert implausible.value == Decimal("10")
    assert [item.value for item in implausible.observations] == [Decimal("-1"), Decimal("10")]
    assert [item.value for item in implausible.rejected_observations] == [Decimal("-1")]
    assert stale.status is ConsensusStatus.conflict
    assert stale.value is None


def test_filtered_observations_remain_auditable_when_a_valid_vote_exists() -> None:
    result = resolve_consensus(
        [
            observation("10", "fundamentus"),
            observation("10", "status_invest", unit="USD"),
        ],
        expected_unit="BRL",
    )

    assert result.status is ConsensusStatus.single_source
    assert result.value == Decimal("10")
    assert [item.source for item in result.observations] == ["fundamentus", "status_invest"]
    assert [item.source for item in result.rejected_observations] == ["status_invest"]


def test_all_filtered_observations_are_explicitly_rejected_and_order_independent() -> None:
    observations = [
        observation("-1", "fundamentus"),
        observation("10", "status_invest", unit="USD"),
    ]

    forward = resolve_consensus(
        observations,
        expected_unit="BRL",
        valid_range=(Decimal("0"), Decimal("100")),
    )
    reverse = resolve_consensus(
        reversed(observations),
        expected_unit="BRL",
        valid_range=(Decimal("0"), Decimal("100")),
    )

    assert forward.status is ConsensusStatus.invalid_data
    assert forward.value is None
    assert [item.source for item in forward.observations] == ["fundamentus", "status_invest"]
    assert [item.source for item in forward.rejected_observations] == [
        "fundamentus",
        "status_invest",
    ]
    assert forward.model_dump() == reverse.model_dump()


def test_correlated_mirrors_count_as_one_independent_origin() -> None:
    result = resolve_consensus(
        [
            observation("10", "mirror_a", origin="upstream"),
            observation("10.01", "mirror_b", origin="upstream"),
        ]
    )

    assert result.status is ConsensusStatus.single_source
    assert result.independent_sources == ("upstream",)


def test_repeated_observations_cannot_outvote_independent_origins() -> None:
    result = resolve_consensus(
        [
            observation("100", "mirror_a1", origin="upstream-a"),
            observation("100", "mirror_a2", origin="upstream-a"),
            observation("100", "mirror_a3", origin="upstream-a"),
            observation("110", "source_b", origin="upstream-b"),
            observation("110", "source_c", origin="upstream-c"),
        ]
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("110")
    assert result.independent_sources == ("upstream-b", "upstream-c")


def test_per_origin_representatives_prevent_duplicate_weighting() -> None:
    result = resolve_consensus(
        [
            observation("100", "source_a1", origin="upstream-a"),
            observation("100", "source_a2", origin="upstream-a"),
            observation("100", "source_a3", origin="upstream-a"),
            observation("102", "source_b", origin="upstream-b"),
            observation("102", "source_c", origin="upstream-c"),
        ]
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("102")


def test_repeated_observations_do_not_break_an_independent_source_tie() -> None:
    result = resolve_consensus(
        [
            observation("100", "source_a", origin="upstream-a"),
            observation("100", "source_b", origin="upstream-b"),
            observation("110", "source_c", origin="upstream-c"),
            observation("110.5", "source_d1", origin="upstream-d"),
            observation("110.5", "source_d2", origin="upstream-d"),
        ]
    )

    assert result.status is ConsensusStatus.conflict
    assert result.value is None
    assert result.independent_sources == (
        "upstream-a",
        "upstream-b",
        "upstream-c",
        "upstream-d",
    )


def test_units_are_normalized_before_comparison() -> None:
    result = resolve_consensus(
        [
            SourceObservation(value=Decimal("10"), source="a", unit="R$"),
            SourceObservation(value=Decimal("10.01"), source="b", unit="brl"),
        ],
        expected_unit="BRL",
    )

    assert result.status is ConsensusStatus.consensus
    assert result.value == Decimal("10.005")


def test_observation_normalizes_lineage_and_rejects_empty_sources() -> None:
    value = SourceObservation(
        value=Decimal("10"),
        source=" primary ",
        source_lineage=(" fundamentus ", "", "status_invest"),
        independent_origin=" root ",
        unit=" percentage ",
    )

    assert value.source == "primary"
    assert value.source_lineage == ("fundamentus", "status_invest")
    assert value.independent_origin == "root"
    assert value.unit == "percent"
    assert value.origin == "root"
    assert value.lineage == value.source_lineage
    with pytest.raises(ValidationError):
        SourceObservation(value=Decimal("NaN"), source="source")
    with pytest.raises(ValidationError):
        SourceObservation(value=Decimal("1"), source=" ")


def test_consensus_rejects_invalid_configuration_and_reports_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="tolerances"):
        resolve_consensus([], relative_tolerance=Decimal("-0.1"))
    with pytest.raises(ValueError, match="max_date_gap_days"):
        resolve_consensus([], max_date_gap_days=-1)
    with pytest.raises(ValueError, match="independent"):
        resolve_consensus([], minimum_independent_sources=0)

    range_result = resolve_consensus(
        [observation("-1", "source")], valid_range=(Decimal("0"), Decimal("10"))
    )
    unit_result = resolve_consensus([observation("1", "source", unit="USD")], expected_unit="BRL")
    finite_result = resolve_consensus([observation("1", "source", as_of=None)], expected_unit="BRL")

    assert range_result.status is ConsensusStatus.invalid_data
    assert unit_result.status is ConsensusStatus.invalid_data
    assert finite_result.status is ConsensusStatus.single_source


def test_consensus_honors_custom_tolerances_and_minimum_votes() -> None:
    observations = [
        observation("10", "a", as_of=date(2026, 7, 30)),
        observation("10.01", "b", as_of=date(2026, 7, 30)),
    ]

    strict = resolve_consensus(observations, relative_tolerance=Decimal("0"))
    three_votes = resolve_consensus(observations, minimum_independent_sources=3)
    stale = resolve_consensus(
        [
            observation("10", "a", as_of=date(2026, 7, 30)),
            observation("10", "b", as_of=date(2026, 7, 1)),
        ],
        max_date_gap_days=timedelta(days=1).days,
    )

    assert strict.status is ConsensusStatus.conflict
    assert three_votes.status is ConsensusStatus.single_source
    assert stale.status is ConsensusStatus.conflict


def test_consensus_handles_empty_units_invalid_values_and_source_lineage() -> None:
    assert normalize_unit("   ") is None

    lineage = SourceObservation(
        value=Decimal("10"),
        source="adapter",
        source_lineage=("upstream",),
    )
    assert lineage.origin == "upstream"
    with pytest.raises(ValueError, match="observation value must be finite"):
        SourceObservation.finite_value(Decimal("NaN"))

    malformed = SourceObservation.model_construct(value=Decimal("NaN"), source="broken")
    invalid = resolve_consensus([malformed])
    assert invalid.status is ConsensusStatus.invalid_data

    units = resolve_consensus(
        [
            observation("10", "brl", unit="BRL"),
            observation("10", "usd", unit="USD"),
        ]
    )
    assert units.status is ConsensusStatus.conflict
