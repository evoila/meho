# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.redaction.flight_recorder`` -- fail-closed trace redaction.

Task #3213, implementing **F2** of the accepted decision
``docs/decisions/dispatch-flight-recorder.md``. This is the load-bearing
security control of the dispatch flight recorder: it is what makes the F5
agent-access override admissible, discharging the operator's verbatim
condition *"as long as there are no secrets in there."*

This package is a **pure library**. It has no capture wiring (the
dispatcher seam is #3214's), no storage (the trace tables are #3212's),
no I/O, no clocks, and no global mutable state. It answers exactly one
question, four ways, and always fails closed:

1. **Header allowlist** (:func:`redact_headers`) -- only enumerated
   known-safe headers survive; everything else is stripped **unread**.
   A blocklist is rejected: it fails open on unknown vendor fields.
2. **Per-connector body-path redaction** (:class:`BodyPathRedactionConfig`,
   :func:`redact_body`) -- declared dotted-path globs scrubbed from
   request and response bodies, with a credential-shape net underneath.
3. **Hard-excluded op families** (:func:`classify_body_exclusion`) --
   credential / session-mint / token op families never record bodies,
   regardless of config; single-sourced with the destructive /
   delete-shaped classifier so the two lists cannot drift.
4. **Redaction-uncertainty signalling** (:attr:`RedactionOutcome.uncertain`,
   :attr:`SpanRedaction.uncertain`) -- any state the engine cannot
   *prove* fully redacted (unparseable/binary body, malformed JSON,
   truncated mid-token, path-config error, unplaceable op family) returns
   an explicit uncertain verdict the caller MUST map to operator-only
   visibility (the F5 degrade).

The one call the capture wiring makes per span is :func:`redact_span`; the
granular functions are exported for finer-grained use and testing.
"""

from meho_backplane.redaction.flight_recorder.bodies import (
    BodyPathRedactionConfig,
    redact_body,
)
from meho_backplane.redaction.flight_recorder.families import (
    SECRET_FAMILY_PATTERNS,
    SECRET_FAMILY_TAGS,
    BodyExclusion,
    classify_body_exclusion,
)
from meho_backplane.redaction.flight_recorder.headers import (
    HEADER_ALLOWLIST,
    redact_headers,
)
from meho_backplane.redaction.flight_recorder.span import SpanRedaction, redact_span
from meho_backplane.redaction.flight_recorder.verdict import (
    BODY_OMITTED_MARKER,
    BODY_PATH_MARKER,
    SECRET_FAMILY_OMITTED_MARKER,
    UNPLACEABLE_FAMILY_MARKER,
    RedactionOutcome,
    merge_uncertainty,
)

__all__ = [
    "BODY_OMITTED_MARKER",
    "BODY_PATH_MARKER",
    "HEADER_ALLOWLIST",
    "SECRET_FAMILY_OMITTED_MARKER",
    "SECRET_FAMILY_PATTERNS",
    "SECRET_FAMILY_TAGS",
    "UNPLACEABLE_FAMILY_MARKER",
    "BodyExclusion",
    "BodyPathRedactionConfig",
    "RedactionOutcome",
    "SpanRedaction",
    "classify_body_exclusion",
    "merge_uncertainty",
    "redact_body",
    "redact_headers",
    "redact_span",
]
