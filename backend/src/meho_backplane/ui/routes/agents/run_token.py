# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Signed, short-lived run-handoff token for the agent run console.

Initiative #1824 (G10.8 Agents console), Task #1829 (T2). The run
console drives a *live* agent run from the browser, and a browser can
only open a Server-Sent-Events stream with the WHATWG ``EventSource``
constructor -- which issues a **GET** with **no custom headers** (only
``withCredentials`` to attach cookies). That rules out the REST run path
(``POST /api/v1/agents/{name}/run/events`` -- a JWT-header POST) for the
console and forces a cookie-authed GET SSE bridge under ``/ui/agents``.

The wrinkle: starting an agent run executes real tool-calls against live
targets and incurs provider cost (the #1829 risk note). A bare
``GET /ui/agents/{name}/run/stream?input=...`` would let *any*
same-session GET kick off a run -- bypassing the CSRF double-submit the
chassis enforces on state-changing ``/ui/*`` requests (GET is a safe
method, so :class:`~meho_backplane.ui.csrf.CSRFMiddleware` waves it
through). That is exactly the residual same-site vector the chassis CSRF
design exists to close.

So the run console splits the flow:

* **``POST /ui/agents/{name}/run``** -- the CSRF-gated, operator-role
  state-changing action. It validates the prompt, confirms the agent is
  runnable (404 / 409 / 429 surface here, before any stream opens), then
  **mints a run token** binding ``(session_id, agent_name, input,
  work_ref)`` and hands back the transcript fragment whose
  ``sse-connect`` carries the token.
* **``GET /ui/agents/{name}/run/stream?token=...``** -- the cookie-authed
  SSE bridge. It verifies the token against the session that minted it
  (so a token cannot be replayed under a different session, and a GET
  with a forged / absent token streams nothing), lifts the operator, and
  proxies :meth:`~meho_backplane.agent.invocation.AgentInvoker.stream_events`.

The token is the load-bearing CSRF-equivalence link between the two
halves: the stream can only run a prompt that a CSRF-validated POST
authorised, for the same session, within the TTL window. It is **not** a
capability that widens access -- the bridge still re-lifts the operator
from the cookie session and re-runs the same tenant-scoped invoker call,
so tenant isolation comes from the lifted operator (identical to the
REST surface), not from the token.

Design: signed payload, mirroring the chassis CSRF HMAC
=======================================================

The chassis policy keeps the FE stack to hand-rolled, zero-new-dep
crypto (no ``itsdangerous`` / ``starlette-csrf``); this module follows
:mod:`meho_backplane.ui.csrf` exactly:

    token = b64url(payload_json) + "." + hex(hmac_sha256(secret, b64url(payload_json)))

where ``payload_json`` is ``{"sid", "name", "input", "work_ref",
"exp"}``. Verification recomputes the MAC with
:func:`hmac.compare_digest` (constant-time), checks the embedded
``session_id`` matches the cookie session, and rejects an expired
``exp``. The HMAC secret reuses
:attr:`Settings.ui_session_encryption_key` -- the same keying material
the CSRF tokens and the session-store Fernet use, rotation-managed by
the same Vault plumbing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Final

import structlog

from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.session_store import EncryptionKeyMissingError

__all__ = [
    "RUN_TOKEN_TTL_SECONDS",
    "DecodedRunToken",
    "mint_run_token",
    "verify_run_token",
]

_log = structlog.get_logger(__name__)

#: Seconds a freshly-minted run token stays valid. The token only has to
#: survive the round-trip from the ``POST`` response landing in the
#: browser to the ``EventSource`` opening the GET stream -- a handful of
#: seconds in practice. A tight TTL keeps the replay window small without
#: risking a slow connection failing to start its own run.
RUN_TOKEN_TTL_SECONDS: Final[int] = 120

#: HMAC algorithm -- SHA-256, matching the CSRF token's floor.
_HMAC_ALG = hashlib.sha256


@dataclass(frozen=True)
class DecodedRunToken:
    """The validated payload a run token carries.

    ``input`` and ``work_ref`` are the values the originating ``POST``
    authorised; the bridge runs the agent with exactly these (it does
    not read them from the GET query string, so a tampered query cannot
    redirect the run).
    """

    session_id: str
    name: str
    input: str
    work_ref: str | None


def _run_token_secret() -> bytes:
    """Return the HMAC secret bytes, reusing the session-encryption key.

    Empty key is a deployment-time misconfiguration; raise the same
    :class:`EncryptionKeyMissingError` the CSRF helper and session store
    raise so the failure surfaces with one remediation pointer rather
    than silently minting forgeable tokens off a zero-byte key.
    """
    key = get_settings().ui_session_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "UI_SESSION_ENCRYPTION_KEY is not set; the agent run console "
            "cannot derive the HMAC keying material for its run-handoff "
            "token. Generate a key with `python -c 'from cryptography."
            "fernet import Fernet; print(Fernet.generate_key().decode())'` "
            "and surface it as the UI_SESSION_ENCRYPTION_KEY env var."
        )
    return key.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    """URL-safe, unpadded base64 -- keeps the token query-string-clean."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    """Inverse of :func:`_b64url_encode` -- re-pad before decoding."""
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def mint_run_token(
    *,
    session_id: str,
    name: str,
    input_: str,
    work_ref: str | None,
    now: float | None = None,
) -> str:
    """Mint a signed run token binding the run to *session_id*.

    The payload is JSON-encoded, base64url-encoded, then HMAC-signed; the
    token is ``"<payload_b64>.<mac_hex>"``. The ``exp`` is ``now +
    RUN_TOKEN_TTL_SECONDS`` so the bridge can reject a stale token without
    server-side state. Stateless minting -- nothing persists; the token's
    only valid recipient is the bridge on the same backplane.
    """
    issued = time.time() if now is None else now
    payload = {
        "sid": session_id,
        "name": name,
        "input": input_,
        "work_ref": work_ref,
        "exp": int(issued + RUN_TOKEN_TTL_SECONDS),
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    mac = hmac.new(_run_token_secret(), payload_b64.encode("ascii"), _HMAC_ALG).hexdigest()
    return f"{payload_b64}.{mac}"


def verify_run_token(
    *,
    session_id: str,
    token: str,
    now: float | None = None,
) -> DecodedRunToken | None:
    """Return the decoded token iff it is valid for *session_id*, else ``None``.

    Stateless verification, in order:

    1. Structural: exactly one ``.`` separating two non-empty halves.
    2. Signature: constant-time HMAC compare over the payload half.
    3. Decode: the payload is valid base64url + JSON with the expected
       keys.
    4. Binding: the embedded ``sid`` equals the cookie *session_id* (so a
       token minted for one session cannot be replayed under another).
    5. Freshness: ``exp`` is in the future.

    Any failure returns ``None`` -- the bridge maps that to a 403 / closed
    stream. The failure class is logged for forensics but not telegraphed
    to the client (remediation is identical: re-submit the run form).
    """
    if not token or token.count(".") != 1:
        return None
    payload_b64, mac_hex = token.split(".")
    if not payload_b64 or not mac_hex:
        return None

    expected_mac = hmac.new(_run_token_secret(), payload_b64.encode("ascii"), _HMAC_ALG).hexdigest()
    if not hmac.compare_digest(expected_mac, mac_hex):
        _log.info("ui_agent_run_token_rejected", reason="signature_invalid")
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        _log.info("ui_agent_run_token_rejected", reason="decode_failed")
        return None
    if not isinstance(payload, dict):
        return None

    token_sid = payload.get("sid")
    if not isinstance(token_sid, str) or not hmac.compare_digest(token_sid, session_id):
        _log.info("ui_agent_run_token_rejected", reason="session_mismatch")
        return None

    exp = payload.get("exp")
    current = time.time() if now is None else now
    if not isinstance(exp, int) or exp < current:
        _log.info("ui_agent_run_token_rejected", reason="expired")
        return None

    name = payload.get("name")
    input_ = payload.get("input")
    work_ref = payload.get("work_ref")
    if not isinstance(name, str) or not isinstance(input_, str):
        return None
    if work_ref is not None and not isinstance(work_ref, str):
        return None
    return DecodedRunToken(
        session_id=token_sid,
        name=name,
        input=input_,
        work_ref=work_ref,
    )
