# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail-closed verdict primitives for the flight-recorder redaction engine.

Task #3213 (F2 of ``docs/decisions/dispatch-flight-recorder.md``). This
module is the leaf of the ``flight_recorder`` redaction subpackage: it
carries the small, dependency-light types every other module in the
package returns, so ``headers`` / ``bodies`` / ``families`` / ``span``
can import from here without an import cycle.

The load-bearing concept is the **redaction-uncertainty verdict**. The
whole flight recorder exists so an operator (and, per F5, an agent) can
read a dispatch trace *"as long as there are no secrets in there."* This
engine can only ever discharge that condition by *proving* it applied
every redaction rule end to end. Where proof is impossible -- an
unparseable body, a body-path config it could not resolve, an op family
it could not place, a body truncated mid-token -- the datum is dropped
fail-closed **and** the outcome is flagged :attr:`RedactionOutcome.uncertain`.

The consumer (capture wiring, #3214) maps ``uncertain`` to the F5
operator-only degrade: an uncertain trace is withheld from the agent
handle entirely and is readable on the operator plane alone. The default
on doubt is *less* exposure, never more.

Purity: no I/O, no clocks, no logging, no global mutable state. Every
function is deterministic for identical inputs, which is what lets the
adversarial test suite drive the engine directly.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BODY_OMITTED_MARKER",
    "BODY_PATH_MARKER",
    "SECRET_FAMILY_OMITTED_MARKER",
    "UNPLACEABLE_FAMILY_MARKER",
    "RedactionOutcome",
    "merge_uncertainty",
]


#: Placed where a declared body path was scrubbed. A non-secret,
#: fixed sentinel: it replaces the entire node (leaf or subtree) at a
#: matching path so a nested credential object never round-trips even
#: partially.
BODY_PATH_MARKER: Final[str] = "[MEHO-REDACTED:body-path]"

#: Placed where a body could not be proven fully redacted and was
#: therefore dropped fail-closed (unparseable / binary / malformed /
#: truncated-unparseable). Pairs with ``uncertain=True`` so the caller
#: degrades the trace to operator-only.
BODY_OMITTED_MARKER: Final[str] = "[MEHO-OMITTED:redaction-uncertain]"

#: Placed where an op belongs to a hard-excluded secret-bearing family
#: (credential / session-mint / token / destructive). The body is never
#: recorded regardless of the per-connector path config. This is a
#: *certain* omission -- the family was placed, so the trace stays
#: agent-readable with the body deliberately blank.
SECRET_FAMILY_OMITTED_MARKER: Final[str] = "[MEHO-OMITTED:secret-bearing-family]"

#: Placed where the op family could not be placed at all (missing /
#: blank op id). Fail-closed: the body is withheld *and* the outcome is
#: uncertain, because an unplaceable op might be a secret-bearing one.
UNPLACEABLE_FAMILY_MARKER: Final[str] = "[MEHO-OMITTED:family-unplaceable]"


class RedactionOutcome(BaseModel):
    """The result of redacting one captured artefact (headers or a body).

    * ``value`` -- the safe-to-persist result. Never contains a secret:
      it is either the redacted structure, ``None`` (nothing to record),
      or one of the fixed omission markers above. When ``uncertain`` is
      ``True`` this is a marker, never the raw datum.
    * ``uncertain`` -- ``True`` when the engine could not *prove* the
      redaction complete. The caller MUST map this to operator-only
      visibility (the F5 degrade). ``False`` means the engine applied
      every applicable rule and the artefact is safe for the agent
      surface.
    * ``reasons`` -- human-readable rationale strings, one per condition
      that fired. Carried into the trace so an operator sees *why* a
      datum was dropped or flagged without reading the engine's source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any = None
    uncertain: bool = False
    reasons: tuple[str, ...] = ()


def merge_uncertainty(*outcomes: RedactionOutcome) -> tuple[bool, tuple[str, ...]]:
    """Fold several artefact outcomes into one span-level verdict.

    A span is uncertain if *any* of its artefacts is uncertain -- doubt
    anywhere degrades the whole trace to operator-only (fail-closed
    composition). Reasons are concatenated in argument order so the
    operator sees every contributing condition.
    """
    uncertain = any(outcome.uncertain for outcome in outcomes)
    reasons = tuple(reason for outcome in outcomes for reason in outcome.reasons)
    return uncertain, reasons
