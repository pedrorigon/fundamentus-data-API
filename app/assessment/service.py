from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx

from app.assessment.models import (
    ASSESSMENT_TIMEZONE,
    AssessmentComponentState,
    AssessmentComponentStatus,
    AssessmentMode,
    AssessmentRunStatus,
    AssessmentSnapshotRequest,
    AssessmentSnapshotResponse,
)
from app.assessment.store import (
    AssessmentStore,
    ClaimResult,
)
from app.assessment.store import (
    BootstrapIdentityConflictError as BootstrapStoreIdentityConflictError,
)
from app.core.errors import APIError, InvalidTickerError
from app.models import (
    FundamentalsSnapshot,
    InstrumentMetadata,
    OpportunityResponse,
    QualityAssetFacts,
    QualityAssetKind,
    QualityAssetRequest,
    QualityFactsRequest,
)
from app.parsers.normalizers import normalize_ticker
from app.services.fundamentals import FundamentalsService
from app.services.opportunity import OpportunityService
from app.services.quality import QualityFactsService


class InvalidAssessmentPeriodError(APIError):
    status_code = 400
    code = "INVALID_ASSESSMENT_PERIOD"
    message = "Assessment period is outside the supported scheduler window."


class BootstrapIdentityConflictError(APIError):
    status_code = 409
    code = "BOOTSTRAP_IDENTITY_CONFLICT"
    message = "Bootstrap identity was already admitted with a different period."


T = TypeVar("T")
# Version 7 invalidates snapshots built before documented domestic ETF costs
# and dated fund assets became part of public quality evidence.
ASSESSMENT_EVIDENCE_VERSION = "v7"
_COMPONENT_ERRORS = (
    APIError,
    ArithmeticError,
    AttributeError,
    httpx.HTTPError,
    KeyError,
    OSError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True)
class _Component[T]:
    value: T | None
    state: AssessmentComponentState


def _now_local() -> datetime:
    """Return the wall clock used for scheduler-window validation."""

    return datetime.now(ASSESSMENT_TIMEZONE)


