# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

import random
from typing import Any


class ReservoirSampler:
    __slots__ = ("k", "n_seen", "rng", "samples")

    def __init__(self, k: int, rng: random.Random):
        self.k = k
        self.rng = rng
        self.n_seen = 0
        self.samples: list[Any] = []

    def add(self, v: Any) -> None:
        n = self.n_seen = self.n_seen + 1
        samples = self.samples
        k = self.k
        if len(samples) < k:
            samples.append(v)
            return
        j = self.rng.randint(1, n)
        if j <= k:
            samples[j - 1] = v

    def get_exact_k(self) -> list[Any]:
        k = self.k
        if k <= 0:
            return []
        samples = self.samples
        n_seen = self.n_seen
        if n_seen <= 0:
            return [None] * k
        n_samples = len(samples)
        if n_samples >= k:
            return list(samples)
        out = list(samples)
        rng_choice = self.rng.choice
        needed = k - n_samples
        out.extend(rng_choice(samples) for _ in range(needed))
        return out
