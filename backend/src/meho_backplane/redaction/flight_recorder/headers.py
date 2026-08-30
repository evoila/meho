# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail-closed header allowlist for the flight-recorder redaction engine.

Task #3213 (F2.1). Only enumerated known-safe headers survive into a
trace; **everything else is stripped unread**. This is an allowlist, not
a blocklist, and that choice is load-bearing:

    A blocklist ("record everything, drop the known-bad names") fails
    *open* the instant a vendor invents a new auth header -- the unknown
    ``X-Acme-Session`` sails through because it is on nobody's block
    list. An allowlist fails *closed*: an unknown header is dropped
    because it is not on the (small, curated) safe list, and the engine
    never even reads its value.

The allowlist is restricted to headers whose values are *structurally*
non-secret: protocol negotiation, content metadata, caching, and
correlation/rate-limit signals. Every header that carries -- or could
carry -- a credential, a cookie, a token, a signed URL, or a client
identity is absent by construction:

* ``authorization`` / ``proxy-authorization`` / ``www-authenticate`` /
  ``proxy-authenticate`` -- RFC 7235 credentials and challenges.
* ``cookie`` / ``set-cookie`` -- session material.
* every ``x-*-token`` / ``x-api-key`` / ``x-csrf-token`` / ``x-vault-token``
  / ``x-amz-security-token`` / CSRF header the connector auth layer sets.
* ``location`` / ``content-location`` / ``referer`` -- can carry tokens
  in redirect targets and signed URLs.
* ``forwarded`` / ``x-forwarded-*`` / ``x-real-ip`` -- client identity /
  network topology (PII), not useful debugging exhaust here.

As defense-in-depth, the value of every *surviving* header is still run
through the credential-shape scrub (:mod:`._content`): if a caller ever
pastes ``Bearer …`` into an otherwise-safe ``user-agent``, the shape net
catches it. That firing does not make the outcome uncertain -- the
engine *did* prove it redacted the shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.flight_recorder.verdict import RedactionOutcome

__all__ = ["HEADER_ALLOWLIST", "redact_headers"]


#: The curated set of known-safe header names, lowercased for
#: case-insensitive matching (HTTP header names are case-insensitive per
#: RFC 7230 §3.2). Kept deliberately small: anything not here is dropped
#: unread. Additions are a security decision, not a convenience one.
HEADER_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        # --- content metadata -------------------------------------------
        "content-type",
        "content-length",
        "content-encoding",
        "content-language",
        "content-range",
        # --- protocol / negotiation -------------------------------------
        "accept",
        "accept-encoding",
        "accept-language",
        "accept-charset",
        "accept-ranges",
        "allow",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "upgrade",
        "expect",
        "max-forwards",
        "host",
        "user-agent",
        "server",
        "via",
        # --- caching / conditional --------------------------------------
        "cache-control",
        "pragma",
        "age",
        "date",
        "expires",
        "last-modified",
        "etag",
        "vary",
        "if-match",
        "if-none-match",
        "if-modified-since",
        "if-unmodified-since",
        "range",
        "retry-after",
        # --- correlation / rate-limit (non-secret) ----------------------
        "x-request-id",
        "x-correlation-id",
        "x-trace-id",
        "traceparent",
        "tracestate",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        # --- response security policy (public directives) ---------------
        "strict-transport-security",
        "x-content-type-options",
        "x-frame-options",
        "referrer-policy",
        "content-security-policy",
    }
)


def redact_headers(headers: Any) -> RedactionOutcome:
    """Return only the allowlisted headers; strip everything else unread.

    *headers* is the raw header mapping (``httpx.Headers``, a plain
    ``dict``, or any ``Mapping``). The returned :class:`RedactionOutcome`
    carries a plain ``dict`` of the survivors under ``value`` (names
    normalised to lowercase), with each survivor's value run through the
    credential-shape scrub.

    Fail-closed conditions:

    * *headers* is not a mapping the engine can iterate -- it cannot
      apply the allowlist, so it records **no** headers and flags the
      outcome ``uncertain`` (the F5 operator-only degrade).
    * a surviving header's value is not a string (bytes / list / other)
      -- the engine cannot scrub it to prove it secret-free, so that one
      header is dropped. Dropping a datum is safe, so this does **not**
      make the whole outcome uncertain; it only adds a reason.

    A non-allowlisted header's value is never read: the loop ``continue``s
    on the name check before touching the value.
    """
    if headers is None:
        return RedactionOutcome(value={}, reasons=("no headers",))
    if not isinstance(headers, Mapping):
        return RedactionOutcome(
            value={},
            uncertain=True,
            reasons=("headers are not a mapping: cannot apply allowlist (fail-closed)",),
        )

    survivors: dict[str, str] = {}
    dropped_non_string = 0
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name not in HEADER_ALLOWLIST:
            # Stripped UNREAD: we never inspect ``raw_value`` for a
            # non-allowlisted header. This is the fail-closed guarantee.
            continue
        if not isinstance(raw_value, str):
            dropped_non_string += 1
            continue
        scrubbed, _ = scrub_content(raw_value)
        survivors[name] = scrubbed

    reasons: tuple[str, ...] = ()
    if dropped_non_string:
        reasons = (
            f"dropped {dropped_non_string} allowlisted header(s) with a "
            "non-string value (cannot prove secret-free)",
        )
    return RedactionOutcome(value=survivors, reasons=reasons)
