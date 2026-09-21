# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSphere Supervisor (WCP) SSO auth mode for the Kubernetes connector.

A vSphere Supervisor (the WCP "Supervisor cluster" fronting a vSphere
workload domain) is a real Kubernetes cluster, but it **cannot mint a
static/durable credential**: WCP owns the cluster RBAC and even
``administrator@vsphere.local`` is not cluster-admin, so the read-only
ServiceAccount + non-expiring-token approach a normal appliance cluster
allows is unavailable. The only credential a Supervisor issues is the
**short-lived vSphere-SSO token** returned by the ``POST /wcp/login``
exchange (it expires with the SSO session). The static-kubeconfig path
in :mod:`~meho_backplane.connectors.kubernetes.kubeconfig` cannot consume
that durably.

This module is the connector-held equivalent of what ``kubectl vsphere
login`` / ``kubelogin`` do: the connector holds the vSphere SSO
``{username, password}`` and performs the login exchange itself to
**mint -> cache -> refresh** the Supervisor bearer token transparently.

Key mechanism — ``kubernetes_asyncio`` token refresh
====================================================

``kubernetes_asyncio.client.Configuration`` exposes an async-capable
``refresh_api_key_hook``: :meth:`Configuration.get_api_key_with_prefix`
awaits it before reading ``api_key["BearerToken"]`` on **every** request.
We attach a hook that re-mints the Supervisor token once the cached one
is inside a refresh margin of its expiry, so the cached
:class:`~kubernetes_asyncio.client.ApiClient` keeps working across the
token's lifetime with no per-op login round-trip. This is the seam the
static kubeconfig path lacks (its token never refreshes), which is the
exact gap that blocked registering a Supervisor as a ``k8s-1.x`` target.

Two field-note behaviours this module honours
=============================================

* **Internal-VIP redirect avoidance.** The ``/wcp/login`` response
  carries a ``kube_config`` whose ``server`` is the Supervisor's *raw
  internal VIP* — unreachable when the Supervisor is dialed over a NAT'd
  alias (a lab / operator-VPN norm). We take the CA from that
  ``kube_config`` but **override the host to the operator-reachable
  ``target.host``** (the top-level Supervisor kube-API context) and never
  follow the internal-VIP workload-session redirects. When the reachable
  alias differs from the cert's SAN, the target's ``tls_server_name``
  names the address to verify the presented cert against, so the CA chain
  **and** the hostname both stay checked — the same SNI / cert-verify name
  is threaded onto both the login POST and the kube-API leg.
* **Least privilege.** The SSO super-admin works but the recommended
  long-term credential is a scoped read-only vSphere SSO user granted a
  read-only vSphere-Namespace role. This module is credential-agnostic —
  it uses whatever SSO ``{username, password}`` the target's secret
  stages; the scoping is an operator-side choice.

TLS trust bootstrap
===================

The login POST needs a trust anchor *before* the CA-bearing
``kube_config`` exists, so its TLS follows the target's knobs exactly
like the shared HTTP transport (``adapters/http.py``): a
``tls_ca_pin`` (the Supervisor CA, staged out-of-band) is verified
against; otherwise ``verify_tls`` toggles system-CA verification on/off.
The cert is asserted against ``tls_server_name`` (falling back to the
dial ``host``) — so a NAT-fronted Supervisor whose cert SANs an internal
VIP verifies with hostname checking on rather than failing the login
handshake. A self-signed Supervisor therefore needs either the CA pinned
on the target (recommended) or ``verify_tls=false`` (lab) — the same
trust bootstrap ``kubectl vsphere login`` needs a thumbprint or
``--insecure-skip-tls-verify`` for.

No secret material is ever logged: structlog events carry host / port /
mode only, never the SSO credentials or the minted token.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import ssl
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
import yaml
from kubernetes_asyncio.client.configuration import Configuration
from kubernetes_asyncio.config import load_kube_config_from_dict

__all__ = [
    "DEFAULT_WCP_LOGIN_TIMEOUT_SECONDS",
    "DEFAULT_WCP_TOKEN_TTL_SECONDS",
    "WCP_LOGIN_PATH",
    "WCP_LOGIN_PORT",
    "WCP_TOKEN_REFRESH_MARGIN_SECONDS",
    "WcpLoginError",
    "WcpToken",
    "build_wcp_api_configuration",
    "wcp_login",
]

_log = structlog.get_logger(__name__)

#: The Supervisor auth endpoint. WCP serves the SSO token exchange on the
#: control-plane HTTPS front (port 443), distinct from the Kubernetes API
#: server on 6443 that the returned kubeconfig points at.
WCP_LOGIN_PATH: str = "/wcp/login"

