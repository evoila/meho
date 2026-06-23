# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Base for ingested REST connectors made dispatchable by an ExecutionProfile.

G0.28-T1 (#1967) — the **gating** half of Initiative #1965 (make ingested
REST read ops dispatchable from a reviewed declarative profile). The
operation-execution path is already declarative: ``dispatch_ingested`` runs
an ingested op off its stored :class:`~meho_backplane.db.models.EndpointDescriptor`
row with no per-vendor Python. The only hand-coded surface blocking an
ingested REST connector from dispatching is ``auth_headers()`` (plus
``fingerprint`` / ``probe``). The auto-shim
(:class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`)
raises :class:`NotImplementedError` exactly there, so a spec-ingested
connector is non-dispatchable.

:class:`ProfiledRestConnector` is the **sibling** of ``GenericRestConnector``
— a :class:`~meho_backplane.connectors.adapters.http.HttpConnector` subclass,
**not** a ``GenericRestConnector`` subclass — that a vetted ``ExecutionProfile``
plugs into to fill that one slot with reviewed declarative data instead of
hand-written Python. Being a sibling (not a subclass) is load-bearing: the
former ``issubclass(GenericRestConnector)`` dispatchability discriminator
would otherwise silently demote a profiled connector as a dead shim and
strip its profile. G0.28-T1 replaces that binary predicate with the
tri-state :func:`~meho_backplane.connectors.base.shim_kind` classifier; this
class is its ``"profiled"`` tier.

Why ``"profiled"`` is its own tier (not just folded into ``"none"``): a
profiled connector carries a bounded ``supported_version_range`` derived
from the ingested spec's version, which can be *narrower* than a shipped
hand-coded class's broad range. If profiled were classified identically to
a hand-coded class, the resolver's most-specific-version-match step would
let a profiled connector out-specific — and therefore shadow — a bespoke
hand-coded connector for the same ``(product, version)``, reinstating the
#1750/#1798 product-shadowing footgun. The tri-state ladder
(``none`` > ``profiled`` > ``bare``) in
:func:`~meho_backplane.connectors.resolver._demote_lower_dispatch_tiers`
keeps a profiled connector *above* a bare shim (it is dispatchable) but
*below* a hand-coded class (a bespoke connector always wins), with
``priority = 0`` so it never out-ranks on the priority rung either.

The session-lifecycle / token-cache harness (G0.28-T4 #1970)
============================================================

T4 hoists the per-target session machinery — the lock, the token cache, the
single-flight, the re-login-once, the empty-``raw_jwt`` fail-closed gate —
**once** into this class, parameterised by the profile's named auth scheme.
Before T4 this harness was copy-pasted across the typed session connectors
(vRLI, keycloak); a profiled connector now reuses the one audited
implementation. The scheme-specific pieces (login path, body encoding,
token + TTL extraction) live in
:mod:`meho_backplane.connectors._shared.profile_auth`, selected by
``self.profile.auth.scheme``:

* ``basic`` / ``static_header`` — **stateless**: the header is computed from
  the secret bundle on every call, no token cache or login round-trip.
* ``session_login`` (vRLI parity: JSON login → body ``.sessionId`` →
  ``Bearer``) and ``oauth2_mint`` (keycloak parity: form client-credentials
  grant → ``Bearer`` with TTL) — **session-stateful**: driven by the harness
  below. ``session_login`` caches until a downstream re-login (idle-expiry
  driven, no TTL); ``oauth2_mint`` re-mints on TTL expiry.

NSX is **out of scope** and stays typed — its auth depends on the httpx
cookie jar (``JSESSIONID`` via ``Set-Cookie``), which the ``dict[str, str]``
``auth_headers`` return contract cannot model (it is the ``cookie_jar_session``
*reserved* scheme in :data:`~meho_backplane.connectors.profile.RESERVED_AUTH_SCHEMES`).

Auth-model gating + fail-closed invariant
=========================================

Every shipped REST connector hardcodes ``shared_service_account`` and
rejects ``per_user`` / impersonation with a :exc:`NotImplementedError`
naming the target + requested mode. The profiled connector follows the same
pattern. The empty-``raw_jwt`` fail-closed check is preserved as a
security-load-bearing invariant: a system-initiated caller (empty JWT)
cannot read the per-target vendor credential out of Vault, so it must never
be served a session token primed by an authenticated caller via a cache hit
— the guard runs *before* the cache lookup.

Scope of T6 (#1972) — the profile-driven ``fingerprint`` / ``probe`` and
pagination — is still pending; those two methods raise
:class:`NotImplementedError` with a profile-oriented message until T6 lands.
A ``ProfiledRestConnector`` that reaches fingerprint/probe before T6 is
therefore classified ``unsupported_feature`` by the dispatcher, **never**
``unreplaced_auto_shim`` — it is not a dead shim, it is a dispatchable
connector whose remaining wiring is incomplete.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import (
    SESSION_SCHEME_SPECS,
    STATELESS_SCHEMES,
    LegacyFallback,
    ProfileAuthError,
    SessionSchemeSpec,
    SessionToken,
    build_static_headers,
)
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.connectors._shared.vcf_auth import is_acceptable_auth_model
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import ShimKind
from meho_backplane.connectors.profile import AuthSpec, ExecutionProfile, split_version
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["ProfileCredentialsLoader", "ProfiledRestConnector"]

_log = structlog.get_logger(__name__)

#: Async callable resolving a ``(target, operator)`` pair to the secret
#: bundle the profile's auth scheme reads. Injected for tests; the default
#: is the shared operator-context Vault KV-v2 reader. The returned dict's
#: keys are the names the profile declared in ``auth.secret_fields``.
ProfileCredentialsLoader = Callable[[Any, Operator], Awaitable[dict[str, str]]]


def _mounted_path(fallback: LegacyFallback, login_path: str, descriptor_path: str) -> str:
    """Prefix *descriptor_path* with the API mount the winning *login_path* implies.

    A descriptor path already carrying either of the fallback's known mount
    prefixes (``/api/...`` / ``/rest/...`` for vCenter) is returned unchanged
    so an explicitly-mounted descriptor isn't double-prefixed. Otherwise the
    spec-relative path is normalised to a leading slash and prefixed with the
    mount the winning login path selects (modern → ``/api``, legacy →
    ``/rest``). Mirrors the typed connector's
    ``meho_backplane.connectors.vmware_rest._mount.mounted_path``.
    """
    known_prefixes = (f"{fallback.modern_op_mount}/", f"{fallback.legacy_op_mount}/")
    if descriptor_path.startswith(known_prefixes):
        return descriptor_path
    mount = fallback.op_mount_for_login_path(login_path)
    normalised = descriptor_path if descriptor_path.startswith("/") else f"/{descriptor_path}"
    return f"{mount}{normalised}"


class _CachedSessionToken:
    """A minted session token plus the monotonic time it expires at (or never)."""

    __slots__ = ("expires_at", "token")

    def __init__(self, token: str, expires_at: float | None) -> None:
        self.token = token
        #: ``None`` → never proactively expires (vRLI's idle-expiry session,
        #: recovered by a downstream re-login). A finite value → re-mint when
        #: the monotonic clock passes it (keycloak's TTL refresh).
        self.expires_at = expires_at

    def is_fresh(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at


class ProfiledRestConnector(HttpConnector):
    """Sibling of ``GenericRestConnector`` for profile-driven ingested REST.

    A concrete :class:`~meho_backplane.connectors.adapters.http.HttpConnector`
    subclass (inheriting its client pooling / retry / TLS-trust transport)
    classified ``"profiled"`` so the tri-state resolver treats it as
    dispatchable — above a bare auto-shim, below a hand-coded class. Carries
    the default ``priority = 0``; registered profiled classes advertise a
    bounded ``supported_version_range`` (derived from the ingested spec's
    version) so they beat a bare shim on dispatchability but never
    out-specific a bespoke hand-coded class.

    The vetted :class:`~meho_backplane.connectors.profile.ExecutionProfile`
    is a **class attribute** (``profile``): the profile-stamping path
    (G0.28-T5 #1971) registers a ``ProfiledRestConnector`` subclass carrying
    the reviewed profile, and the dispatcher instantiates that subclass with
    no constructor args (one cached instance per class). Tests construct an
    instance directly, passing ``profile=`` (and a stub
    ``credentials_loader=``) to exercise a scheme without a registry round-trip.

    The session/token harness (lock / cache / single-flight / re-login-once /
    TTL refresh / empty-jwt fail-closed) lives here once, parameterised by
    ``self.profile.auth.scheme``; see the module docstring.
    """

    # G0.28-T1 (#1967) — the "profiled" tier of the tri-state classifier.
    _shim_kind: ShimKind = "profiled"

    # Explicit (matches the inherited default) so the resolver-relevant
    # contract is readable at the class: a profiled connector never wins the
    # priority rung against a hand-coded class.
    priority: int = 0

    #: The vetted profile this connector dispatches against. Set on the
    #: stamped subclass by the profile-stamping path; ``None`` on the bare
    #: base class (which is registered only as the resolver's dispatchable
    #: stand-in in tests). ``auth_headers`` raises a clear error when it is
    #: ``None`` rather than dereferencing it.
    profile: ExecutionProfile | None = None

    def __init__(
        self,
        *,
        profile: ExecutionProfile | None = None,
        credentials_loader: ProfileCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        # A constructor-supplied profile (tests) overrides the class
        # attribute; production stamped subclasses set the class attribute
        # and instantiate with no args.
        if profile is not None:
            self.profile = profile
        self._injected_loader = credentials_loader
        # Per-target session-token cache keyed on the tenant-unique
        # ``(tenant_id, id)`` tuple (#1642), guarded by one lock per
        # connector instance. Only the two session-stateful schemes
        # (session_login / oauth2_mint) populate it.
        self._session_tokens: dict[tuple[str, str], _CachedSessionToken] = {}
        # The login path that actually minted each target's session (#2031).
        # Only a scheme with a LegacyFallback (session_login_basic) ever
        # records anything other than its modern path here; the recorded path
        # drives op-path mount (see :meth:`mount_op_path`) and session
        # teardown so a legacy-only target (vcsim) routes ops to ``/rest``
        # instead of defaulting to the modern ``/api`` mount.
        self._session_login_paths: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()

    # -- auth -----------------------------------------------------------

    def _require_profile(self, target: Any) -> ExecutionProfile:
        """Return the attached profile or raise a clear error.

        A profiled connector with no profile is a wiring error (a bare base
        class reached dispatch). The message names the target so the operator
        sees which target tried to dispatch against an unstamped connector.
        """
        if self.profile is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no ExecutionProfile attached; "
                f"target {getattr(target, 'name', '?')!r} cannot dispatch. Stamp a "
                f"reviewed profile (G0.28-T5) before dispatching."
            )
        return self.profile

    async def _load_credentials(self, target: Any, operator: Operator) -> dict[str, str]:
        """Resolve the secret bundle the profile's auth scheme reads.

        An injected loader (tests) wins; otherwise the default operator-context
        Vault read pulls **exactly the fields the profile declared** in
        ``auth.secret_fields`` out of ``target.secret_ref`` via the shared
        :func:`load_basic_credentials` helper. Reading the declared field
        names (rather than the helper's ``username``/``password`` default) is
        what lets ``static_header`` (a ``token`` field) and ``oauth2_mint``
        (``client_id`` / ``client_secret``) resolve the right secret shape
        through the one shared reader — same no-secret-in-logs discipline and
        fail-closed empty-JWT / missing-field error contract every connector
        reuses.
        """
        if self._injected_loader is not None:
            return await self._injected_loader(target, operator)
        profile = self._require_profile(target)
        return await load_basic_credentials(target, operator, fields=profile.auth.secret_fields)

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        """Return the auth headers for *target*, driven by the profile's scheme.

        Rejects any ``auth_model`` other than ``shared_service_account`` /
        ``None`` with a :exc:`NotImplementedError` naming the target + mode
        — the same pattern every typed REST connector uses (``per_user`` /
        impersonation is out of scope; a profile is a shared-service-account
        construct).

        Stateless schemes (``basic`` / ``static_header``) compute the header
        from the freshly resolved secret bundle. Session-stateful schemes
        return ``{spec.token_header: <placed token>}`` for a token obtained
        through the session harness (cached, single-flight,
        refresh-on-expiry, fail-closed): ``session_login`` / ``oauth2_mint``
        place a ``"Bearer <token>"`` in ``Authorization``;
        ``session_login_basic`` places the raw token in vCenter's
        ``vmware-api-session-id`` header.
        """
        profile = self._require_profile(target)
        auth = profile.auth
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"{type(self).__name__} only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{getattr(target, 'name', '?')!r} requested auth_model={auth_model!r}"
            )

        if auth.scheme in STATELESS_SCHEMES:
            # Stateless: the credential read still fails closed on an empty
            # operator JWT inside the loader; no token cache is involved.
            secret = await self._load_credentials(target, operator)
            return build_static_headers(auth, secret)

        spec = self._session_spec(auth.scheme)
        token = await self._session_token(target, operator)
        value = f"Bearer {token}" if spec.token_value_kind == "bearer" else token
        return {spec.token_header: value}

    # -- op-path mount (modern/legacy endpoint fallback) ----------------

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        """Map a spec-relative ingested-op *path* onto *target*'s live mount.

        Overrides the identity :meth:`HttpConnector.mount_op_path` hook the
        dispatcher calls for ``source_kind='ingested'`` ops. A profile whose
        session scheme declares no
        :class:`~meho_backplane.connectors._shared.profile_auth.LegacyFallback`
        (vRLI / keycloak / the stateless schemes) needs no remount — the
        descriptor path is reachable verbatim, so this returns it unchanged.

        For a fallback-bearing scheme (vCenter's ``session_login_basic``,
        #2031) the vCenter REST surface is mounted at ``/api`` on modern and
        ``/rest`` on legacy / vcsim. Establishing the session is what records
        the live login path (the modern→legacy 404 fallback in
        :meth:`_mint_session_token`); it is idempotent + cached, so the call
        here costs nothing on the warm path and is what lets the *first* op
        against a legacy-only target mount at ``/rest`` instead of defaulting
        to ``/api`` and 404ing. A descriptor path already carrying a known
        mount prefix is returned unchanged so it isn't double-prefixed.
        Behaviour parity with the typed
        :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`.
        """
        profile = self.profile
        # No profile / a stateless scheme has no session endpoint to mount
        # against — fall through to the identity mount (matching the base
        # HttpConnector hook). The profile-required error stays on the
        # dispatch surface (auth_headers), not this advisory remount.
        if profile is None or profile.auth.scheme in STATELESS_SCHEMES:
            return path
        spec = self._session_spec(profile.auth.scheme)
        fallback = spec.legacy_fallback
        if fallback is None:
            return path
        await self._session_token(target, operator)
        login_path = self._session_login_paths.get(
            target_cache_key(target), spec.login_path(profile.auth)
        )
        return _mounted_path(fallback, login_path, path)

    # -- session-token harness (session_login / oauth2_mint) ------------

    async def _session_token(self, target: Any, operator: Operator) -> str:
        """Return the cached session token for *target*, minting on first use / expiry.

        The hoisted harness, once: lock-serialised single-flight so two
        concurrent first-use callers don't both pay the login round-trip, a
        cache fast-path under the lock, and a fail-closed empty-``raw_jwt``
        gate *before* the cache lookup so a system-initiated caller cannot be
        served a token primed by an authenticated caller (the
        security-load-bearing invariant preserved from the typed
        connectors). ``session_login`` caches until invalidated by a
        downstream re-login (``ttl_seconds=None`` → never proactively
        expires); ``oauth2_mint`` re-mints once the monotonic clock passes
        the TTL.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={getattr(target, 'name', '?')!r} has no operator JWT "
                "(system-initiated calls cannot read per-target vendor credentials)"
            )
        cache_key = target_cache_key(target)
        async with self._session_lock:
            now = time.monotonic()
            cached = self._session_tokens.get(cache_key)
            if cached is not None and cached.is_fresh(now):
                return cached.token
            minted = await self._mint_session_token(target, operator)
            expires_at = None if minted.ttl_seconds is None else now + minted.ttl_seconds
            self._session_tokens[cache_key] = _CachedSessionToken(minted.token, expires_at)
            return minted.token

    async def _mint_session_token(self, target: Any, operator: Operator) -> SessionToken:
        """Run the scheme's login round-trip and return the minted token + TTL.

        Reads the secret bundle (operator-context, fail-closed) then POSTs
        the scheme's login round-trip on the pooled client (see
        :meth:`_post_login` for why this bypasses the
        ``auth_headers``-stamping ``_post_json`` seam). ``encoding`` picks
        the body slot: JSON for ``session_login``, form-encoded for
        ``oauth2_mint``; ``session_login_basic`` sends no body and carries
        its credentials as HTTP Basic on the login POST instead. A non-2xx
        surfaces as :exc:`httpx.HTTPStatusError`; a 2xx with no usable token
        surfaces as :exc:`ProfileAuthError` naming the target + scheme.
        """
        profile = self._require_profile(target)
        auth = profile.auth
        spec = self._session_spec(auth.scheme)
        secret = await self._load_credentials(target, operator)
        body = spec.build_body(auth, secret)
        path = spec.login_path(auth)
        payload, established_path = await self._post_login(target, spec, auth, path, body, secret)
        minted = spec.extract_token(payload)
        if minted is None:
            raise ProfileAuthError(
                f"{auth.scheme!r} session login for target "
                f"{getattr(target, 'name', '?')!r} returned no usable token"
            )
        # Record the login path that won so op-path mount + teardown follow
        # the live endpoint (modern /api vs legacy /rest for vCenter). #2031
        self._session_login_paths[target_cache_key(target)] = established_path
        _log.info(
            "profiled_session_established",
            target=getattr(target, "name", None),
            host=getattr(target, "host", None),
            scheme=auth.scheme,
            product=profile.product,
            login_path=established_path,
        )
        return minted

    async def _post_login(
        self,
        target: Any,
        spec: SessionSchemeSpec,
        auth: AuthSpec,
        path: str,
        body: Mapping[str, str],
        secret: Mapping[str, str],
    ) -> tuple[Any, str]:
        """Run the login round-trip; return ``(parsed JSON, winning path)``.

        Goes through the pooled :class:`httpx.AsyncClient` **directly** rather
        than the inherited ``_post_json`` seam, for two load-bearing reasons:

        * ``_post_json`` calls :meth:`auth_headers` to stamp the request — but
          a profiled connector's ``auth_headers`` is *what is establishing
          this session*, so routing the login POST through it would recurse
          (and deadlock on the non-reentrant session lock the caller already
          holds). The login carries its credentials in the request body
          (``session_login`` / ``oauth2_mint``) or as an HTTP Basic header
          built here (``session_login_basic``), never via ``auth_headers``,
          so it must skip ``auth_headers`` entirely.
        * The login round-trip is "one attempt, surface the failure cleanly"
          — it deliberately bypasses the idempotent-GET tenacity retry, the
          same posture
          :func:`~meho_backplane.connectors._shared.vcf_auth.vcf_session_login`
          takes for the typed connectors.

        The client is still the pooled per-target client, so it inherits the
        connector's TLS-trust / base-URL / timeout config. Credential
        carriage is picked from the spec's ``login_credentials``:
        ``"basic"`` sends HTTP Basic creds (from the profile's first two
        declared secret fields) and an empty body — vCenter's
        ``POST /api/session`` shape; ``"body"`` picks the body slot from the
        spec's ``encoding`` (``json`` → ``json=``, ``form`` → ``data=``).

        **Modern→legacy 404 fallback (#2031).** When the spec declares a
        :class:`~meho_backplane.connectors._shared.profile_auth.LegacyFallback`
        and the modern *path* responds **HTTP 404 only**, the login is retried
        once at the legacy path before raising. A 401 / 403 / 5xx on the modern
        path is an auth / server failure — not "this deployment lacks the
        modern endpoint" — and is **not** retried on legacy. The returned
        winning path is recorded by the caller so op-path mount + teardown
        follow the live endpoint. Behaviour parity with the typed
        :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`.
        A non-2xx (after the fallback) surfaces as :exc:`httpx.HTTPStatusError`.
        """
        resp = await self._login_attempt(target, spec, auth, path, body, secret)
        established_path = path
        if resp.status_code == 404 and spec.legacy_fallback is not None:
            legacy_path = spec.legacy_fallback.legacy_login_path
            resp = await self._login_attempt(target, spec, auth, legacy_path, body, secret)
            established_path = legacy_path
        resp.raise_for_status()
        return resp.json(), established_path

    async def _login_attempt(
        self,
        target: Any,
        spec: SessionSchemeSpec,
        auth: AuthSpec,
        path: str,
        body: Mapping[str, str],
        secret: Mapping[str, str],
    ) -> httpx.Response:
        """POST one login attempt at *path*; return the raw response.

        Picks credential carriage from the spec's ``login_credentials`` and,
        for a body-carried scheme, the body slot from ``encoding``. Returns
        the response **without** raising on a non-2xx so the caller can
        inspect the status for the 404 modern→legacy fallback decision.
        """
        client = await self._http_client(target)
        request_headers = dict(spec.request_headers)
        if spec.login_credentials == "basic":
            basic_auth = spec.build_login_auth(auth, secret)
            if basic_auth is None:
                # A basic-credentials scheme must yield a (user, pass) pair;
                # None here is a scheme-wiring error, not an operator fault.
                raise ProfileAuthError(
                    f"{auth.scheme!r} declares login_credentials='basic' but its "
                    f"build_login_auth produced no credential pair"
                )
            return await client.post(path, headers=request_headers, auth=basic_auth)
        if spec.encoding == "form":
            return await client.post(path, data=dict(body), headers=request_headers)
        return await client.post(path, json=dict(body), headers=request_headers)

    @staticmethod
    def _session_spec(scheme: str) -> SessionSchemeSpec:
        """Return the :class:`SessionSchemeSpec` for a session-stateful scheme.

        Raises :class:`ProfileAuthError` for a scheme with no spec — a
        non-session scheme should have been handled by the stateless branch,
        so reaching here is a wiring error.
        """
        spec = SESSION_SCHEME_SPECS.get(scheme)
        if spec is None:
            raise ProfileAuthError(
                f"no session spec registered for scheme {scheme!r}; "
                f"stateless schemes route through build_static_headers"
            )
        return spec

    async def _invalidate_session(self, target: Any) -> None:
        """Drop the cached session token for *target*.

        Called on a downstream session-expiry status so the next
        :meth:`_session_token` re-establishes from a clean state. Holds the
        lock so a concurrent re-establish doesn't race the invalidation.
        Mirrors the typed connectors' re-login-once recovery seam; the
        downstream-call retry loop that invokes it lands with the
        profile-driven dispatch path (the dispatcher owns the wire call).
        """
        async with self._session_lock:
            cache_key = target_cache_key(target)
            self._session_tokens.pop(cache_key, None)
            # Drop the recorded login path too so a re-establish re-discovers
            # the live endpoint from a clean state (#2031).
            self._session_login_paths.pop(cache_key, None)

    # -- fingerprint / probe (G0.28-T6 #1972) ---------------------------

    async def fingerprint(
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Fingerprint the upstream from the profile's declarative recipe.

        Reads :attr:`ExecutionProfile.fingerprint`: GETs its ``path``,
        reads the version string from the literal top-level ``version_key``,
        and renders it into ``(version, build)`` via the named
        :func:`~meho_backplane.connectors.profile.split_version` splitter
        (harbor's ``-`` split, vRLI's 5-part dot split). On transport or
        status failure, returns a non-reachable result whose
        ``extras["error"]`` carries the exception class + message — the same
        shape the hand-coded harbor / SDDC / NSX connectors established.

        ``operator`` is threaded through to the auth-bearing GET when the
        recipe is ``authenticated``; an unauthenticated fingerprint endpoint
        (vRLI's ``/api/v2/version``) does not need it. When the recipe is
        authenticated and no operator is supplied, the call falls through to
        :meth:`auth_headers` (which raises until T4 wires it) — the same
        operator-context requirement the hand-coded connectors carry.
        """
        spec = self._require_profile(target).fingerprint
        probed_at = datetime.now(UTC)
        product = self.product
        try:
            if spec.authenticated:
                if operator is None:
                    raise RuntimeError(
                        f"fingerprint recipe for {product!r} is authenticated but "
                        "no operator was supplied"
                    )
                payload = await self._get_json(target, spec.path, operator=operator)
            else:
                payload = await self._get_unauthenticated_json(target, spec.path)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor=product,
                product=product,
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {spec.path}",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        raw_version = payload.get(spec.version_key)
        version_str, build_str = split_version(
            spec.version_splitter,
            raw_version if isinstance(raw_version, str) else None,
        )
        return FingerprintResult(
            vendor=product,
            product=product,
            version=version_str,
            build=build_str,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {spec.path}",
        )

    async def probe(self, target: Any) -> ProbeResult:
        """Probe reachability from the profile's declarative recipe.

        When :attr:`ExecutionProfile.probe` is the ``'delegate'`` sentinel,
        the probe runs the fingerprint round-trip and reports ``ok`` =
        ``reachable`` (the SDDC Manager / NSX precedent). When it is a
        :class:`~meho_backplane.connectors.profile.ProbeSpec`, GETs its
        ``path`` and compares the literal top-level ``ok_field`` value
        against ``ok_value`` (harbor's ``GET /api/v2.0/health`` with
        ``status == 'healthy'``).

        A dedicated health probe is run unauthenticated — it is a
        reachability check, not a credentialled read; this matches harbor's
        health endpoint, which needs no auth. On transport / status failure
        the probe returns ``ok=False`` with the exception in ``reason``.
        """
        profile = self._require_profile(target)
        probed_at = datetime.now(UTC)
        if profile.probe == "delegate":
            fp = await self.fingerprint(target)
            reason = None if fp.reachable else str(fp.extras.get("error") or "unreachable")
            return ProbeResult(ok=fp.reachable, reason=reason, probed_at=probed_at)
        spec = profile.probe
        try:
            payload = await self._get_unauthenticated_json(target, spec.path)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                probed_at=probed_at,
            )
        actual = payload.get(spec.ok_field)
        if actual == spec.ok_value:
            return ProbeResult(ok=True, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=f"{spec.ok_field}={actual!r} (expected {spec.ok_value!r})",
            probed_at=probed_at,
        )

    async def _get_unauthenticated_json(self, target: Any, path: str) -> dict[str, Any]:
        """GET *path* with no auth headers, returning parsed JSON.

        The fingerprint/probe recipes may target an unauthenticated version
        / health endpoint (vRLI's ``/api/v2/version``, harbor's
        ``/api/v2.0/health``). The base :meth:`HttpConnector._get_json`
        always calls :meth:`auth_headers` (which raises on a profiled
        connector until T4 wires it), so this seam issues the request
        through the pooled, TLS-trust-aware client without an auth header.
        """
        client = await self._http_client(target)
        resp = await client.request("GET", path)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def execute(
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Raise — ingested ops dispatch through ``dispatch_ingested``, not here.

        Like every typed connector's ``execute``, this is a dead legacy
        shim: an ingested op runs off its ``EndpointDescriptor`` row via the
        dispatcher, not through a per-connector ``execute``. The raise keeps
        a stray direct call loud rather than silently degenerate; the
        :class:`OperationResult` annotation satisfies the
        :class:`~meho_backplane.connectors.base.Connector` ABC.
        """
        raise NotImplementedError(
            "ProfiledRestConnector.execute is a dead shim; ingested ops dispatch "
            "through dispatch_ingested off the EndpointDescriptor row, not here."
        )

    async def aclose(self) -> None:
        """Clear cached session tokens then tear down the httpx pool.

        No revoke is issued — the cached tokens are short-lived (TTL refresh
        for ``oauth2_mint``; idle-expiry for ``session_login``) and a
        per-target revoke round-trip during lifespan shutdown is more risk
        than benefit (the same posture every typed session connector takes).
        """
        async with self._session_lock:
            self._session_tokens.clear()
            self._session_login_paths.clear()
        await super().aclose()
