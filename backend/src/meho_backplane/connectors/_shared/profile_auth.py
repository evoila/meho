# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Named auth-scheme extractors for ``ProfiledRestConnector`` (#1970).

G0.28-T4 — the runtime half of the named-auth catalog T3 (#1969) defined as
a closed :data:`~meho_backplane.connectors.profile.AuthSchemeName` Literal.
Each catalog value selects one vetted Python extractor here; this module is
the registry that maps a scheme name to that extractor. No DSL — the
profile names a scheme, the scheme names a function, the function returns a
``dict[str, str]`` (the substrate-minimalism line #1177 holds: the profile
configures *which* reviewed extractor runs, never *how* a token is parsed).

The named schemes split on whether they hold session state:

* **Stateless per request** — ``basic`` (HTTP Basic from
  ``username``/``password``) and ``static_header`` (a pre-issued token
  placed bearer-wrapped or raw). These compute the header from the secret
  bundle on every call; no token cache, no login round-trip.
* **Session-stateful** — ``session_login`` (POST credentials to a login
  endpoint, read a short-lived token out of the JSON body, send it as
  ``Bearer``; vRLI's shape), ``session_login_basic`` (POST with HTTP Basic
  credentials and no body, read the raw JSON-string token out of the
  response body, send it in a bespoke header; vCenter's ``/api/session``
  shape, #2025, with the vetted modern→legacy 404 fallback of #2031) and
  ``oauth2_mint`` (an OAuth2 client-credentials *form*
  grant minting a ``Bearer`` token with a TTL; keycloak's shape). These
  need the per-target lock / token cache / single-flight /
  TTL-or-expiry-driven refresh / fail-closed harness, which
  :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` owns
  **once** (the whole point of T4 — the harness was duplicated across vRLI
  and keycloak before).

This module supplies the *scheme-specific* pieces the harness drives:

* :func:`build_static_headers` — the stateless ``basic`` / ``static_header``
  computation.
* :class:`SessionSchemeSpec` — the per-scheme login mechanics
  (login path builder, request encoding, token + TTL extractor) the
  stateful harness invokes. :data:`SESSION_SCHEME_SPECS` registers one per
  session scheme, selected by the profile's ``auth.scheme``.

The login round-trip itself reuses the audited transport seams on
:class:`~meho_backplane.connectors.adapters.http.HttpConnector`
(``_post_json``'s ``json=`` / ``data=`` body shapes from T2 #1968) so a
profiled connector inherits the same retry / TLS-trust / pooling behaviour
the typed connectors have.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meho_backplane.connectors.profile import AuthSpec

__all__ = [
    "SESSION_SCHEME_SPECS",
    "STATELESS_SCHEMES",
    "LegacyFallback",
    "ProfileAuthError",
    "SessionSchemeSpec",
    "SessionToken",
    "build_static_headers",
]


class ProfileAuthError(RuntimeError):
    """A profile-driven auth extractor could not produce a usable result.

    Raised when a named scheme's secret bundle is missing a field the
    scheme declared in ``auth.secret_fields`` (a misconfigured profile /
    Vault secret), or a session-login response carried no usable token.
    The message names the scheme and the offending field / target; it
    never echoes a credential value.
    """


# ---------------------------------------------------------------------------
# Secret-bundle field access (shared by every scheme)
# ---------------------------------------------------------------------------


def _require_field(secret: Mapping[str, str], field: str, *, scheme: str) -> str:
    """Return ``secret[field]`` or raise a profile-named error.

    The credential loader (``load_basic_credentials`` with the profile's
    ``secret_fields``) already raises when a *declared* field is absent in
    Vault, so this is the defensive second gate for an extractor reading a
    field the profile didn't declare — a programming error in the scheme
    wiring rather than an operator misconfiguration. Never echoes the value.
    """
    value = secret.get(field)
    if not value:
        raise ProfileAuthError(
            f"{scheme!r} auth scheme requires a non-empty {field!r} secret field; "
            f"the resolved secret bundle has no usable {field!r} value"
        )
    return value


# ---------------------------------------------------------------------------
# Stateless schemes — basic / static_header
# ---------------------------------------------------------------------------

#: The two named schemes that compute their header per request with no
#: session state. The session-stateful schemes (``session_login`` /
#: ``oauth2_mint``) are everything in
#: :data:`~meho_backplane.connectors.profile.NAMED_AUTH_SCHEMES` minus these.
STATELESS_SCHEMES: frozenset[str] = frozenset({"basic", "static_header"})


def _basic_auth_value(username: str, password: str) -> str:
    """Return the ``Basic <b64(user:pass)>`` header value.

    Same UTF-8-then-base64 encoding the typed connectors use
    (:func:`meho_backplane.connectors._shared.vcf_auth.basic_auth_header`);
    duplicated as a one-liner here rather than imported so the profiled
    auth path carries no coupling to the VCF-specific module.
    """
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def build_static_headers(auth: AuthSpec, secret: Mapping[str, str]) -> dict[str, str]:
    """Compute the per-request headers for a stateless scheme.

    Handles the two no-session schemes:

    * ``basic`` — reads ``username`` / ``password`` from *secret* and
      returns ``{auth.header_name: "Basic <b64>"}``.
    * ``static_header`` — reads the single pre-issued token field and places
      it per ``auth.value_kind``: ``bearer`` wraps it as ``"Bearer <tok>"``,
      ``raw`` places it verbatim (an ``X-Api-Key``-style header).

    The token field for ``static_header`` is the *first* name in
    ``auth.secret_fields`` (the profile declares exactly the field the
    pre-issued token lives under). Raises :class:`ProfileAuthError` for a
    scheme this builder does not handle — the caller's stateful path owns
    ``session_login`` / ``oauth2_mint``.
    """
    if auth.scheme == "basic":
        username = _require_field(secret, "username", scheme="basic")
        password = _require_field(secret, "password", scheme="basic")
        return {auth.header_name: _basic_auth_value(username, password)}
    if auth.scheme == "static_header":
        token_field = auth.secret_fields[0]
        token = _require_field(secret, token_field, scheme="static_header")
        # value_kind is required for static_header (AuthSpec enforces it),
        # so it is never None here; bearer wraps, raw places verbatim.
        value = f"Bearer {token}" if auth.value_kind == "bearer" else token
        return {auth.header_name: value}
    raise ProfileAuthError(
        f"build_static_headers does not handle scheme {auth.scheme!r}; "
        f"session schemes route through the session-token harness"
    )


# ---------------------------------------------------------------------------
# Session-stateful schemes — session_login / oauth2_mint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionToken:
    """A minted session token plus its optional TTL.

    ``ttl_seconds`` is ``None`` for a scheme whose login response carries no
    expiry (vRLI's session is idle-expiry-driven, recovered by a re-login on
    a downstream session-expiry status rather than a proactive TTL refresh).
    A finite ``ttl_seconds`` (keycloak's ``expires_in``) lets the harness
    re-mint before the token would fail a downstream call.
    """

    token: str
    ttl_seconds: float | None


@dataclass(frozen=True)
class LegacyFallback:
    """A vetted modern→legacy session-endpoint pair for a single scheme (#2031).

    Some appliances expose the same session-establish semantics at two
    different paths depending on deployment vintage: a *modern* path served
    by current releases and a *legacy* path served by older releases (and by
    the vendor's test simulator). The credentials and response shape are
    identical across the pair — only the path (and the API mount the
    subsequent ops live under) differs.

    This is a **closed, per-scheme constant**, not a profile-supplied knob:
    the scheme names exactly one vetted pair, the profile cannot widen it,
    and there is no free-form list of candidate login paths (the #1177 /
    #1969 no-DSL line). The only scheme that currently declares a fallback is
    ``session_login_basic`` (vCenter's modern ``/api/session`` →
    legacy ``/rest/com/vmware/cis/session``), mirroring the typed
    :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`.

    Attributes
    ----------
    legacy_login_path
        The legacy session-establish path tried **only** when the modern
        :attr:`SessionSchemeSpec.login_path` returns HTTP 404. A 401 / 403 /
        5xx on the modern path is an auth / server failure, not "this
        deployment lacks the modern endpoint", and does **not** trigger the
        legacy attempt.
    modern_op_mount
        The API mount prefix ingested ops live under when the *modern* login
        path won (vCenter modern: ``/api``).
    legacy_op_mount
        The API mount prefix ingested ops live under when the *legacy* login
        path won (vCenter legacy / simulator: ``/rest``).
    """

    legacy_login_path: str
    modern_op_mount: str
    legacy_op_mount: str

    def op_mount_for_login_path(self, login_path: str) -> str:
        """Return the op-mount prefix the *winning* login path implies.

        The legacy login path selects :attr:`legacy_op_mount`; any other
        recorded path (the modern path, or an unrecognised value) selects
        :attr:`modern_op_mount` so a future addition fails toward the
        production-correct mount rather than silently misrouting every op.
        Mirrors the typed connector's ``api_mount_for_session_path``.
        """
        if login_path == self.legacy_login_path:
            return self.legacy_op_mount
        return self.modern_op_mount


@dataclass(frozen=True)
class SessionSchemeSpec:
    """The scheme-specific mechanics of a session-stateful named scheme.

    The connector-owned harness drives the lock / cache / single-flight /
    refresh; this spec supplies what differs per scheme — the login path,
    how the login carries its credentials, the request body encoding, the
    request headers, how the token (plus TTL) is read out of the response,
    and how it is applied to downstream requests. One instance per session
    scheme lives in :data:`SESSION_SCHEME_SPECS`, selected by the profile's
    ``auth.scheme``. Every field is a vetted per-scheme constant — none is a
    profile-supplied knob, so the closed-catalog / no-DSL line (#1177)
    holds: the profile names a scheme, the scheme fixes the shape.

    Attributes
    ----------
    login_path
        Builds the login endpoint path from the resolved ``AuthSpec`` (a
        constant for vRLI; keycloak's realm path is profile-independent so
        it is also constant here — realm routing is a T6 concern).
    login_credentials
        Where the login round-trip carries its credentials. ``"body"``
        sends them in the request body built by :attr:`build_body` (vRLI's
        JSON creds, keycloak's form grant). ``"basic"`` sends them as an
        HTTP Basic ``Authorization`` header on the login POST with an empty
        body (vCenter's ``POST /api/session``).
    encoding
        ``"json"`` serialises the login body as JSON (vRLI);
        ``"form"`` serialises it as ``application/x-www-form-urlencoded``
        (OAuth2 token grants). Ignored when ``login_credentials="basic"``
        (the login carries no body). Picks the matching ``_post_json`` body
        slot.
    build_body
        Builds the login request body from the secret bundle. Returns an
        empty mapping for a ``login_credentials="basic"`` scheme (the creds
        ride the Basic header, not the body).
    build_login_auth
        Returns the ``(username, password)`` HTTP Basic credentials the
        login POST is sent with, or ``None`` for a ``login_credentials="body"``
        scheme (whose creds ride the body instead). Keeps secret-field
        knowledge inside this module — the harness just forwards the tuple
        to httpx's ``auth=``.
    request_headers
        Static headers sent on the login POST (``Content-Type`` /
        ``Accept``).
    extract_token
        Reads ``(token, ttl)`` out of the parsed JSON response body, or
        ``None`` when no usable token is present (the harness then raises a
        target-named :class:`ProfileAuthError`).
    token_header
        The header name the established session token is written into on
        downstream requests. ``"Authorization"`` for the Bearer schemes;
        vCenter's session token rides a bespoke ``vmware-api-session-id``
        header.
    token_value_kind
        How the established token is placed in :attr:`token_header`.
        ``"bearer"`` wraps it as ``"Bearer <token>"`` (vRLI / keycloak);
        ``"raw"`` places it verbatim (vCenter's session-id header).
    legacy_fallback
        An optional vetted :class:`LegacyFallback` modern→legacy pair (#2031).
        ``None`` for a scheme served at a single path (vRLI / keycloak). When
        present, the harness retries the login at the legacy path on an HTTP
        404 from the modern :attr:`login_path` (404 only) and records the
        winning path so op-path mount + teardown follow it. Only
        ``session_login_basic`` declares one (vCenter's
        ``/api/session`` → ``/rest/com/vmware/cis/session``).
    """

    login_path: Callable[[AuthSpec], str]
    login_credentials: str
    encoding: str
    build_body: Callable[[AuthSpec, Mapping[str, str]], dict[str, str]]
    build_login_auth: Callable[[AuthSpec, Mapping[str, str]], tuple[str, str] | None]
    request_headers: Mapping[str, str]
    extract_token: Callable[[Any], SessionToken | None]
    token_header: str
    token_value_kind: str
    legacy_fallback: LegacyFallback | None = None


def _no_login_auth(_auth: AuthSpec, _secret: Mapping[str, str]) -> tuple[str, str] | None:
    """No HTTP Basic credentials on the login POST.

    The body-carried schemes (``session_login`` / ``oauth2_mint``) send
    their credentials in the request body, so the login POST takes no
    ``auth=`` tuple. The basic-auth schemes override this with a builder
    that reads the credential pair out of the secret bundle.
    """
    return None


# -- session_login (vRLI: json login -> body .sessionId -> Bearer) ----------

#: vRLI's session-establish endpoint. The connector POSTs the JSON body and
#: reads ``sessionId`` out of the response. Behaviour parity with
#: :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`.
_VRLI_SESSION_PATH = "/api/v2/sessions"

#: vRLI identity-source default, matching the typed connector + wrapper.
_VRLI_DEFAULT_PROVIDER = "Local"


def _session_login_body(auth: AuthSpec, secret: Mapping[str, str]) -> dict[str, str]:
    """Build vRLI's ``{username, password, provider}`` login body.

    ``provider`` is a non-secret constant (``"Local"``) — the profile carries
    no per-target provider knob (that stays a typed-connector concern), so a
    profiled vRLI uses the wrapper's default identity source. ``username`` /
    ``password`` come from the secret bundle the profile declared.
    """
    return {
        "username": _require_field(secret, "username", scheme="session_login"),
        "password": _require_field(secret, "password", scheme="session_login"),
        "provider": _VRLI_DEFAULT_PROVIDER,
    }


def _extract_session_login_token(payload: Any) -> SessionToken | None:
    """Read ``sessionId`` out of the vRLI login response body.

    vRLI returns ``{"sessionId": "<token>", "ttl": <seconds>}``. The
    session is idle-expiry-driven and recovered by a re-login on a
    downstream session-expiry status, so the harness does **not** key a
    proactive refresh off ``ttl`` — ``ttl_seconds`` is ``None``, matching
    the typed connector which caches the token until a 440/401 invalidates
    it. Returns ``None`` for a missing / non-string / empty ``sessionId``
    so the harness raises the consistent target-named error.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("sessionId")
    if not isinstance(value, str) or not value:
        return None
    return SessionToken(token=value, ttl_seconds=None)


# -- session_login_basic (vCenter: basic-auth login -> raw-string token) ----

#: vCenter's modern (vSphere 7.0+) session-establish endpoint. The connector
#: POSTs with HTTP Basic credentials and no body; the response body *is* the
#: session token as a JSON-quoted string. Behaviour parity with
#: :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`'s
#: ``SESSION_PATH_MODERN``. This is the *modern* login path; the profiled
#: harness now also tries the legacy ``/rest/com/vmware/cis/session`` on a
#: 404, mirroring the typed connector (#2031 — see
#: :data:`_VCENTER_LEGACY_FALLBACK`).
_VCENTER_SESSION_PATH = "/api/session"

#: The header a vCenter session token rides on downstream requests, per
#: Broadcom's vSphere Automation API. Matches the typed connector's
#: ``_SESSION_HEADER``.
_VCENTER_SESSION_HEADER = "vmware-api-session-id"

#: The vetted vCenter modern→legacy session-endpoint pair (#2031). When the
#: modern ``POST /api/session`` 404s, the harness retries the legacy
#: ``POST /rest/com/vmware/cis/session`` — the only path the upstream
#: ``vmware/vcsim`` simulator (and very old vCenter) registers. The winning
#: path then selects the op mount: ``/api`` on modern, ``/rest`` on legacy.
#: These constants intentionally duplicate the typed connector's
#: ``vmware_rest/_mount.py`` values rather than importing them, keeping the
#: generic profiled-auth module free of a vendor-module dependency (the same
#: no-coupling decision the duplicated ``_basic_auth_value`` reflects).
_VCENTER_LEGACY_FALLBACK = LegacyFallback(
    legacy_login_path="/rest/com/vmware/cis/session",
    modern_op_mount="/api",
    legacy_op_mount="/rest",
)


def _session_login_basic_body(_auth: AuthSpec, _secret: Mapping[str, str]) -> dict[str, str]:
    """Build vCenter's (empty) login body.

    ``POST /api/session`` carries its credentials as an HTTP Basic
    ``Authorization`` header (see :func:`_session_login_basic_auth`) — not
    in the request body. The body is therefore always empty; this builder
    exists so the spec's ``build_body`` contract is uniform across session
    schemes.
    """
    return {}


def _session_login_basic_auth(auth: AuthSpec, secret: Mapping[str, str]) -> tuple[str, str] | None:
    """Build vCenter's ``(username, password)`` HTTP Basic credentials.

    ``POST /api/session`` authenticates with HTTP Basic, mirroring the typed
    :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`'s
    ``client.post(SESSION_PATH_MODERN, auth=(username, password))``. The
    field names come from the profile's first two declared
    ``secret_fields`` (conventionally ``username`` / ``password``); reading
    them by position keeps this builder field-name-agnostic while still
    failing closed via :func:`_require_field` on a missing value.
    """
    username = _require_field(secret, auth.secret_fields[0], scheme=auth.scheme)
    password = _require_field(secret, auth.secret_fields[1], scheme=auth.scheme)
    return (username, password)


def _extract_session_login_basic_token(payload: Any) -> SessionToken | None:
    """Read the session token out of vCenter's ``/api/session`` response.

    The modern endpoint returns the token as a JSON-quoted **string** — the
    body *is* the token, parsed by ``response.json()`` into :class:`str`
    (parity with the typed connector's ``_extract_session_token`` modern
    shape). The 404 login-path fallback (#2031) reaches the legacy endpoint,
    but the legacy ``{"value": "<token>"}`` response-object shape the typed
    connector also tolerates is not parsed here — the profiled extractor
    reads the JSON-string body only. The session
    has no proactive TTL — it caches until a downstream session-expiry
    status triggers a re-login — so ``ttl_seconds`` is ``None``. Returns
    ``None`` for a non-string / empty body so the harness raises the
    consistent target-named error.
    """
    if not isinstance(payload, str) or not payload:
        return None
    return SessionToken(token=payload, ttl_seconds=None)


# -- oauth2_mint (keycloak: form client-credentials grant -> Bearer) --------

#: Keycloak's token endpoint path. The admin-realm segment in the typed
#: connector is a realm-routing concern T6 owns; the named scheme uses the
#: conventional ``master`` admin realm so a profiled keycloak mints against
#: the same endpoint shape the typed connector does.
_OAUTH2_TOKEN_PATH = "/realms/master/protocol/openid-connect/token"

#: Refresh margin shaved off ``expires_in`` so a near-expiry token is
#: re-minted before a downstream call fails on it. Mirrors the typed
#: keycloak connector's ``_TOKEN_REFRESH_MARGIN_SECONDS``.
_OAUTH2_REFRESH_MARGIN_SECONDS = 30.0

#: Fallback TTL when the token response omits / malforms ``expires_in``.
#: Keycloak's admin access-token lifespan defaults to 60 s; the floor keeps
#: a malformed response from pinning a token forever.
_OAUTH2_DEFAULT_TTL_SECONDS = 60.0


def _oauth2_mint_body(auth: AuthSpec, secret: Mapping[str, str]) -> dict[str, str]:
    """Build the OAuth2 client-credentials grant form body.

    ``grant_type=client_credentials`` with ``client_id`` / ``client_secret``
    from the secret bundle the profile declared. Form-encoded by the
    ``oauth2_mint`` spec's ``encoding="form"`` — Keycloak's token endpoint
    does not accept JSON.
    """
    return {
        "grant_type": "client_credentials",
        "client_id": _require_field(secret, "client_id", scheme="oauth2_mint"),
        "client_secret": _require_field(secret, "client_secret", scheme="oauth2_mint"),
    }


def _extract_oauth2_token(payload: Any) -> SessionToken | None:
    """Read ``access_token`` + effective TTL out of an OAuth2 token response.

    Returns the access token with ``ttl_seconds = expires_in - margin``
    (floored at 1 s), or :data:`_OAUTH2_DEFAULT_TTL_SECONDS` when
    ``expires_in`` is missing / non-numeric. Returns ``None`` for a body
    carrying no usable ``access_token`` so the harness raises the
    target-named error. Mirrors the typed connector's
    ``_parse_token_response``.
    """
    if not isinstance(payload, dict):
        return None
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expires_in = payload.get("expires_in")
    ttl = float(expires_in) if isinstance(expires_in, (int, float)) else _OAUTH2_DEFAULT_TTL_SECONDS
    effective = max(1.0, ttl - _OAUTH2_REFRESH_MARGIN_SECONDS)
    return SessionToken(token=token, ttl_seconds=effective)


#: One :class:`SessionSchemeSpec` per session-stateful named scheme,
#: selected by the profile's ``auth.scheme``. The connector-owned harness
#: looks the spec up here and drives it; adding a session scheme is a
#: deliberate act (a new Literal value in T3 + a vetted spec here).
SESSION_SCHEME_SPECS: dict[str, SessionSchemeSpec] = {
    "session_login": SessionSchemeSpec(
        login_path=lambda _auth: _VRLI_SESSION_PATH,
        login_credentials="body",
        encoding="json",
        build_body=_session_login_body,
        build_login_auth=_no_login_auth,
        request_headers={"Content-Type": "application/json", "Accept": "application/json"},
        extract_token=_extract_session_login_token,
        token_header="Authorization",
        token_value_kind="bearer",
    ),
    "session_login_basic": SessionSchemeSpec(
        login_path=lambda _auth: _VCENTER_SESSION_PATH,
        login_credentials="basic",
        encoding="json",
        build_body=_session_login_basic_body,
        build_login_auth=_session_login_basic_auth,
        request_headers={"Accept": "application/json"},
        extract_token=_extract_session_login_basic_token,
        token_header=_VCENTER_SESSION_HEADER,
        token_value_kind="raw",
        legacy_fallback=_VCENTER_LEGACY_FALLBACK,
    ),
    "oauth2_mint": SessionSchemeSpec(
        login_path=lambda _auth: _OAUTH2_TOKEN_PATH,
        login_credentials="body",
        encoding="form",
        build_body=_oauth2_mint_body,
        build_login_auth=_no_login_auth,
        request_headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        extract_token=_extract_oauth2_token,
        token_header="Authorization",
        token_value_kind="bearer",
    ),
}
