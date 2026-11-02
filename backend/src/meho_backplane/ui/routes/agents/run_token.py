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

Design: encrypted payload with a keyed session digest
=====================================================

The chassis policy keeps the FE stack to zero-new-dep crypto (no
``itsdangerous`` / ``starlette-csrf``). The token is a
:class:`cryptography.fernet.Fernet` token -- the same primitive the
session store uses:

    token = fernet.encrypt_at_time(payload_json, now)

where ``payload_json`` is ``{"sd", "name", "input", "work_ref"}``.
Fernet encrypts (AES-128-CBC) and authenticates (HMAC-SHA256) the
payload and stamps the issue time, so:

* the token is opaque: the run prompt and the session binding cannot be
  read from the stream URL;
* ``sd`` is a keyed HMAC-SHA256 digest of the console session id, never
  the session id itself, so even the decrypted payload holds no value
  that works as a session cookie;
* verification decrypts with ``ttl=RUN_TOKEN_TTL_SECONDS`` (Fernet's own
  timestamp check), recomputes the session digest from the cookie
  session, and compares it with :func:`hmac.compare_digest`.

The Fernet key is :attr:`Settings.ui_session_encryption_key` -- the same
keying material the CSRF tokens and the session-store Fernet use,
rotation-managed by the same Vault plumbing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Final

import structlog
from cryptography.fernet import Fernet, InvalidToken

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

#: Domain label for the keyed session digest, so the digest cannot be
#: confused with any other HMAC the console derives from the same key.
_SESSION_DIGEST_LABEL: Final[bytes] = b"meho-agent-run-token-session:"


@dataclass(frozen=True)
class DecodedRunToken:
    """The validated payload a run token carries.

    ``input`` and ``work_ref`` are the values the originating ``POST``
    authorised; the bridge runs the agent with exactly these (it does
    not read them from the GET query string, so a tampered query cannot
    redirect the run).
    """

    name: str
    input: str
    work_ref: str | None


def _run_token_key() -> str:
    """Return the configured session-encryption key.

    Empty key is a deployment-time misconfiguration; raise the same
    :class:`EncryptionKeyMissingError` the CSRF helper and session store
    raise so the failure surfaces with one remediation pointer.
    """
    key = get_settings().ui_session_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "UI_SESSION_ENCRYPTION_KEY is not set; the agent run console "
            "cannot derive the keying material for its run-handoff "
            "token. Generate a key with `python -c 'from cryptography."
            "fernet import Fernet; print(Fernet.generate_key().decode())'` "
            "and surface it as the UI_SESSION_ENCRYPTION_KEY env var."
        )
    return key


def _session_digest(key: str, session_id: str) -> str:
    """Keyed HMAC-SHA256 digest of *session_id* -- the token's session binding."""
    return hmac.new(
        key.encode("utf-8"),
        _SESSION_DIGEST_LABEL + session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def mint_run_token(
    *,
    session_id: str,
    name: str,
    input_: str,
    work_ref: str | None,
    now: float | None = None,
) -> str:
    """Mint an encrypted run token bound to *session_id*.

    The payload carries a keyed digest of the session id (not the id),
    the agent name, the prompt and the work reference. It is
    JSON-encoded and Fernet-encrypted with the issue time *now*, so the
    bridge can reject a stale token without server-side state.
    """
    key = _run_token_key()
    payload = {
        "sd": _session_digest(key, session_id),
        "name": name,
        "input": input_,
        "work_ref": work_ref,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    fernet = Fernet(key.encode("ascii"))
    if now is None:
        return fernet.encrypt(raw).decode("ascii")
    return fernet.encrypt_at_time(raw, int(now)).decode("ascii")


def verify_run_token(
    *,
    session_id: str,
    token: str,
    now: float | None = None,
) -> DecodedRunToken | None:
    """Return the decoded token iff it is valid for *session_id*, else ``None``.

    Stateless verification, in order:

    1. Decrypt + authenticate + freshness: Fernet checks the MAC and
       that the token is no older than ``RUN_TOKEN_TTL_SECONDS``.
    2. Decode: the plaintext is JSON with the expected keys.
    3. Binding: the embedded ``sd`` equals the digest of the cookie
       *session_id* (so a token minted for one session cannot be used
       under another).

    Any failure returns ``None`` -- the bridge maps that to a 403 / closed
    stream. The failure class is logged for forensics but not telegraphed
    to the client (remediation is identical: re-submit the run form).
    """
    # Fernet's base64 decoder raises ValueError (not InvalidToken) on
    # non-ASCII input, so screen it out first.
    if not token or not token.isascii():
        return None
    key = _run_token_key()
    fernet = Fernet(key.encode("ascii"))
    try:
        if now is None:
            raw = fernet.decrypt(token, ttl=RUN_TOKEN_TTL_SECONDS)
        else:
            raw = fernet.decrypt_at_time(token, ttl=RUN_TOKEN_TTL_SECONDS, current_time=int(now))
    except InvalidToken:
        _log.info("ui_agent_run_token_rejected", reason="invalid_or_expired")
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _log.info("ui_agent_run_token_rejected", reason="decode_failed")
        return None
    if not isinstance(payload, dict):
        return None

    token_digest = payload.get("sd")
    if not isinstance(token_digest, str) or not hmac.compare_digest(
        token_digest, _session_digest(key, session_id)
    ):
        _log.info("ui_agent_run_token_rejected", reason="session_mismatch")
        return None

    name = payload.get("name")
    input_ = payload.get("input")
    work_ref = payload.get("work_ref")
    if not isinstance(name, str) or not isinstance(input_, str):
        return None
    if work_ref is not None and not isinstance(work_ref, str):
        return None
    return DecodedRunToken(name=name, input=input_, work_ref=work_ref)
