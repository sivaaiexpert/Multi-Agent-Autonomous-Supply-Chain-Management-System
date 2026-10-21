"""Deterministic synthetic data generators. EVERYTHING produced here is synthetic and says so in the reports: it validates the engines'
behaviour against a known data-generating process, it is not evidence about any real business."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DemandProcess:
    base: float
    weekly_amplitude: float = 0.25
    trend_per_day: float = 0.0
    noise: float = 0.6                    # std-dev as a multiple of sqrt(level): Poisson-like
    surge_start: int | None = None        # day index at which demand starts to grow
    surge_factor: float = 1.0

    def level(self, day: int, phase: float = 0.0) -> float:
        lvl = self.base * (1 + self.weekly_amplitude * math.sin(2 * math.pi * (day % 7) / 7 + phase)) * (1 + self.trend_per_day * day)
        if self.surge_start is not None and day >= self.surge_start:
            lvl *= self.surge_factor
        return max(lvl, 0.0)

    def sample(self, day: int, rng: random.Random, phase: float = 0.0) -> float:
        lvl = self.level(day, phase)
        return max(0.0, rng.gauss(lvl, math.sqrt(lvl) * self.noise))


def demand_series(process: DemandProcess, days: int, seed: int, start_day: int = 0) -> list[float]:
    rng = random.Random(seed)
    phase = random.Random(seed + 99).uniform(0, 2 * math.pi)
    return [round(process.sample(start_day + i, rng, phase), 3) for i in range(days)]


def random_process(rng: random.Random) -> DemandProcess:
    return DemandProcess(base=rng.uniform(3, 40), weekly_amplitude=rng.uniform(0.0, 0.4), trend_per_day=rng.uniform(-0.003, 0.006),
                         noise=rng.uniform(0.3, 0.9))


def random_supplier_options(rng: random.Random, n: int | None = None):
    from scm.optimizer import SupplierOption
    n = n or rng.randint(2, 5)
    return [SupplierOption(f"S{i}", f"Supplier {i}", round(rng.uniform(5, 60), 2), rng.randint(2, 21), rng.uniform(60, 99), rng.uniform(0.9, 1.0),
                           rng.choice([1, 10, 25, 50]), rng.choice([None, 120, 300, 800]), rng.choice(["US", "EU", "APAC"])) for i in range(n)]
