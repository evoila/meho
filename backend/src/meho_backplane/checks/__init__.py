# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded, typed check primitives for Initiative #2416.

This package's first two modules are dependency-pure (stdlib + pydantic +
this package): :mod:`meho_backplane.checks.assertions` holds the frozen spec
models plus the set-wide five-state vocabulary, and
:mod:`meho_backplane.checks.evaluate` holds the pure ``select -> compare``
evaluator. Downstream tasks add DB-facing siblings to this package (#2503 the
Sensor entity, #2505 the check runner, #2506 the Dashboard rollup); keep the
two modules re-exported here free of I/O.
"""

from __future__ import annotations

from meho_backplane.checks.assertions import (
    AssertionOutcome,
    AssertionSpec,
    CheckState,
    Compare,
    SelectSpec,
)
from meho_backplane.checks.evaluate import evaluate_assertion

__all__ = [
    "AssertionOutcome",
    "AssertionSpec",
    "CheckState",
    "Compare",
    "SelectSpec",
    "evaluate_assertion",
]