#: Port the ``/wcp/login`` exchange listens on. The Supervisor front-end
#: (auth + downloads UI) is on 443; the Kubernetes API server the minted
#: token authenticates to is a separate port (``target.port``, default
#: 6443). A non-default login port is a documented refinement (extras
#: override) if a NAT alias remaps it — the standard WCP port is 443.
WCP_LOGIN_PORT: int = 443

#: httpx timeout for the login POST.
DEFAULT_WCP_LOGIN_TIMEOUT_SECONDS: float = 15.0

#: Fallback token lifetime when the minted token is not a decodable JWT
#: (so no ``exp`` claim is readable). Deliberately short: a Supervisor
#: SSO token is normally a JWT and its real ``exp`` drives refresh, so
#: this fallback rarely triggers; keeping it short bounds how long a
#: silently-shorter-than-assumed SSO session could serve a stale token.
DEFAULT_WCP_TOKEN_TTL_SECONDS: float = 600.0

#: Re-mint this many seconds before the token's stamped expiry, absorbing
#: clock skew and in-flight request latency.
WCP_TOKEN_REFRESH_MARGIN_SECONDS: float = 60.0


class WcpLoginError(RuntimeError):
    """The ``/wcp/login`` SSO exchange failed or returned an unusable body.

    Raised on a non-200 status, a body missing ``session_id`` /
    ``kube_config``, or a ``kube_config`` that does not parse to a
    mapping. The message never echoes the SSO credentials or the response
    body (which carries the token).
    """


@dataclass(frozen=True, slots=True)
class WcpToken:
    """A minted Supervisor bearer token plus its monotonic expiry.

    ``expires_at_monotonic`` is stamped against :func:`time.monotonic`
    (not wall-clock) so a system clock jump cannot make a live token look
    expired or vice versa — the same discipline the GitHub App
    installation-token cache uses.
    """

    token: str
    expires_at_monotonic: float


def _apply_bearer(configuration: Configuration, token: WcpToken) -> None:
    """Write *token* as ``api_key['BearerToken']`` on *configuration*.

    ``load_kube_config_from_dict`` bakes the ``"Bearer "`` prefix into the
    api_key value with an empty ``api_key_prefix``; match that shape so
    the ``authorization`` header renders identically whether the bearer
    came from a kubeconfig or from a WCP re-mint.
    """
    configuration.api_key["BearerToken"] = f"Bearer {token.token}"


