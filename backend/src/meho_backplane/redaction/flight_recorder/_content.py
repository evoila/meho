# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Defense-in-depth credential-shape scrub for flight-recorder traces.

Task #3213 (F2). The header allowlist (``headers``) and the per-connector
body-path config (``bodies``) are the *structural* controls; this module
is the shared **shape** net that runs underneath both, reusing the
Tier-1 redaction engine already shipped by Initiative #805
(:func:`meho_backplane.redaction.engine.redact`).

Why a second net beneath the structural controls? The F5 agent-access
override rests on "no secrets in there". The allowlist keeps unknown
headers out and the body-path config scrubs declared paths, but a
credential can still be *shaped* like one the moment a value survives:
a ``Bearer …`` string pasted into a benign ``user-agent`` header, or a
JWT nested at a path the connector author forgot to declare. Running the
credential-shape patterns over every surviving string leaf closes that
gap for every *known* secret shape without widening the structural
surface.

This is exactly the redaction precedent the preview path already relies
on (``operations/_request_preview.py`` runs the connector-boundary
engine over the would-be body); the flight recorder pins a tight,
credential-only policy rather than resolving a tenant policy, because
its job is "no secret shapes", not the broader infra-leak masking the
connector-boundary policy also does.

The policy is a module constant built at import (no I/O -- the engine
and patterns are pure), so the scrub costs one frozen policy for the
process lifetime.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.policy import RedactionPolicy

__all__ = ["CONTENT_POLICY_ID", "scrub_content"]

#: The flight-recorder credential-shape policy id -- surfaced on the
#: manifest and stable so a reader can attribute a firing to this net.
CONTENT_POLICY_ID: Final[str] = "flight-recorder-credential-shapes"

#: Tight, credential-only Tier-1 policy. Only the *secret*-shaped named
#: patterns are pinned; the infra-leak shapes (``uuid`` / ``ipv4`` /
#: ``ipv6`` / ``fqdn``) are deliberately excluded -- a hostname or UUID
#: in a trace is not a secret and over-redacting them would gut the
#: trace's debugging value. ``action='redact'`` replaces the whole match
#: with a fixed marker: partial reveal of a credential is itself a leak.
_CONTENT_POLICY: Final[RedactionPolicy] = RedactionPolicy.model_validate(
    {
        "id": CONTENT_POLICY_ID,
        "version": 1,
        "description": (
            "Flight-recorder defense-in-depth: redact credential-shaped "
            "strings in any surviving header value or body leaf (#3213)."
        ),
        "rules": [
            {
                "name": "fr-authorization-header",
                "pattern": "authorization_header",
                "action": "redact",
                "reason": "authorization header value (RFC 7235 secret)",
            },
            {
                "name": "fr-bearer-token",
                "pattern": "bearer_token",
                "action": "redact",
                "reason": "bearer token",
            },
            {
                "name": "fr-jwt",
                "pattern": "jwt",
                "action": "redact",
                "reason": "JSON Web Token",
            },
            {
                "name": "fr-api-key",
                "pattern": "api_key",
                "action": "redact",
                "reason": "API key / access / refresh / session token / secret id",
            },
            {
                "name": "fr-kubeconfig",
                "pattern": "kubeconfig",
                "action": "redact",
                "reason": "embedded kubeconfig credential",
            },
        ],
    },
)


def scrub_content(payload: Any) -> tuple[Any, bool]:
    """Redact credential-shaped strings anywhere in *payload*.

    Returns ``(redacted, fired)`` where *fired* is ``True`` when at
    least one credential shape matched. The engine walks nested
    dict / list / str structures and rewrites only string leaves, so a
    caller can hand it a bare string (a header value), a parsed JSON
    object, or a list and get the same-shaped result back.

    Never raises for input shape: non-str / non-container leaves pass
    through untouched (the engine's documented contract).
    """
    result = redact(payload, _CONTENT_POLICY)
    return result.redacted, bool(result.manifest)