class AssessmentSnapshotService:
    """Build and publish one globally shareable assessment snapshot."""

    def __init__(
        self,
        store: AssessmentStore,
        opportunity: OpportunityService,
        fundamentals: FundamentalsService,
        quality: QualityFactsService,
        *,
        period_history_days: int = 370,
        retry_backoff_seconds: int = 15,
        bootstrap_retry_after_seconds: int = 2,
    ) -> None:
        self.store = store
        self.opportunity = opportunity
        self.fundamentals = fundamentals
        self.quality = quality
        self.period_history_days = max(1, period_history_days)
        self.retry_backoff_seconds = max(0, retry_backoff_seconds)
        self.bootstrap_retry_after_seconds = min(60, max(1, bootstrap_retry_after_seconds))
        self._bootstrap_wakeup: Callable[[], None] | None = None

    def set_bootstrap_wakeup(self, callback: Callable[[], None] | None) -> None:
        """Attach the lifecycle-managed worker's wake callback."""

        self._bootstrap_wakeup = callback

    async def resolve(self, request: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
        normalized = self._normalize_request(request)
        if normalized.mode is AssessmentMode.bootstrap:
            return await self._resolve_bootstrap(normalized)
        key = assessment_key(normalized)
        now = _now_local()
        self._validate_period(normalized.period_at, now=now, enforce_capture_window=False)
        if _assessment_window_state(normalized.period_at, now) == "closed":
            # A period is immutable after its capture window closes.  A
            # durable result remains readable inside retention, but a missing
            # row must never be backfilled with data observed later.
            existing = await self.store.get(key)
            if existing is not None:
                return self._claim_response(normalized, ClaimResult("existing", existing))
            raise InvalidAssessmentPeriodError(
                "Assessment period is outside the supported scheduler window."
            )
        claim = await self.store.claim(
            key=key,
            ticker=normalized.ticker,
            kind=normalized.kind.value,
            profile=None,
            venue=normalized.venue,
            period_at=normalized.period_at,
        )
        if claim.state != "claimed":
            return self._claim_response(normalized, claim)
        assert claim.token is not None
        assert claim.generation is not None
        response, _committed = await self._execute_claim(normalized, key, claim)
        return response

    async def execute_claim(
        self,
        request: AssessmentSnapshotRequest,
        key: str,
        claim: ClaimResult,
    ) -> tuple[AssessmentSnapshotResponse, bool]:
        """Build/publish one worker-owned claim off the HTTP request path."""

        return await self._execute_claim(request, key, claim)

    async def _resolve_bootstrap(
        self, request: AssessmentSnapshotRequest
    ) -> AssessmentSnapshotResponse:
        assert request.bootstrap_key is not None
        key = bootstrap_assessment_key(request)
        self._validate_bootstrap_period(request.period_at)
        try:
            admitted = await self.store.enqueue_bootstrap(
                key=key,
                bootstrap_key=request.bootstrap_key,
                ticker=request.ticker,
                kind=request.kind.value,
                venue=request.venue,
                corporate_name=request.corporate_name,
                period_at=request.period_at,
            )
        except BootstrapStoreIdentityConflictError as exc:
            # Do not expose the already-admitted timestamp or instrument
            # identity.  The caller can safely retry with the original
            # immutable input, but cannot use this response to probe another
            # client's snapshot.
            raise BootstrapIdentityConflictError() from exc

        record = admitted.record
        if record is not None and record.response() is not None:
            existing = self._bootstrap_response(request, record.response())
            if existing.status is AssessmentRunStatus.failed:
                existing = existing.model_copy(
                    update={"retry_after_seconds": _remaining_seconds(record.retry_at)}
                )
            return existing
        if admitted.state == "failed":
            return self._bootstrap_processing_response(
                request,
                period_at=admitted.work.period_at,
                status=AssessmentRunStatus.failed,
                error=admitted.work.error or "Bootstrap assessment failed.",
            )
        if admitted.state == "retry_wait":
            return self._bootstrap_processing_response(
                request,
                period_at=admitted.work.period_at,
                status=AssessmentRunStatus.failed,
                error=admitted.work.error or "Bootstrap assessment is waiting to retry.",
                retry_after_seconds=_remaining_seconds(admitted.work.retry_at),
            )
        response = self._bootstrap_processing_response(
            request,
            period_at=admitted.work.period_at,
            status=AssessmentRunStatus.processing,
            retry_after_seconds=self.bootstrap_retry_after_seconds,
        )
        if admitted.state == "queued" and self._bootstrap_wakeup is not None:
            self._bootstrap_wakeup()
        return response

    def _validate_bootstrap_period(self, period_at: datetime) -> None:
        if period_at.tzinfo is None or period_at.utcoffset() is None:
            raise InvalidAssessmentPeriodError("Assessment period must include a timezone offset.")
        current = _now_local().astimezone(UTC)
        normalized = period_at.astimezone(UTC)
        if normalized > current:
            raise InvalidAssessmentPeriodError("Assessment periods cannot be in the future.")
        if normalized.date() < current.date() - timedelta(days=self.period_history_days):
            raise InvalidAssessmentPeriodError("Assessment period is outside retention window.")

    def _bootstrap_response(
        self,
        request: AssessmentSnapshotRequest,
        response: AssessmentSnapshotResponse | None,
    ) -> AssessmentSnapshotResponse:
        if response is None:
            return self._bootstrap_processing_response(
                request,
                period_at=request.period_at,
                status=AssessmentRunStatus.processing,
                retry_after_seconds=self.bootstrap_retry_after_seconds,
            )
        return response.model_copy(
            update={
                "mode": AssessmentMode.bootstrap,
                "bootstrap_key": request.bootstrap_key,
            }
        )

    def _bootstrap_processing_response(
        self,
        request: AssessmentSnapshotRequest,
        *,
        period_at: datetime,
        status: AssessmentRunStatus,
        error: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> AssessmentSnapshotResponse:
        return AssessmentSnapshotResponse(
            mode=AssessmentMode.bootstrap,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            profile=None,
            venue=request.venue,
            period_at=period_at,
            status=status,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )

    def _normalize_request(self, request: AssessmentSnapshotRequest) -> AssessmentSnapshotRequest:
        normalized = request
        if normalized.profile is not None:
            # Profile is a caller hint retained for wire compatibility.  It
            # is not part of global evidence identity and must not select the
            # provider methodology used for this shared snapshot.
            normalized = normalized.model_copy(update={"profile": None})
        if normalized.kind is QualityAssetKind.fixed_income:
            return normalized
        try:
            ticker = normalize_ticker(normalized.ticker)
        except ValueError as exc:
            raise InvalidTickerError(ticker=normalized.ticker) from exc
        if ticker == normalized.ticker:
            return normalized
        return normalized.model_copy(update={"ticker": ticker})

    async def _execute_claim(
        self,
        request: AssessmentSnapshotRequest,
        key: str,
        claim: ClaimResult,
    ) -> tuple[AssessmentSnapshotResponse, bool]:
        assert claim.token is not None
        assert claim.generation is not None
        try:
            response = await self._build(request)
        # Provider adapters normalize their expected failures below, but an
        # ordinary programming or integration exception must still leave a
        # durable, sanitized failure for this claim. ``Exception`` excludes
        # ``CancelledError``/other control-flow ``BaseException`` values, so
        # shutdown and task cancellation continue to propagate normally.
        except Exception as exc:
            response = self._unexpected_failure(request, exc)
            saved = await self.store.fail(
                key=key,
                token=claim.token,
                generation=claim.generation,
                response=response,
                retry_after_seconds=self._retry_delay(claim.record.attempts),
            )
            return response, saved

        # A build with no usable component is a provider failure.  Route it
        # through ``fail`` so a transient outage cannot replace the durable
        # last-good snapshot with an empty completed result.
        if response.status is AssessmentRunStatus.failed:
            saved = await self.store.fail(
                key=key,
                token=claim.token,
                generation=claim.generation,
                response=response,
                retry_after_seconds=self._retry_delay(claim.record.attempts),
            )
            return response, saved

        published = await self.store.publish(
            key=key,
            token=claim.token,
            generation=claim.generation,
            response=response,
        )
        if published:
            return response, True
        # A lease can expire while an upstream request is in flight.  Never
        # return a stale worker's result as if it had been published.
        latest = await self.store.get(key)
        if latest is not None and latest.response() is not None:
            return latest.response(), False  # type: ignore[return-value]
        return self._in_progress_response(request), False

    def _validate_period(
        self,
        period_at: datetime,
        *,
        now: datetime | None = None,
        enforce_capture_window: bool = True,
    ) -> None:
        local = period_at.astimezone(ASSESSMENT_TIMEZONE)
        current = (now or _now_local()).astimezone(ASSESSMENT_TIMEZONE)
        if local > current:
            raise InvalidAssessmentPeriodError("Assessment periods cannot be in the future.")
        if local.date() < current.date() - timedelta(days=self.period_history_days):
            raise InvalidAssessmentPeriodError("Assessment period is outside retention window.")
        if enforce_capture_window and _assessment_window_state(local, current) == "closed":
            raise InvalidAssessmentPeriodError(
                "Assessment period is outside the supported scheduler window."
            )

    async def _build(self, request: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
        fetched_at = datetime.now(UTC)
        if request.kind in {QualityAssetKind.crypto, QualityAssetKind.fixed_income}:
            components = {
                "opportunity": _unsupported("This asset kind has no market opportunity resolver."),
                "fundamentals": _unsupported(
                    "This asset kind has no issuer fundamentals resolver."
                ),
                "quality": _unsupported(
                    "This asset kind requires a network or issuer-specific quality provider."
                ),
            }
            digest = _digest(components, None, None, None)
            return AssessmentSnapshotResponse(
                mode=request.mode,
                bootstrap_key=request.bootstrap_key,
                ticker=request.ticker,
                kind=request.kind,
                profile=None,
                venue=request.venue,
                period_at=request.period_at,
                fetched_at=fetched_at,
                status=_overall_status(value.state for value in components.values()),
                evidence_digest=digest,
                components={key: value.state for key, value in components.items()},
            )

        # ETFs are valued from their fund profile.  Calling the equity
        # opportunity resolver here would issue an unrelated request and, in
        # turn, make a second attempt from the quality resolver.
        opportunity = (
            _Component(
                None,
                _unsupported("Opportunity metrics are not applicable to ETF assets.").state,
            )
            if request.kind is QualityAssetKind.etf
            else await self._opportunity(request.ticker, request.period_at)
        )
        fundamentals = await self._fundamentals(request, opportunity.value)
        quality = await self._quality(request, opportunity.value, fundamentals.value)
        digest = _digest(
            {
                "opportunity": opportunity.state,
                "fundamentals": fundamentals.state,
                "quality": quality.state,
            },
            opportunity.value,
            fundamentals.value,
            quality.value,
        )
        return AssessmentSnapshotResponse(
            mode=request.mode,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            profile=_quality_profile(quality.value),
            venue=request.venue,
            period_at=request.period_at,
            fetched_at=fetched_at,
            status=_overall_status((opportunity.state, fundamentals.state, quality.state)),
            evidence_digest=digest,
            opportunity=opportunity.value,
            fundamentals=fundamentals.value,
            quality=quality.value,
            components={
                "opportunity": opportunity.state,
                "fundamentals": fundamentals.state,
                "quality": quality.state,
            },
        )

    async def _opportunity(
        self, ticker: str, period_at: datetime
    ) -> _Component[OpportunityResponse]:
        try:
            result = await self.opportunity.opportunity(ticker, as_of=period_at)
            has_values = any(
                metric.value is not None
                for metric in result.metrics.__dict__.values()
                if hasattr(metric, "value")
            )
            if has_values:
                state = AssessmentComponentState(status=AssessmentComponentStatus.available)
            elif result.source_failures:
                state = AssessmentComponentState(
                    status=AssessmentComponentStatus.failed,
                    error_code="OPPORTUNITY_SOURCES_UNAVAILABLE",
                    error="Opportunity sources failed before comparable metrics were resolved.",
                    retryable=True,
                )
            else:
                state = AssessmentComponentState(
                    status=AssessmentComponentStatus.missing_data,
                    error="No comparable opportunity metrics were resolved.",
                )
            return _Component(result, state)
        except _COMPONENT_ERRORS as exc:
            return _Component(None, _error_state(exc))

    async def _fundamentals(
        self,
        request: AssessmentSnapshotRequest,
        opportunity: OpportunityResponse | None,
    ) -> _Component[FundamentalsSnapshot]:
        if request.kind is not QualityAssetKind.stock:
            return _Component(
                None,
                _unsupported("Fundamentals are currently defined for issuer stocks only.").state,
            )
        instrument = opportunity.instrument if opportunity is not None else None
        metrics = opportunity.metrics if opportunity is not None else None
        # ``corporate_name`` is deliberately excluded from the shared snapshot
        # key.  A caller supplied hint therefore cannot be allowed to choose
        # the issuer whose filings are persisted for every other caller.  B3
        # metadata is the provider-owned identity and is the only name that is
        # safe to use for a globally shared lookup.  When it is unavailable or
        # untrusted, pass ``None`` so FundamentalsService follows its typed
        # fallback instead of letting the first caller poison the snapshot.
        # Request-level names are hints retained by the bootstrap queue for
        # wire compatibility, but provider-owned B3 identity remains the only
        # safe source for a shared CVM lookup.  A caller label must never be
        # allowed to choose the issuer persisted for every other client.
        corporate_name = _trusted_b3_name(request.ticker, instrument)
        try:
            result = await self.fundamentals.snapshot(
                request.ticker,
                corporate_name,
                reference_shares=metrics.shares_outstanding.value if metrics else None,
                earnings_per_share=metrics.earnings_per_share.value if metrics else None,
                book_value_per_share=metrics.book_value_per_share.value if metrics else None,
                recurring_dividends_per_share=metrics.dividends_12m.value if metrics else None,
                supplemental_sources=(
                    {
                        "earnings_per_share": ",".join(metrics.earnings_per_share.sources),
                        "book_value_per_share": ",".join(metrics.book_value_per_share.sources),
                        "recurring_dividends_per_share": ",".join(metrics.dividends_12m.sources),
                    }
                    if metrics
                    else None
                ),
                instrument=instrument,
                underlying_ticker=instrument.underlying_ticker if instrument else None,
                underlying_name=instrument.underlying_name if instrument else None,
            )
            if result.unavailable_reason or not result.periods:
                return _Component(
                    result,
                    AssessmentComponentState(
                        status=AssessmentComponentStatus.missing_data,
                        error=result.unavailable_reason or "No financial periods were resolved.",
                    ),
                )
            return _Component(
                result,
                AssessmentComponentState(status=AssessmentComponentStatus.available),
            )
        except _COMPONENT_ERRORS as exc:
            return _Component(None, _error_state(exc))

    async def _quality(
        self,
        request: AssessmentSnapshotRequest,
        opportunity: OpportunityResponse | None,
        fundamentals: FundamentalsSnapshot | None,
    ) -> _Component[QualityAssetFacts]:
        try:
            result = await self.quality.resolve(
                QualityFactsRequest(
                    assets=[
                        QualityAssetRequest(
                            ticker=request.ticker,
                            kind=request.kind,
                            # The provider derives methodology from canonical
                            # issuer/fund evidence.  Caller profile hints are
                            # intentionally ignored for shared snapshots.
                            profile=None,
                        )
                    ]
                ),
                opportunity_by_ticker={request.ticker: opportunity},
                fundamentals_by_ticker={request.ticker: fundamentals},
            )
            facts = result.assets[0] if result.assets else None
            if facts is None:
                return _Component(
                    None,
                    AssessmentComponentState(
                        status=AssessmentComponentStatus.missing_data,
                        error="Quality resolver returned no asset.",
                    ),
                )
            if facts.error_code:
                return _Component(
                    facts,
                    AssessmentComponentState(
                        status=AssessmentComponentStatus.failed,
                        error_code=facts.error_code,
                        error=facts.unavailable_reason or "Quality evidence resolution failed.",
                        retryable=facts.retryable,
                    ),
                )
            if facts.unavailable_reason:
                return _Component(
                    facts,
                    AssessmentComponentState(
                        status=AssessmentComponentStatus.missing_data,
                        error=facts.unavailable_reason,
                    ),
                )
            return _Component(
                facts,
                AssessmentComponentState(status=AssessmentComponentStatus.available),
            )
        except _COMPONENT_ERRORS as exc:
            return _Component(None, _error_state(exc))

    def _claim_response(
        self,
        request: AssessmentSnapshotRequest,
        claim: ClaimResult,
    ) -> AssessmentSnapshotResponse:
        existing = claim.record.response()
        if existing is not None:
            if claim.state == "in_progress":
                existing = existing.model_copy(update={"status": AssessmentRunStatus.processing})
            if claim.state == "retry_wait":
                retry = _remaining_seconds(claim.record.retry_at)
                existing = existing.model_copy(update={"retry_after_seconds": retry})
            return existing
        status = (
            AssessmentRunStatus.processing
            if claim.state == "in_progress"
            else AssessmentRunStatus.failed
        )
        return AssessmentSnapshotResponse(
            mode=request.mode,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            profile=None,
            venue=request.venue,
            period_at=request.period_at,
            status=status,
            error=(
                "An assessment for this period is already in progress."
                if claim.state == "in_progress"
                else claim.record.error or "Assessment failed before a result was published."
            ),
            retry_after_seconds=_remaining_seconds(claim.record.lease_expires_at)
            if claim.state == "in_progress"
            else _remaining_seconds(claim.record.retry_at),
        )

    def _unexpected_failure(
        self,
        request: AssessmentSnapshotRequest,
        exc: Exception,
    ) -> AssessmentSnapshotResponse:
        state = _error_state(exc)
        digest = _digest({"assessment": state}, None, None, None)
        return AssessmentSnapshotResponse(
            mode=request.mode,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            profile=None,
            venue=request.venue,
            period_at=request.period_at,
            status=AssessmentRunStatus.failed,
            evidence_digest=digest,
            components={"assessment": state},
            error=state.error,
        )

    def _in_progress_response(
        self, request: AssessmentSnapshotRequest
    ) -> AssessmentSnapshotResponse:
        return AssessmentSnapshotResponse(
            mode=request.mode,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            profile=None,
            venue=request.venue,
            period_at=request.period_at,
            status=AssessmentRunStatus.processing,
            error="Assessment ownership changed before publication; retry to read the winner.",
        )

    def _retry_delay(self, attempts: int) -> int:
        return int(self.retry_backoff_seconds * (2 ** max(0, attempts - 1)))


def assessment_key(request: AssessmentSnapshotRequest) -> str:
    """Build a bounded key from the immutable assessment identity."""

    if request.mode is AssessmentMode.bootstrap:
        return bootstrap_assessment_key(request)
    venue = request.venue or "-"
    return f"assessment:{ASSESSMENT_EVIDENCE_VERSION}:" + ":".join(
        (request.ticker, request.kind.value, venue, request.period_at.isoformat())
    )


def bootstrap_assessment_key(request: AssessmentSnapshotRequest) -> str:
    """Build the timestamp-independent key for one bootstrap identity."""

    if request.bootstrap_key is None:
        raise ValueError("bootstrap_key is required for a bootstrap assessment")
    venue = request.venue or "-"
    return f"assessment:{ASSESSMENT_EVIDENCE_VERSION}:bootstrap:" + ":".join(
        (request.ticker, request.kind.value, venue, request.bootstrap_key)
    )


def _unsupported(message: str) -> _Component[Any]:
    return _Component(
        None,
        AssessmentComponentState(status=AssessmentComponentStatus.unsupported, error=message),
    )


def _trusted_b3_name(ticker: str, instrument: InstrumentMetadata | None) -> str | None:
    """Return a provider-owned issuer name suitable for a shared snapshot.

    Request-level names are hints and are intentionally not accepted here:
    they are outside ``assessment_key`` and could otherwise make identical
    snapshots depend on whichever account happened to claim the row first.
    Only a matching B3 instrument with an explicit high-confidence identity
    can select CVM filings.  Other directory sources remain useful for display
    but are not authoritative for this lookup.
    """

    if instrument is None:
        return None
    if str(getattr(instrument, "ticker", "")).strip().upper() != ticker:
        return None
    if str(getattr(instrument, "source", "")).strip().lower() != "b3":
        return None
    if str(getattr(instrument, "confidence", "")).strip().lower() not in {
        "high",
        "verified",
        "authoritative",
    }:
        return None
    name = getattr(instrument, "name", None)
    if not isinstance(name, str):
        return None
    normalized = " ".join(name.split())
    return normalized or None


def _quality_profile(quality: QualityAssetFacts | None) -> str | None:
    """Return only the profile classified from provider evidence."""

    return quality.profile if quality is not None else None


def _assessment_window_state(period_at: datetime, now: datetime) -> str:
    """Classify a scheduler slot relative to its São Paulo capture window."""

    local_period = period_at.astimezone(ASSESSMENT_TIMEZONE)
    local_now = now.astimezone(ASSESSMENT_TIMEZONE)
    next_hour = {12: 14, 14: 16, 16: 19, 19: 0}[local_period.hour]
    start = local_period.replace(minute=0, second=0, microsecond=0)
    end = (
        (start + timedelta(days=1)).replace(hour=0)
        if next_hour == 0
        else start.replace(hour=next_hour)
    )
    if local_now < start:
        return "future"
    if local_now >= end:
        return "closed"
    return "open"


def _overall_status(states: Any) -> AssessmentRunStatus:
    """Keep provider failures out of the completed-result path.

    A component marked ``missing_data`` completed its lookup and found no
    usable observation.  That is a valid, immutable result for the period;
    retrying it only spends provider capacity and can prevent downstream
    engines from using their independent evidence.  Transport, schema and
    other source failures are represented explicitly as ``failed`` and remain
    retryable.
    """

    statuses = tuple(state.status for state in states)
    if any(status is AssessmentComponentStatus.failed for status in statuses):
        return AssessmentRunStatus.failed
    if any(status is AssessmentComponentStatus.available for status in statuses):
        return AssessmentRunStatus.completed
    return AssessmentRunStatus.completed


def _error_state(exc: Exception) -> AssessmentComponentState:
    if isinstance(exc, APIError):
        return AssessmentComponentState(
            status=AssessmentComponentStatus.failed,
            error_code=exc.code,
            error=exc.message,
            retryable=exc.retryable,
        )
    return AssessmentComponentState(
        status=AssessmentComponentStatus.failed,
        error_code=type(exc).__name__.upper(),
        error="Component resolution failed.",
        retryable=True,
    )


def _digest(
    states: dict[str, Any],
    opportunity: OpportunityResponse | None,
    fundamentals: FundamentalsSnapshot | None,
    quality: QualityAssetFacts | None,
) -> str:
    payload = {
        "states": {key: _json_value(value) for key, value in states.items()},
        "opportunity": opportunity.model_dump(mode="json") if opportunity else None,
        "fundamentals": fundamentals.model_dump(mode="json") if fundamentals else None,
        "quality": quality.model_dump(mode="json") if quality else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    """Convert pydantic state objects to deterministic JSON primitives."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _remaining_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((value - datetime.now(UTC)).total_seconds()))


__all__ = [
    "ASSESSMENT_EVIDENCE_VERSION",
    "AssessmentSnapshotService",
    "BootstrapIdentityConflictError",
    "InvalidAssessmentPeriodError",
    "assessment_key",
    "bootstrap_assessment_key",
]