class _WcpTokenRefresher:
    """Async ``refresh_api_key_hook`` that re-mints the Supervisor token.

    Holds the login parameters and the current token behind a
    single-flight lock. ``kubernetes_asyncio`` calls (and awaits) the hook
    before every request; it re-mints only once the cached token is inside
    :data:`WCP_TOKEN_REFRESH_MARGIN_SECONDS` of expiry, writing the fresh
    bearer straight into the live :class:`Configuration` so the cached
    client keeps working with no rebuild.
    """

    def __init__(self, host: str, initial: WcpToken, **login_kwargs: Any) -> None:
        self._host = host
        self._login_kwargs = login_kwargs
        self._token = initial
        self._lock = asyncio.Lock()

    @property
    def token(self) -> WcpToken:
        return self._token

    def _fresh_enough(self) -> bool:
        margin = WCP_TOKEN_REFRESH_MARGIN_SECONDS
        return time.monotonic() < self._token.expires_at_monotonic - margin

    async def __call__(self, configuration: Configuration) -> None:
        if self._fresh_enough():
            return
        async with self._lock:
            # Re-check under the lock: a single-flight winner may already
            # have re-minted while this coroutine waited.
            if self._fresh_enough():
                return
            self._token, _ = await wcp_login(self._host, **self._login_kwargs)
            _apply_bearer(configuration, self._token)


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url JWT segment, restoring stripped ``=`` padding."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _jwt_exp(token: str) -> float | None:
    """Best-effort read of a JWT ``exp`` claim — no signature verification.

    The Supervisor token is our own credential; we decode its ``exp``
    purely to schedule proactive refresh, never for an auth decision, so
    an unverified read is sound (the same pattern client-go's exec
    plugins use). Returns ``None`` for any non-JWT / unreadable token so
    the caller falls back to :data:`DEFAULT_WCP_TOKEN_TTL_SECONDS`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


def _token_expiry_monotonic(token: str, *, now_wall: float, now_monotonic: float) -> float:
    """Map a token's wall-clock ``exp`` onto the monotonic clock.

    Prefers the JWT ``exp`` claim (accurate to the SSO session's real
    lifetime); falls back to a short fixed TTL when the token carries no
    readable ``exp``. The refresh margin is applied by the consumer at
    check time, not baked in here.
    """
    exp_wall = _jwt_exp(token)
    if exp_wall is not None:
        remaining = exp_wall - now_wall
        if remaining > 0:
            return now_monotonic + remaining
    return now_monotonic + DEFAULT_WCP_TOKEN_TTL_SECONDS


def _wcp_server_hostname(host: str, tls_server_name: str | None) -> str:
    """The single SNI / cert-verify name **both** legs assert the cert against.

    The operator's ``tls_server_name`` override, falling back to the dial
    ``host``. Shared by the login POST (``sni_hostname`` extension) and the
    kube-API leg (``Configuration.tls_server_name`` → aiohttp
    ``server_hostname``) so the two can never drift: a NAT-fronted
    Supervisor whose cert SANs an internal VIP (dial ``host`` ≠ SAN)
    verifies precisely when ``tls_server_name`` names that SAN.
    """
    return tls_server_name or host


@dataclass(frozen=True, slots=True)
class _WcpTls:
    """Resolved TLS policy for the login POST (httpx).

    ``verify`` is the value handed to :class:`httpx.AsyncClient`'s
    ``verify=`` — an :class:`ssl.SSLContext` when a CA is pinned, else the
    ``verify_tls`` bool. ``server_hostname`` is the shared cert-verify name
    from :func:`_wcp_server_hostname`. The API leg draws its trust anchor
    from the ``/wcp/login`` response's ``kube_config`` (not ``ca_pem``), so
    it consumes only ``server_hostname``, never this ``verify`` context.
    """

    verify: ssl.SSLContext | bool
    server_hostname: str


def _wcp_tls(
    *, host: str, verify_tls: bool, ca_pem: str | None, tls_server_name: str | None
) -> _WcpTls:
    """Resolve the login POST's TLS policy — ``tls_ca_pin`` wins, else ``verify_tls``.

    Mirrors the shared HTTP transport's precedence
    (``adapters/http.py``): a pinned CA is verified against with hostname
    checking on; otherwise ``verify_tls`` toggles default system-CA
    verification. The cert-verify / SNI name comes from
    :func:`_wcp_server_hostname` so the login leg asserts the cert against
    the same name the API leg does, instead of always asserting the dial
    host.
    """
    verify: ssl.SSLContext | bool = (
        ssl.create_default_context(cadata=ca_pem) if ca_pem else verify_tls
    )
    return _WcpTls(verify=verify, server_hostname=_wcp_server_hostname(host, tls_server_name))


def _is_tls_verification_error(exc: BaseException) -> bool:
    """True when *exc*'s cause/context chain carries an :class:`ssl.SSLError`.

    ``httpx`` surfaces a handshake / certificate-verification failure as an
    :class:`httpx.ConnectError` that wraps (via ``raise ... from``) the
    underlying :class:`ssl.SSLError`. Walking the ``__cause__`` /
    ``__context__`` chain lets a hostname or CA mismatch on the login leg
    read as an actionable TLS failure rather than a bare connect error.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _coerce_kube_config(raw: object) -> dict[str, Any]:
    """Normalise the response ``kube_config`` (YAML string or mapping) to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise WcpLoginError(f"/wcp/login kube_config failed to parse as YAML: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise WcpLoginError(
        f"/wcp/login response kube_config is not a kubeconfig mapping (got {type(raw).__name__})"
    )


async def wcp_login(
    host: str,
    *,
    username: str,
    password: str,
    verify_tls: bool,
    ca_pem: str | None,
    tls_server_name: str | None = None,
    login_port: int = WCP_LOGIN_PORT,
    timeout: float = DEFAULT_WCP_LOGIN_TIMEOUT_SECONDS,
    now_wall: float | None = None,
    now_monotonic: float | None = None,
) -> tuple[WcpToken, dict[str, Any]]:
    """Perform the ``POST /wcp/login`` SSO exchange for the Supervisor.

    Sends HTTP Basic ``{username, password}`` (the vSphere SSO
    credential) to ``https://{host}:{login_port}/wcp/login`` — no body,
    which selects the **top-level Supervisor context** (a
    ``guest_cluster_*`` body would select a per-workload sub-session, the
    internal-VIP redirect the field note says to avoid). Returns the
    minted :class:`WcpToken` (expiry stamped from the JWT ``exp`` when
    present) and the response ``kube_config`` mapping (the source of the
    Supervisor CA the caller builds TLS trust from).
    """
    tls = _wcp_tls(host=host, verify_tls=verify_tls, ca_pem=ca_pem, tls_server_name=tls_server_name)
    url = f"https://{host}:{login_port}{WCP_LOGIN_PATH}"
    try:
        async with httpx.AsyncClient(verify=tls.verify, timeout=timeout) as http:
            resp = await http.post(
                url,
                auth=(username, password),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                # Verify the presented cert against tls_server_name (SNI /
                # cert-verify name), not the dial host, so a NAT-fronted
                # Supervisor whose cert SANs an internal VIP still verifies.
                extensions={"sni_hostname": tls.server_hostname},
            )
    except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
        if _is_tls_verification_error(exc):
            raise WcpLoginError(
                f"TLS verification failed for {host!r} "
                f"(server_name {tls.server_hostname!r}): {type(exc).__name__} — "
                "the pinned CA or the cert SAN does not match; "
                "check tls_ca_pin / tls_server_name"
            ) from exc
        raise WcpLoginError(
            f"/wcp/login exchange to {host!r} failed: {type(exc).__name__}"
        ) from exc

    if resp.status_code != 200:
        raise WcpLoginError(
            f"/wcp/login exchange to {host!r} returned HTTP {resp.status_code} "
            "(check the SSO credential and that the target is a vSphere Supervisor)"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise WcpLoginError(f"/wcp/login response from {host!r} was not JSON") from exc
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise WcpLoginError(f"/wcp/login response from {host!r} carried no session_id")
    kube_config = _coerce_kube_config(body.get("kube_config"))

    wall = now_wall if now_wall is not None else time.time()
    mono = now_monotonic if now_monotonic is not None else time.monotonic()
    token = WcpToken(
        token=session_id,
        expires_at_monotonic=_token_expiry_monotonic(session_id, now_wall=wall, now_monotonic=mono),
    )
    _log.info("wcp_login_minted", host=host, login_port=login_port)
    return token, kube_config


async def build_wcp_api_configuration(
    *,
    host: str,
    api_port: int,
    username: str,
    password: str,
    verify_tls: bool,
    ca_pem: str | None,
    tls_server_name: str | None = None,
    login_port: int = WCP_LOGIN_PORT,
    timeout: float = DEFAULT_WCP_LOGIN_TIMEOUT_SECONDS,
) -> Configuration:
    """Build a Supervisor-authenticated ``Configuration`` with self-refresh.

    Performs the initial :func:`wcp_login`, takes the Supervisor CA from
    the returned ``kube_config``, then **overrides the host to the
    operator-reachable ``host:api_port``** (never the internal-VIP
    ``server`` in the kube_config) and installs an async
    ``refresh_api_key_hook`` that re-mints the token before expiry. The
    presented cert is verified against ``tls_server_name`` (falling back to
    ``host``) — the same SNI / cert-verify name the login leg uses — so a
    NAT-fronted Supervisor whose cert SANs an internal VIP verifies with
    hostname checking on. The returned ``Configuration`` drives an ordinary
    :class:`~kubernetes_asyncio.client.ApiClient` (read/inventory ops) or
    a :class:`~kubernetes_asyncio.stream.ws_client.WsApiClient` (exec).
    """
    login_kwargs: dict[str, Any] = {
        "username": username,
        "password": password,
        "verify_tls": verify_tls,
        "ca_pem": ca_pem,
        "tls_server_name": tls_server_name,
        "login_port": login_port,
        "timeout": timeout,
    }
    token, kube_config = await wcp_login(host, **login_kwargs)

    cfg: Configuration = type.__call__(Configuration)
    # Reuse the kubeconfig loader purely to lift the Supervisor CA (and
    # write it to the temp CA file ``ssl_ca_cert`` points at); host and
    # bearer are overridden immediately below.
    await load_kube_config_from_dict(config_dict=kube_config, client_configuration=cfg)

    cfg.host = f"https://{host}:{api_port}"
    if not verify_tls:
        cfg.verify_ssl = False
    else:
        # We dial the reachable alias, not the Supervisor cert's
        # internal-VIP SAN, so verify the cert against tls_server_name
        # (falling back to the dial host) rather than the alias — the same
        # SNI / cert-verify name the login leg uses. kubernetes_asyncio's
        # rest client maps ``tls_server_name`` onto the aiohttp
        # ``server_hostname`` (SNI + cert hostname check), keeping the CA
        # chain verified with hostname checking on.
        cfg.tls_server_name = _wcp_server_hostname(host, tls_server_name)

    _apply_bearer(cfg, token)
    cfg.refresh_api_key_hook = _WcpTokenRefresher(host, token, **login_kwargs)
    return cfg
