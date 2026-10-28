from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from threading import Lock

type LabelSet = tuple[tuple[str, str], ...]


_HISTOGRAM_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)


@dataclass
class _Histogram:
    counts: list[int]
    total: float = 0.0
    count: int = 0

    @classmethod
    def create(cls) -> _Histogram:
        return cls([0] * len(_HISTOGRAM_BUCKETS))

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        for index, boundary in enumerate(_HISTOGRAM_BUCKETS):
            if value <= boundary:
                self.counts[index] += 1


class MetricsRegistry:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._labeled_counters: Counter[tuple[str, LabelSet]] = Counter()
        self._gauges: dict[tuple[str, LabelSet], float] = {}
        self._histograms: dict[tuple[str, LabelSet], _Histogram] = {}

    def inc(
        self,
        name: str,
        amount: int = 1,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        label_set = _normalize_labels(labels)
        with self._lock:
            if label_set:
                self._labeled_counters[(name, label_set)] += amount
            else:
                self._counters[name] += amount

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if not isfinite(value) or value < 0:
            return
        label_set = _normalize_labels(labels)
        with self._lock:
            histogram = self._histograms.get((name, label_set))
            if histogram is None:
                histogram = _Histogram.create()
                self._histograms[(name, label_set)] = histogram
            histogram.observe(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if not isfinite(value):
            return
        label_set = _normalize_labels(labels)
        with self._lock:
            self._gauges[(name, label_set)] = value

    def reset(self) -> None:
        """Clear observations and restart process uptime for isolated tests."""
        with self._lock:
            self.started_at = time.time()
            self._counters.clear()
            self._labeled_counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def render_prometheus(self) -> str:
        with self._lock:
            started_at = self.started_at
            counters = dict(self._counters)
            labeled_counters = dict(self._labeled_counters)
            gauges = dict(self._gauges)
            histograms = {
                key: _Histogram(list(value.counts), value.total, value.count)
                for key, value in self._histograms.items()
            }
        lines = [
            "# HELP fundamentus_api_uptime_seconds Process uptime.",
            "# TYPE fundamentus_api_uptime_seconds gauge",
            f"fundamentus_api_uptime_seconds {time.time() - started_at:.3f}",
        ]
        counter_names = sorted(counters) + sorted({name for name, _labels in labeled_counters})
        for name in dict.fromkeys(counter_names):
            metric = f"fundamentus_api_{name}_total"
            lines.extend(
                [
                    f"# HELP {metric} Counter for {name}.",
                    f"# TYPE {metric} counter",
                ]
            )
            if name in counters:
                lines.append(f"{metric} {counters[name]}")
            for (labeled_name, labels), value in sorted(labeled_counters.items()):
                if labeled_name == name:
                    lines.append(f"{metric}{_render_labels(labels)} {value}")

        for name in sorted({metric_name for metric_name, _labels in gauges}):
            metric = f"fundamentus_api_{name}"
            lines.extend(
                [
                    f"# HELP {metric} Gauge for {name}.",
                    f"# TYPE {metric} gauge",
                ]
            )
            for (gauge_name, labels), gauge_value in sorted(gauges.items()):
                if gauge_name == name:
                    lines.append(f"{metric}{_render_labels(labels)} {gauge_value:g}")

        for name in sorted({metric_name for metric_name, _labels in histograms}):
            metric = f"fundamentus_api_{name}"
            lines.extend(
                [
                    f"# HELP {metric} Duration observations for {name}.",
                    f"# TYPE {metric} histogram",
                ]
            )
            for (histogram_name, labels), histogram in sorted(histograms.items()):
                if histogram_name != name:
                    continue
                for boundary, count in zip(_HISTOGRAM_BUCKETS, histogram.counts, strict=True):
                    bucket_labels = labels + (("le", _format_boundary(boundary)),)
                    lines.append(f"{metric}_bucket{_render_labels(bucket_labels)} {count}")
                inf_labels = labels + (("le", "+Inf"),)
                lines.append(f"{metric}_bucket{_render_labels(inf_labels)} {histogram.count}")
                lines.append(f"{metric}_sum{_render_labels(labels)} {histogram.total:.6f}")
                lines.append(f"{metric}_count{_render_labels(labels)} {histogram.count}")
        return "\n".join(lines) + "\n"


INCOME_SOURCE_LABELS = frozenset(
    {"fundamentus", "status_invest", "official_companies", "fundos_net", "other"}
)
INCOME_PHASE_LABELS = frozenset({"collect", "persist", "publish", "worker", "other"})
INCOME_OUTCOME_LABELS = frozenset(
    {"success", "failed", "timeout", "deferred", "lost_claim", "cancelled", "drained", "other"}
)


def normalize_income_source(value: object) -> str:
    source = str(value)
    return source if source in INCOME_SOURCE_LABELS else "other"


def record_income_phase(
    source: object,
    phase: object,
    outcome: object,
    duration_seconds: float | None = None,
) -> None:
    """Record bounded producer observations without affecting producer behavior."""
    source_label = normalize_income_source(source)
    phase_text = str(phase)
    phase_label = phase_text if phase_text in INCOME_PHASE_LABELS else "other"
    outcome_text = str(outcome)
    outcome_label = outcome_text if outcome_text in INCOME_OUTCOME_LABELS else "other"
    labels = {"source": source_label, "phase": phase_label, "outcome": outcome_label}
    try:
        metrics.inc("income_producer_operations", labels=labels)
        if duration_seconds is not None:
            metrics.observe("income_producer_duration_seconds", duration_seconds, labels=labels)
    except Exception:  # noqa: BLE001 - telemetry cannot mask producer behavior
        return


def initialize_income_source_busy() -> None:
    for source in sorted(INCOME_SOURCE_LABELS):
        set_income_source_busy(source, False)


def set_income_source_busy(source: object, busy: bool) -> None:
    """Set the bounded current-drain gauge for one source label."""
    try:
        metrics.set_gauge(
            "income_source_busy",
            1.0 if busy else 0.0,
            labels={"source": normalize_income_source(source)},
        )
    except Exception:  # noqa: BLE001 - telemetry cannot mask producer behavior
        return


def _normalize_labels(labels: Mapping[str, str] | None) -> LabelSet:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _render_labels(labels: LabelSet) -> str:
    if not labels:
        return ""
    values = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
    return "{" + values + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_boundary(value: float) -> str:
    return f"{value:g}"


metrics = MetricsRegistry()
