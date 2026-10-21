"""LLM resilience layer: model routing, bounded retries with exponential backoff, per-model circuit breakers, metrics.

    task -> ModelRouter.route() -> [primary, secondary, fallback]  (configurable, de-duplicated, never hard-coded in agents)
    for each model: breaker closed? -> up to `retries`+1 attempts (transient errors only, exponential backoff + deterministic jitter)
    all models exhausted / breaker open / deadline spent -> caller uses its DETERMINISTIC fallback (this layer returns None)

One shared deadline (the configured timeout) bounds the whole call across retries and models, so a hung model cannot multiply the wait.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable

from .settings import LLMCfg, get_settings

TRANSIENT = (ConnectionError, TimeoutError, OSError)


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open for `cooldown` -> half-open (one probe) -> closed on success / open on failure."""

    def __init__(self, threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures, self._probing = 0, False
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self.opened_at is None:
                return "closed"
            return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if self.clock() - self.opened_at >= self.cooldown and not self._probing:
                self._probing = True                    # exactly one probe request while half-open
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.failures, self.opened_at, self._probing = 0, None, False

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            self._probing = False
            if self.failures >= self.threshold:
                self.opened_at = self.clock()


@dataclass
class Metrics:
    request_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    fallback_count: int = 0          # calls answered by a secondary/fallback model OR by the deterministic path
    retry_count: int = 0
    breaker_rejections: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    by_model: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **inc) -> None:
        with self._lock:
            for k, v in inc.items():
                setattr(self, k, getattr(self, k) + v)

    def observe(self, model: str, latency_ms: float, ok: bool, tin: int = 0, tout: int = 0) -> None:
        with self._lock:
            self.latencies_ms.append(latency_ms)
            if len(self.latencies_ms) > 5000:
                del self.latencies_ms[:1000]
            m = self.by_model.setdefault(model, {"calls": 0, "failures": 0, "tokens_in": 0, "tokens_out": 0})
            m["calls"] += 1
            m["failures"] += 0 if ok else 1
            m["tokens_in"] += tin
            m["tokens_out"] += tout
            self.tokens_in += tin
            self.tokens_out += tout

    def snapshot(self) -> dict:
        from .obs import percentile
        with self._lock:
            lat = list(self.latencies_ms)
            base = {k: getattr(self, k) for k in ("request_count", "failure_count", "timeout_count", "fallback_count", "retry_count",
                                                  "breaker_rejections", "tokens_in", "tokens_out")}
            base["by_model"] = {k: dict(v) for k, v in self.by_model.items()}
        base.update(p50_ms=percentile(lat, 50), p95_ms=percentile(lat, 95), p99_ms=percentile(lat, 99))
        return base

    def reset(self) -> None:
        with self._lock:
            self.request_count = self.failure_count = self.timeout_count = self.fallback_count = self.retry_count = 0
            self.breaker_rejections = self.tokens_in = self.tokens_out = 0
            self.latencies_ms.clear()
            self.by_model.clear()


METRICS = Metrics()
_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKER_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="llm")


def breaker_for(model: str, cfg: LLMCfg | None = None) -> CircuitBreaker:
    cfg = cfg or get_settings().llm
    with _BREAKER_LOCK:
        if model not in _BREAKERS:
            _BREAKERS[model] = CircuitBreaker(cfg.breaker_threshold, cfg.breaker_cooldown_seconds)
        return _BREAKERS[model]


def reset_breakers() -> None:
    with _BREAKER_LOCK:
        _BREAKERS.clear()


class ModelRouter:
    """Maps a task to an ordered model list. `simple` tasks (summaries, email tone) use the cheap/primary model; `complex` tasks may use
    `complex_model_override`; the secondary and fallback models are tried when the earlier ones fail. The run-level model selected in the UI
    (cfg['llm_model']) always wins as the primary."""

    def __init__(self, cfg: LLMCfg | None = None):
        self.cfg = cfg or get_settings().llm

    def route(self, task: str = "simple", primary_override: str | None = None) -> list[str]:
        c = self.cfg
        first = primary_override or c.primary_model
        order = [c.complex_model_override if task == "complex" and c.complex_model_override else first, first, c.secondary_model, c.fallback_model]
        seen, out = set(), []
        for m in order:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out


@dataclass
class CallOutcome:
    reply: object | None
    model: str | None
    attempts: int = 0
    retries: int = 0
    error: str = ""
    timed_out: bool = False
    used_fallback: bool = False
    latency_ms: float = 0.0


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with deterministic jitter (0.75x..1.25x derived from the attempt number, so tests are reproducible)."""
    jitter = 0.75 + 0.5 * (((attempt * 2654435761) % 1000) / 1000.0)
    return min(cap, base * (2 ** attempt)) * jitter


def call_with_policy(invoke: Callable[[str, float], object], models: list[str], timeout: float, retries: int, backoff_base: float,
                     backoff_cap: float, cfg: LLMCfg | None = None, sleep: Callable[[float], None] = time.sleep) -> CallOutcome:
    """`invoke(model, remaining_seconds)` performs ONE attempt. Never raises; the outcome says what happened."""
    cfg = cfg or get_settings().llm
    METRICS.add(request_count=1)
    deadline = time.monotonic() + timeout
    out = CallOutcome(None, None)
    t0 = time.perf_counter()
    for idx, model in enumerate(models):
        br = breaker_for(model, cfg)
        if not br.allow():
            METRICS.add(breaker_rejections=1)
            out.error = out.error or f"circuit breaker open for {model}"
            continue
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                out.timed_out, out.error = True, f"deadline of {timeout:g}s exhausted"
                break
            out.attempts += 1
            fut: Future = _POOL.submit(invoke, model, remaining)
            try:
                out.reply = fut.result(timeout=remaining)
                out.model, out.used_fallback = model, idx > 0
                br.success()
                out.latency_ms = (time.perf_counter() - t0) * 1000
                if out.used_fallback:
                    METRICS.add(fallback_count=1)
                return out
            except FutureTimeout:
                fut.cancel()
                br.failure()
                out.timed_out, out.error = True, f"{model} timed out after {remaining:.0f}s"
                METRICS.add(timeout_count=1, failure_count=1)
                break                                           # a timeout already spent the budget: no retry on the same model
            except Exception as exc:
                br.failure()
                out.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                METRICS.add(failure_count=1)
                if isinstance(exc, TRANSIENT) and attempt < retries:
                    out.retries += 1
                    METRICS.add(retry_count=1)
                    delay = min(backoff_delay(attempt, backoff_base, backoff_cap), max(0.0, deadline - time.monotonic()))
                    if delay > 0:
                        sleep(delay)
                    continue
                break                                           # permanent error (e.g. model not found): try the next model
        if out.timed_out:
            break
    out.latency_ms = (time.perf_counter() - t0) * 1000
    METRICS.add(fallback_count=1)                               # nothing answered: the caller's deterministic fallback takes over
    return out
