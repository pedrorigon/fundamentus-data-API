"""Put confirmed fund quota splits on a comparable per-quota basis.

The event register is deliberately small. A large share-count jump alone does
not authorize an adjustment; both the official event and the observed series
must agree. Raw provider observations remain unchanged outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.models import FundDistribution, FundMonthlyReport


@dataclass(frozen=True)
class ConfirmedQuotaSplit:
    ticker: str
    cnpj: str
    effective_date: date
    factor: Decimal
    source_url: str


@dataclass(frozen=True)
class NormalizedFundUnits:
    reports: list[FundMonthlyReport]
    distributions: list[FundDistribution]
    sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    uncertain_reports: bool = False
    uncertain_distributions: bool = False


_CONFIRMED_SPLITS = (
    ConfirmedQuotaSplit(
        ticker="ALZR11",
        cnpj="28737771000185",
        effective_date=date(2025, 5, 6),
        factor=Decimal("10"),
        source_url="https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento?cvm=true&id=907949",
    ),
    ConfirmedQuotaSplit(
        ticker="SNAG11",
        cnpj="28152777000190",
        effective_date=date(2023, 8, 2),
        factor=Decimal("10"),
        source_url="https://www.suno.com.br/asset/wp-content/uploads/2022/07/Fato-Relevante-SNAG11-Aprovacao-SPLIT.pdf",
    ),
)


def normalize_fund_units(
    ticker: str,
    cnpj: str | None,
    reports: list[FundMonthlyReport],
    distributions: list[FundDistribution],
) -> NormalizedFundUnits:
    """Adjust only confirmed, observed transitions and flag ambiguous units."""

    normalized_cnpj = "".join(character for character in cnpj or "" if character.isdigit())
    events = [
        event
        for event in _CONFIRMED_SPLITS
        if event.ticker == ticker.strip().upper() and event.cnpj == normalized_cnpj
    ]
    adjusted_reports = reports
    adjusted_distributions = distributions
    warnings: list[str] = []
    sources: list[str] = []
    uncertain_reports = False
    uncertain_distributions = False
    for event in events:
        report_transition, report_uncertain = _report_transition(adjusted_reports, event)
        distribution_transition, distribution_uncertain = _distribution_transition(
            adjusted_distributions, event
        )
        if report_transition is not None:
            adjusted_reports = [
                report.model_copy(
                    update={
                        "nav_per_share": report.nav_per_share / event.factor,
                        "issued_shares": (
                            report.issued_shares * event.factor
                            if report.issued_shares is not None
                            else None
                        ),
                    }
                )
                if report.as_of < report_transition
                else report
                for report in adjusted_reports
            ]
            adjusted_reports = _repair_transition_return(adjusted_reports, report_transition)
            sources.append(event.source_url)
            warnings.append(f"Quota units adjusted using confirmed split: {event.source_url}")
        elif report_uncertain:
            uncertain_reports = True
            warnings.append(f"Quota units could not be reconciled: {event.source_url}")
        if distribution_transition is not None:
            adjusted_distributions = [
                distribution.model_copy(update={"value": distribution.value / event.factor})
                if distribution.ex_date < distribution_transition
                else distribution
                for distribution in adjusted_distributions
            ]
            if event.source_url not in sources:
                sources.append(event.source_url)
            if report_transition is None:
                warnings.append(f"Quota units adjusted using confirmed split: {event.source_url}")
        elif distribution_uncertain:
            uncertain_distributions = True
            warnings.append(f"Distribution units could not be reconciled: {event.source_url}")
    if _unconfirmed_unit_jump(adjusted_reports):
        uncertain_reports = True
        uncertain_distributions = bool(adjusted_distributions)
        warnings.append("Fund quota units show an unverified discontinuity")
    return NormalizedFundUnits(
        reports=adjusted_reports,
        distributions=adjusted_distributions,
        sources=tuple(sources),
        warnings=tuple(warnings),
        uncertain_reports=uncertain_reports,
        uncertain_distributions=uncertain_distributions,
    )


def _unconfirmed_unit_jump(reports: list[FundMonthlyReport]) -> bool:
    for before, after in zip(reports, reports[1:], strict=False):
        if not before.issued_shares or not after.issued_shares:
            continue
        quantity_ratio = after.issued_shares / before.issued_shares
        nav_ratio = after.nav_per_share / before.nav_per_share
        if (
            quantity_ratio >= Decimal("3")
            and nav_ratio <= Decimal("0.4")
            and Decimal("0.75") <= quantity_ratio * nav_ratio <= Decimal("1.25")
        ):
            return True
    return False


def _report_transition(
    reports: list[FundMonthlyReport], event: ConfirmedQuotaSplit
) -> tuple[date | None, bool]:
    if len(reports) < 2 or reports[0].as_of >= event.effective_date:
        return None, False
    if reports[-1].as_of < event.effective_date - timedelta(days=35):
        return None, False
    candidates: list[date] = []
    suspicious = False
    for before, after in zip(reports, reports[1:], strict=False):
        if not _near_event(after.as_of, event.effective_date):
            continue
        nav_ratio = after.nav_per_share / before.nav_per_share
        if not _near_factor(nav_ratio, Decimal("1") / event.factor):
            continue
        suspicious = True
        if before.issued_shares and after.issued_shares:
            confirmed = _near_factor(after.issued_shares / before.issued_shares, event.factor)
        elif before.net_assets and after.net_assets:
            confirmed = Decimal("0.75") <= after.net_assets / before.net_assets <= Decimal("1.25")
        else:
            confirmed = False
        if confirmed:
            candidates.append(after.as_of)
    return (candidates[0] if len(candidates) == 1 else None), suspicious and len(candidates) != 1


def _distribution_transition(
    distributions: list[FundDistribution], event: ConfirmedQuotaSplit
) -> tuple[date | None, bool]:
    if len(distributions) < 2 or distributions[0].ex_date >= event.effective_date:
        return None, False
    candidates: list[date] = []
    suspicious = False
    for before, after in zip(distributions, distributions[1:], strict=False):
        if not _near_event(after.ex_date, event.effective_date):
            continue
        if before.value <= 0:
            continue
        if _near_factor(after.value / before.value, Decimal("1") / event.factor):
            suspicious = True
            candidates.append(after.ex_date)
    return (candidates[0] if len(candidates) == 1 else None), suspicious and len(candidates) != 1


def _repair_transition_return(
    reports: list[FundMonthlyReport], transition: date
) -> list[FundMonthlyReport]:
    for index, report in enumerate(reports):
        if report.as_of == transition and index > 0:
            prior_nav = reports[index - 1].nav_per_share
            if prior_nav > 0:
                repaired = report.model_copy(
                    update={"monthly_nav_return": report.nav_per_share / prior_nav - Decimal("1")}
                )
                return [*reports[:index], repaired, *reports[index + 1 :]]
    return reports


def _near_event(observed: date, effective: date) -> bool:
    return effective - timedelta(days=40) <= observed <= effective + timedelta(days=55)


def _near_factor(observed: Decimal, expected: Decimal) -> bool:
    return expected * Decimal("0.8") <= observed <= expected * Decimal("1.2")
