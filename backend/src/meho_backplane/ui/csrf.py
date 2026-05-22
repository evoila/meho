# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Double-submit-cookie CSRF protection for ``/ui/*`` state-changing routes.

Initiative #337 (G10.0 Frontend chassis), Task #866 (T5). The BFF
session cookie already carries ``SameSite=Strict`` -- which by spec
omits the cookie from every cross-site navigation, blocking the entire
CSRF class on top-level requests. CSRF here is **belt-and-braces**
against the residual same-site vector: a malicious sub-domain on
``*.evba.lab`` (or a compromise of an unrelated path on the same host)
could still issue an XHR carrying the session cookie. The
double-submit token defeats that vector because a same-site attacker
JS cannot read the session cookie (``HttpOnly``) and therefore cannot
derive the matching CSRF token to echo back.

Design: OWASP's "Signed Double-Submit Cookie" pattern
=====================================================

The naive double-submit pattern (random value cookie + same value in a
custom header) is vulnerable to cookie injection from a sub-domain
attacker who can set a cookie on the parent domain. OWASP's CSRF
Prevention Cheat Sheet recommends binding the token to the user's
session via HMAC:

    token = hmac_sha256(secret, session_id || random) + "." + random

The server validates by recomputing the HMAC from the presented random
half + the session_id read from the validated ``meho_session`` cookie;
mismatch → reject. An attacker forging the cookie cannot also forge a
header echo matching the HMAC (the secret never leaves the server) and
cannot read the session_id (``HttpOnly`` cookie + same-origin
restriction on cross-origin JS).

Why in-house instead of ``starlette-csrf``
==========================================

