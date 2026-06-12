# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Result shapes for the dual-run soak harness (G11.7-T2 #1402).

The enums and dataclasses the harness verifier (:mod:`scripts.soak_harness`)
produces and the shell driver (``scripts/soak/soak-harness.sh``) serialises
to JSON. Split out from the verifier so the comparison logic and the data
model evolve independently and neither file grows past the size gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ScorecardCell",
    "Severity",
    "SoakReport",
    "StageResult",
]


class Severity(StrEnum):
    """How a single soak finding bears on graduation.

    A ``BLOCKER`` is the Task's "any semantic divergence is a blocker"
    rule: it pins the scorecard cell at its current colour. An
    ``EXPLAINED`` finding is a divergence the operator has recorded a
    rationale for (the harness keeps it visible but does not let it
    block). ``INFO`` is a cosmetic-only note.
    """

    BLOCKER = "blocker"
    EXPLAINED = "explained"
    INFO = "info"


class ScorecardCell(StrEnum):
    """The retirement-scorecard write-column states an op moves through.

    ``BLOCKED`` (⛔) → ``SHADOW`` (🟡) → ``READY`` (✅). The harness never
    writes the scorecard directly (that is an ops-repo action); it emits
    the cell its evidence *supports*, and the documented procedure in the
    runbook maps that to the scorecard edit.
    """

    BLOCKED = "blocked"  # ⛔ — not yet dual-run-clean
    SHADOW = "shadow"  # 🟡 — stages 1-4 pass; live soak (stage 5) in flight
    READY = "ready"  # ✅ — clean soak, zero unexplained diffs, zero gaps


@dataclass(slots=True)
class StageResult:
    """The outcome of one soak stage for one op."""

    stage: int
    name: str
    passed: bool
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blockers(self) -> list[dict[str, Any]]:
        """Findings that pin the scorecard cell."""
        return [f for f in self.findings if f.get("severity") == Severity.BLOCKER.value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "passed": self.passed,
            "findings": self.findings,
        }


@dataclass(slots=True)
class SoakReport:
    """The full per-op soak verdict the driver writes out."""

    op_id: str
    connector_id: str
    stages: list[StageResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(s.passed for s in self.stages)

    @property
    def has_blocker(self) -> bool:
        return any(s.blockers for s in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "connector_id": self.connector_id,
            "all_passed": self.all_passed,
            "has_blocker": self.has_blocker,
            "stages": [s.to_dict() for s in self.stages],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)
