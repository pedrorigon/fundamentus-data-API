import time
from collections import Counter
from threading import Lock


class MetricsRegistry:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = Lock()
        self._counters: Counter[str] = Counter()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def render_prometheus(self) -> str:
        with self._lock:
            counters = dict(self._counters)
        lines = [
            "# HELP fundamentus_api_uptime_seconds Process uptime.",
            "# TYPE fundamentus_api_uptime_seconds gauge",
            f"fundamentus_api_uptime_seconds {time.time() - self.started_at:.3f}",
        ]
        for name, value in sorted(counters.items()):
            metric = f"fundamentus_api_{name}_total"
            lines.extend(
                [
                    f"# HELP {metric} Counter for {name}.",
                    f"# TYPE {metric} counter",
                    f"{metric} {value}",
                ]
            )
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