* The chassis policy (decisions #9 + #10) keeps the FE stack to
  hand-rolled, zero-new-dep components. ``starlette-csrf`` adds a
  transitive dependency for ~30 lines of crypto + middleware logic
  that the chassis can carry directly.
* ``starlette-csrf`` ties to its own cookie name / header name and
  ignores the BFF's session_id; in-house lets us pin the HMAC to the
  ``meho_session`` cookie value end-to-end.
* The middleware shape mirrors :class:`UISessionMiddleware` (pure ASGI,
  one ``__call__``, narrow path-prefix scope) so a single mental model
  governs every ``/ui/*`` pre-routing hook.

Scope
=====

The middleware short-circuits on three conditions before any token
check happens:

1. Non-HTTP scope (lifespan, websocket) -- passes through.
2. Path outside ``/ui/*`` -- passes through (``/api/*`` and ``/mcp``
   handle their own auth + idempotency).
3. Method is read-only (``GET`` / ``HEAD`` / ``OPTIONS``) -- passes
   through (CSRF protects state-changing requests only).

The remaining ``POST`` / ``PATCH`` / ``PUT`` / ``DELETE`` requests
under ``/ui/`` must carry the CSRF token in **either** the
``X-CSRF-Token`` header (HTMX form-injected) **or** the ``csrf_token``
form field (server-rendered form fallback). Missing or invalid →
``403 Forbidden`` with a stable detail string.

Token issuance
==============

The server mints a fresh token on every authenticated dashboard render
(:func:`mint_csrf_token`). Surface templates embed the token in the
HTMX-friendly shape::

    <body hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'>

so every HTMX ``hx-post`` / ``hx-delete`` request inherits the header
automatically. Forms outside the HTMX surface include the token in a
hidden field; the middleware accepts either source.

References
----------

* OWASP CSRF Prevention Cheat Sheet (signed double-submit cookie):
  https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
* RFC 6265bis -- ``SameSite`` cookie attribute:
  https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
* MDN: ``Set-Cookie`` Same-Site:
  https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final
from urllib.parse import parse_qs

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.routes import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import EncryptionKeyMissingError

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER_NAME",
    "CSRFMiddleware",
    "mint_csrf_token",
    "verify_csrf_token",
]


#: Name of the cookie carrying the double-submit token. Separate from
#: ``meho_session`` so the JS-readable half (the random tail of the
#: token) can be set without ``HttpOnly`` -- HTMX needs to echo it
#: back in the ``X-CSRF-Token`` header, which requires JavaScript
#: access. The HMAC binding to the session_id is what defeats cookie
#: injection from a sub-domain attacker.
CSRF_COOKIE_NAME: Final[str] = "meho_csrf"

#: Custom header HTMX populates on every state-changing request via
#: ``hx-headers='{"X-CSRF-Token": "..."}'`` on the page ``<body>``.
#: The header MUST be a custom name (not ``Cookie`` /
#: ``Authorization``) so the same-origin restriction on cross-origin
#: JS prevents an attacker on a different origin from sending it.
CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"

#: Hidden form-field name for surface templates rendering a classic
#: ``<form method="post">`` that the HTMX header pattern can't reach
#: (e.g. an HTML upload form without ``hx-encoding``). The middleware
#: accepts either source.
CSRF_FORM_FIELD: Final[str] = "csrf_token"

#: HTTP methods that bypass CSRF. The remainder are state-changing and
#: must carry a valid token.
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

#: Prefix every UI route lives under. Outside this prefix the
#: middleware passes through; ``/api/*`` and ``/mcp/*`` enforce their
#: own request validation via Bearer-token signatures.
_UI_PREFIX: Final[str] = "/ui/"

#: BFF auth surfaces -- ``/ui/auth/login`` / ``/ui/auth/callback`` /
#: ``/ui/auth/logout`` are all ``GET`` per Task #865, so they fall
#: through ``_SAFE_METHODS`` already. The constant is documented for
#: greppability rather than as a runtime branch.
_AUTH_PREFIX: Final[str] = "/ui/auth/"

#: Random-half byte length. 32 bytes = 256 bits of entropy; comfortably
#: above the OWASP "cryptographically strong random" floor.
_RANDOM_BYTES: Final[int] = 32

#: HMAC algorithm. SHA-256 keeps the token compact while exceeding
#: the OWASP "strong cryptographic hash" floor.
_HMAC_ALG = hashlib.sha256


def _csrf_secret() -> bytes:
    """Return the HMAC secret bytes.

    Reuses :attr:`Settings.ui_session_encryption_key` as the keying
    material instead of introducing a second env var. The session-store
    Fernet key is a URL-safe base64 32-byte value -- already
    cryptographically strong and rotation-managed by the same Vault
    plumbing that lands the Fernet key itself. Mixing CSRF + session
    encryption under one key is acceptable because the two consumers
    apply different KDFs (Fernet's HMAC-SHA256 + AES; ours raw
    HMAC-SHA256) over distinct input domains (session token bytes vs
    ``session_id || random``).

    Empty key (``UI_SESSION_ENCRYPTION_KEY`` unset) is a deployment-
    time misconfiguration. Raising
    :class:`~meho_backplane.ui.auth.session_store.EncryptionKeyMissingError`
    surfaces the failure with the same exception type and remediation
    pointer the session store uses (``_get_fernet`` line 252), so the
    chassis fail-fast contract claimed in the docstring above is
    actually enforced rather than papered over with a zero-byte HMAC
    key (which would silently mint deterministic tokens an attacker
    could forge off-line).

    Returns the key bytes verbatim -- :func:`hmac.new` accepts any
    bytes-like key.
    """
    key = get_settings().ui_session_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "UI_SESSION_ENCRYPTION_KEY is not set; the operator-console "
            "CSRF middleware cannot derive its HMAC keying material. "
            "Generate a key with `python -c 'from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())'` and "
            "surface it as the UI_SESSION_ENCRYPTION_KEY env var."
        )
    return key.encode("utf-8")


def mint_csrf_token(session_id: str) -> str:
    """Mint a fresh signed double-submit CSRF token for *session_id*.

    Token shape: ``"<hmac_hex>.<random_hex>"`` where ``hmac_hex`` is
    HMAC-SHA256(secret, ``session_id || random``) hex-encoded. The two
    halves are joined with ``.`` so the random-half can be parsed out
    by :func:`verify_csrf_token` without an explicit length parameter.

    The token is the value the template embeds in
    ``hx-headers='{"X-CSRF-Token": "<token>"}'`` AND the value the
    middleware sets on the ``meho_csrf`` cookie. Both ride the same
    response; the browser sends the cookie back automatically and the
    HTMX wiring sends the header echo. Mismatch → 403.

    The minting is stateless -- nothing persists server-side. Any
    in-flight token presented with a matching ``session_id`` validates
    until the operator logs out (or the session row expires); replay
    is not a concern because the token's only valid recipient is the
    backplane itself, and the SameSite=Strict session cookie blocks
    cross-site replay outright.
    """
    random_bytes = secrets.token_bytes(_RANDOM_BYTES)
    random_hex = random_bytes.hex()
    payload = session_id.encode("ascii") + random_bytes
    mac = hmac.new(_csrf_secret(), payload, _HMAC_ALG).hexdigest()
    return f"{mac}.{random_hex}"


def verify_csrf_token(session_id: str, token: str) -> bool:
    """Return ``True`` iff *token* is a valid signed double-submit for *session_id*.

    Stateless verification: split the token on ``.``, decode the
    random half, recompute the HMAC, and compare via
    :func:`hmac.compare_digest` (constant-time -- prevents the timing
    side-channel a naive ``==`` would expose).

    Returns ``False`` on any structural failure (no separator, wrong
    field count, non-hex bytes, empty halves). The middleware logs the
    failure class for forensic visibility but does not telegraph the
    cause in the response body (the operator-facing remediation is
    identical -- refresh the page).
    """
    if not token or "." not in token:
        return False
    parts = token.split(".")
    if len(parts) != 2:
        return False
    mac_hex, random_hex = parts
    if not mac_hex or not random_hex:
        return False
    try:
        random_bytes = bytes.fromhex(random_hex)
    except ValueError:
        return False
    payload = session_id.encode("ascii") + random_bytes
    expected = hmac.new(_csrf_secret(), payload, _HMAC_ALG).hexdigest()
    return hmac.compare_digest(expected, mac_hex)


def _select_path(scope: Scope) -> str:
    """Return the ASGI scope's path, defaulting to ``/``."""
    raw = scope.get("path")
    return raw if isinstance(raw, str) and raw else "/"


def _select_method(scope: Scope) -> str:
    """Return the ASGI scope's HTTP method, upper-cased."""
    raw = scope.get("method")
    return raw.upper() if isinstance(raw, str) else "GET"


def _extract_cookie(scope: Scope, cookie_name: str) -> str | None:
    """Pull a single cookie value out of the raw ASGI headers.

    Mirrors :func:`~meho_backplane.ui.auth.middleware._extract_session_cookie`
    so the two pre-routing hooks share the parse pattern.
    """
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return None
    name_bytes = cookie_name.encode("ascii")
    for hdr_name, hdr_value in headers:
        if not isinstance(hdr_name, (bytes, bytearray)) or hdr_name != b"cookie":
            continue
        if not isinstance(hdr_value, (bytes, bytearray)):
            continue
        value_bytes = bytes(hdr_value)
        for chunk in value_bytes.split(b";"):
            chunk = chunk.strip()
            if not chunk or b"=" not in chunk:
                continue
            cookie_key, _, cookie_value = chunk.partition(b"=")
            if cookie_key.strip() == name_bytes:
                try:
                    return cookie_value.decode("ascii")
                except UnicodeDecodeError:
                    return None
    return None


def _extract_header(scope: Scope, header_name: str) -> str | None:
    """Pull a request-header value (latin-1 decoded) out of the ASGI scope."""
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return None
    needle = header_name.lower().encode("ascii")
    for hdr_name, hdr_value in headers:
        if not isinstance(hdr_name, (bytes, bytearray)):
            continue
        if hdr_name.lower() != needle:
            continue
        if isinstance(hdr_value, (bytes, bytearray)):
            return bytes(hdr_value).decode("latin-1")
    return None


def _forbidden_response(reason: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Build the ASGI three-tuple (status, headers, body) for a 403.

    Inlined rather than synthesised via :class:`fastapi.responses.JSONResponse`
    so the middleware stays single-allocation on the rejection path.
    """
    body = b'{"detail":"csrf_token_invalid"}'
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-csrf-rejection-reason", reason.encode("ascii")),
    ]
    return 403, headers, body


async def _send_forbidden(send: Send, reason: str) -> None:
    """Emit the canonical 403 response shape for a CSRF rejection."""
    status_code, headers, body = _forbidden_response(reason)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _extract_form_token(receive: Receive) -> tuple[str | None, list[Message]]:
    """Drain the request body, return ``(token, buffered_messages)``.

    ASGI body messages can only be received once; if the middleware
    consumes the body to read a form field, the downstream handler
    gets an empty body unless we replay the captured messages via a
    replacement ``receive`` closure. This function buffers up to a
    bounded number of ``http.request`` messages, parses the
    ``csrf_token`` form field, and returns both the parsed value and
    the buffer for replay.

    The bound is intentionally tight (256 KiB total / 32 messages) --
    state-changing form submissions in v0.2 are small (logout button,
    eventual scope-promotion form). A request exceeding the bound is
    treated as "no form token" and falls through to the header check;
    the original body still flows through to the handler verbatim
    because we capture every message even past the parse-bail point.
    """
    buffered: list[Message] = []
    body_parts: list[bytes] = []
    body_total = 0
    max_body = 256 * 1024
    max_msgs = 32
    while True:
        message = await receive()
        buffered.append(message)
        if message["type"] != "http.request":
            break
        body_chunk = message.get("body", b"")
        if isinstance(body_chunk, (bytes, bytearray)):
            body_parts.append(bytes(body_chunk))
            body_total += len(body_chunk)
        if not message.get("more_body"):
            break
        if body_total > max_body or len(buffered) > max_msgs:
            # Keep buffering until the client signals end -- otherwise
            # downstream handler stalls. The token-parse step below
            # bails on the oversize case by skipping the form parse.
            continue
    if body_total > max_body:
        return None, buffered
    body = b"".join(body_parts)
    if not body:
        return None, buffered
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=False)
    except UnicodeDecodeError:
        return None, buffered
    values = parsed.get(CSRF_FORM_FIELD)
    if not values:
        return None, buffered
    return values[0], buffered


def _replay_receive(buffered: list[Message]) -> Receive:
    """Return a fresh ``Receive`` that drains *buffered* before blocking.

    Once the buffer is exhausted, subsequent ``receive`` calls (e.g. a
    streaming handler waiting on disconnect) would hang forever -- so
    the closure yields a synthetic ``http.disconnect`` after the last
    buffered message so a chunked downstream loop terminates cleanly.
    """
    iterator = iter(buffered)

    async def replay() -> Message:
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}

    return replay


async def _resolve_presented_token(
    scope: Scope,
    receive: Receive,
) -> tuple[str | None, Receive]:
    """Return the presented CSRF token + a (possibly-replayed) ``receive``.

    Tries the ``X-CSRF-Token`` request header first; falls back to the
    ``csrf_token`` form field on a form-urlencoded request body. Form-
    body parsing drains the ASGI receive stream, so the function
    returns a replayed receive that yields the buffered messages back
    to the downstream handler when the body fallback ran. When the
    header is present the original ``receive`` is returned untouched.
    """
    header_token = _extract_header(scope, CSRF_HEADER_NAME)
    if header_token is not None:
        return header_token, receive
    content_type = _extract_header(scope, "content-type") or ""
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return None, receive
    form_token, buffered = await _extract_form_token(receive)
    return form_token, _replay_receive(buffered)


def _validate_csrf(
    *,
    session_id: str | None,
    cookie_token: str | None,
    presented_token: str | None,
) -> str | None:
    """Return ``None`` on success, or the rejection reason string on failure.

    Encapsulates the four-stage validation chain so the middleware
    ``__call__`` stays a thin orchestrator:

    1. Missing session cookie -- the UISessionMiddleware would have
       already redirected an unauthenticated ``GET``; a state-changing
       request without a session is rejected outright.
    2. Missing token half (cookie or header) -- the double-submit
       contract needs both presented.
    3. Cookie value != presented value -- guards the naive
       double-submit failure mode (cookie injection from a same-site
       attacker echoing back an arbitrary value).
    4. HMAC signature mismatch against the session_id -- the binding
       that lifts the pattern to OWASP's "signed double-submit"
       posture.
    """
    if not session_id:
        return "no_session"
    if not cookie_token or not presented_token:
        return "missing_token"
    if not hmac.compare_digest(cookie_token, presented_token):
        return "value_mismatch"
    if not verify_csrf_token(session_id, presented_token):
        return "signature_invalid"
    return None


class CSRFMiddleware:
    """Pure-ASGI middleware enforcing the double-submit CSRF token on ``/ui/*``.

    Stateless beyond ``self.app`` -- one instance handles concurrent
    requests. Verification logic lives in :func:`verify_csrf_token`
    and the ``mint_csrf_token`` helper the dashboard handler calls;
    this class only sequences the ASGI parsing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = _select_path(scope)
        if not path.startswith(_UI_PREFIX):
            # Out-of-prefix -- /api/* and /mcp/* have their own auth
            # and idempotency story. The CSRF middleware is /ui-only.
            await self.app(scope, receive, send)
            return

        method = _select_method(scope)
        if method in _SAFE_METHODS:
            # Read-only methods cannot mutate state, so a CSRF token
            # check is by definition not required. The double-submit
            # cookie is set on the GET response by the dashboard
            # handler (and refreshed on every authenticated render);
            # subsequent state-changing requests carry the cookie back.
            await self.app(scope, receive, send)
            return

        log = structlog.get_logger(__name__)
        session_id = _extract_cookie(scope, SESSION_COOKIE_NAME)
        cookie_token = _extract_cookie(scope, CSRF_COOKIE_NAME)

        # Cheap pre-checks BEFORE draining the body. ``_resolve_presented_token``
        # can buffer up to 256 KiB of form body to read the ``csrf_token``
        # field; doing that work for a request that's about to be rejected
        # on a missing session cookie or missing CSRF cookie is wasted
        # allocation and a DoS-amplification vector (an unauthenticated
        # attacker forces the chassis to buffer a 256 KiB body per request
        # before the inevitable 403). The validation chain in
        # ``_validate_csrf`` already short-circuits on these two reasons,
        # so we run the same checks here -- ahead of the body parse -- and
        # only call ``_resolve_presented_token`` when both halves of the
        # double-submit cookie are actually present.
        if not session_id:
            log.info("ui_csrf_rejected", path=path, method=method, reason="no_session")
            await _send_forbidden(send, "no_session")
            return
        if not cookie_token:
            log.info("ui_csrf_rejected", path=path, method=method, reason="missing_token")
            await _send_forbidden(send, "missing_token")
            return

        presented_token, replay = await _resolve_presented_token(scope, receive)

        rejection = _validate_csrf(
            session_id=session_id,
            cookie_token=cookie_token,
            presented_token=presented_token,
        )
        if rejection is not None:
            log.info(
                "ui_csrf_rejected",
                path=path,
                method=method,
                reason=rejection,
            )
            await _send_forbidden(send, rejection)
            return

        await self.app(scope, replay, send)
