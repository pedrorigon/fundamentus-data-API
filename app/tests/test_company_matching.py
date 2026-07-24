from __future__ import annotations

from app.services.company_matching import match_company, normalize_company_name


def test_normalizes_legal_forms_and_accents() -> None:
    assert normalize_company_name("WEG S.A.") == normalize_company_name("WEG SA")
    assert normalize_company_name("MAGAZINE LUIZA S/A") == normalize_company_name(
        "MAGAZINE LUIZA S.A."
    )
    assert "BALCAO" in normalize_company_name("B3 S.A. - BRASIL, BOLSA, BALCÃO")


def test_expands_cvm_abbreviations() -> None:
    assert normalize_company_name("BCO BRADESCO S.A.") == normalize_company_name(
        "BANCO BRADESCO S.A."
    )


def test_name_made_only_of_noise_tokens_keeps_raw_tokens() -> None:
    assert normalize_company_name("CIA BRASIL SA") != ""


def test_matches_exact_normalized_name() -> None:
    match = match_company(
        "WEG S.A.",
        {"11111111000111": "WEG SA", "22222222000122": "VALE S.A."},
    )

    assert match is not None
    assert match.cnpj == "11111111000111"
    assert match.confidence == "high"


def test_matches_abbreviated_filing_name() -> None:
    match = match_company(
        "BANCO BRADESCO S.A.",
        {"60746948000112": "BCO BRADESCO S.A."},
    )

    assert match is not None
    assert match.cnpj == "60746948000112"


def test_matches_when_filing_name_has_extra_tokens() -> None:
    match = match_company(
        "ITAU UNIBANCO HOLDING S.A.",
        {"60872504000123": "ITAU UNIBANCO HOLDING S.A."},
    )

    assert match is not None
    assert match.confidence == "high"


def test_rejects_ambiguous_candidates() -> None:
    assert (
        match_company(
            "PETROLEO SA",
            {"1": "PETROLEO SA", "2": "PETROLEO SA"},
        )
        is None
    )


def test_rejects_unrelated_company_sharing_one_token() -> None:
    assert match_company("VALE S.A.", {"1": "VALE VERDE ENERGIA SA"}) is None


def test_returns_none_without_candidates() -> None:
    assert match_company("WEG S.A.", {}) is None


def test_returns_none_for_empty_name() -> None:
    assert match_company("", {"1": "WEG SA"}) is None


def test_partial_match_reports_medium_confidence() -> None:
    match = match_company(
        "PETROLEO BRASILEIRO S.A. PETROBRAS",
        {"33000167000101": "PETROLEO BRASILEIRO PETROBRAS DISTRIBUIDORA"},
    )

    if match is not None:
        assert match.confidence == "medium"


def test_rejects_two_candidates_with_equal_partial_overlap() -> None:
    assert (
        match_company(
            "ALFA BETA ENERGIA",
            {"1": "ALFA BETA ENERGIA GAMA", "2": "ALFA BETA ENERGIA DELTA"},
        )
        is None
    )


def test_ignores_candidate_whose_name_is_only_noise() -> None:
    match = match_company("WEG S.A.", {"1": "SA", "2": "WEG SA"})

    assert match is not None
    assert match.cnpj == "2"
