# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Span-level combiner for the flight-recorder redaction engine.

Task #3213 (F2). This is the one call the capture wiring (#3214) makes
per vendor-call span: hand it the raw captured artefacts plus the op
context, get back the redacted artefacts and **one** fail-closed verdict.
It composes the three structural controls -- the header allowlist
(:mod:`.headers`), the per-connector body-path config (:mod:`.bodies`),
and the hard-excluded op families (:mod:`.families`) -- and folds their
outcomes into a single ``uncertain`` flag the caller maps to the F5
operator-only degrade.

The combiner is shape-neutral: it takes primitive kwargs, never a span
model, so it never couples this library to the storage shape #3212 owns
or the capture shape #3214 owns. The caller reads the redacted artefacts
and the verdict off :class:`SpanRedaction` and lays them onto its own row.

Fail-closed composition: a span is uncertain if *any* artefact is
uncertain. Doubt anywhere withholds the whole trace from the agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from meho_backplane.redaction.flight_recorder.bodies import redact_body
from meho_backplane.redaction.flight_recorder.families import classify_body_exclusion
from meho_backplane.redaction.flight_recorder.headers import redact_headers
from meho_backplane.redaction.flight_recorder.verdict import (
    SECRET_FAMILY_OMITTED_MARKER,
    UNPLACEABLE_FAMILY_MARKER,
    RedactionOutcome,
    merge_uncertainty,
)

__all__ = ["SpanRedaction", "redact_span"]


class SpanRedaction(BaseModel):
    """The redacted, safe-to-persist artefacts for one span + its verdict.

    * ``request_headers`` / ``response_headers`` -- allowlisted survivors
      (plain dicts, names lowercased). Empty when none survived.
    * ``request_body`` / ``response_body`` -- the redacted body, ``None``
      (nothing to record), or an omission marker (family-excluded or
      dropped fail-closed).
    * ``body_recorded`` -- ``False`` when the op family is hard-excluded
      or unplaceable, so no body content was recorded at all.
    * ``uncertain`` -- ``True`` when the engine could not prove full
      redaction for some artefact. The caller MUST degrade this trace to
      operator-only (F5). ``False`` means every artefact is safe for the
      agent surface.
    * ``reasons`` -- the concatenated rationale strings from every
      artefact, for operator display.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_headers: dict[str, str] = {}
    response_headers: dict[str, str] = {}
    request_body: Any = None
    response_body: Any = None
    body_recorded: bool = True
    uncertain: bool = False
    reasons: tuple[str, ...] = ()


def redact_span(
    *,
    op_id: str | None,
    connector_id: str | None = None,
    method: str | None = None,
    tags: Iterable[str] = (),
    request_headers: Any = None,
    response_headers: Any = None,
    request_body: Any = None,
    response_body: Any = None,
    request_content_type: str | None = None,
    response_content_type: str | None = None,
    request_truncated: bool = False,
    response_truncated: bool = False,
    body_paths: tuple[str, ...] = (),
    delete_shaped_patterns: tuple[str, ...] | None = None,
) -> SpanRedaction:
    """Redact one span's captured artefacts and return a single verdict.

    Headers always pass through the allowlist. Bodies pass through the
    per-connector path config + shape scrub **unless** the op family is
    hard-excluded, in which case no body is recorded at all. The final
    ``uncertain`` flag is the OR of every artefact's uncertainty.
    """
    req_headers = redact_headers(request_headers)
    resp_headers = redact_headers(response_headers)

    exclusion = classify_body_exclusion(
        op_id,
        tags=tags,
        method=method,
        delete_shaped_patterns=delete_shaped_patterns,
    )

    if exclusion.excluded:
        marker = UNPLACEABLE_FAMILY_MARKER if exclusion.uncertain else SECRET_FAMILY_OMITTED_MARKER
        reason = (exclusion.reason,) if exclusion.reason else ()
        req_body = RedactionOutcome(value=marker, uncertain=exclusion.uncertain, reasons=reason)
        resp_body = RedactionOutcome(value=marker, uncertain=exclusion.uncertain, reasons=reason)
        body_recorded = False
    else:
        req_body = redact_body(
            request_body,
            paths=body_paths,
            content_type=request_content_type,
            truncated=request_truncated,
        )
        resp_body = redact_body(
            response_body,
            paths=body_paths,
            content_type=response_content_type,
            truncated=response_truncated,
        )
        body_recorded = True

    uncertain, reasons = merge_uncertainty(req_headers, resp_headers, req_body, resp_body)

    return SpanRedaction(
        request_headers=req_headers.value,
        response_headers=resp_headers.value,
        request_body=req_body.value,
        response_body=resp_body.value,
        body_recorded=body_recorded,
        uncertain=uncertain,
        reasons=reasons,
    )
