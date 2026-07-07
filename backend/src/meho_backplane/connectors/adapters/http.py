# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Abstract HTTP-API connector with shared transport plumbing.

Every HTTP-API connector (vSphere, NSX, Harbor, Hetzner Robot, etc.)
inherits :class:`HttpConnector` and overrides ``auth_headers()`` plus the
three ABC methods (``fingerprint``, ``probe``, ``execute``).

**Retry policy:** Three retries on idempotent verbs (GET, HEAD, OPTIONS)
with exponential backoff (0.5 s → 1 s → 2 s). The ``_request_json`` helper
carries the retry decorator; non-idempotent callers must bypass it.
``_retryable`` allows retries only on connection errors and 5xx responses —
4xx responses represent caller/auth errors that retrying would not fix.

**Client pooling:** Each :class:`HttpConnector` instance owns a dict of
``httpx.AsyncClient`` keyed by the tenant-unique
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``). The client is created lazily on first use and reused
across all operations against the same target. Keying on ``target.name``
alone would collide two same-named targets in different tenants — target
names are unique only per ``(tenant_id, name)`` — and since each pooled
client is host-bound via ``base_url``, the collision would route one
tenant's request to the other tenant's host and leak credentials across
the tenant boundary (evoila/meho#1682).

**Cert-bundle / TLS-trust support:** httpx honours ``SSL_CERT_FILE``
natively. The per-target TLS trust has three states, in precedence order:

1. **CA-pin** (``tls_ca_pin`` set, evoila/meho#1784) — the secure path.
   The client is built with a context from
   :func:`_build_ca_pinned_ssl_context`:
   :func:`ssl.create_default_context` plus
   :meth:`~ssl.SSLContext.load_verify_locations` ``(cadata=<pem>)``, which
   **keeps** ``CERT_REQUIRED`` + ``check_hostname`` ON, so chain and
   hostname are still enforced — now also trusting the pinned CA (the
   govc-``-thumbprint`` pattern). Takes precedence over
   ``verify_tls=False`` (the two are mutually exclusive at the API layer).
2. **Insecure opt-out** (``verify_tls=False``, evoila/meho#1774) — the
   audited last resort. The client is built with an explicit insecure
   :class:`ssl.SSLContext` (``check_hostname`` off, ``CERT_NONE``) so it
   can reach a self-signed / internal-CA appliance with no pin. Per-target,
   never global, and loud — see :func:`_insecure_ssl_context` and the WARN
   emitted at client construction.
3. **Default** (``verify_tls=True``, no pin) — the client is built with
   **no** ``verify=`` argument, so the global ``SSL_CERT_FILE`` /
   chart-trust-bundle path (evoila/meho#209) is in effect unchanged.

**Client pool key:** the pool is keyed on
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``) **plus** :meth:`HttpConnector.extra_cache_dimensions`
(``(verify_tls, ca_pin_digest)``), i.e.
``(tenant_id, id, verify_tls, ca_pin_digest)``. Appending the two
TLS-trust dimensions keeps the ``(tenant_id, id)`` tenant-isolation prefix
intact (evoila/meho#1682/#1642) while ensuring a PATCH that flips
``verify_tls`` **or** rotates ``tls_ca_pin`` is not served the stale
pooled client built under the previous trust material. The pin digest is
the empty string when unpinned, so an unpinned target's key is unchanged
in its pin slot from the #1781 shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector
from meho_backplane.targets.ssrf_guard import (
    TargetDestinationBlockedError,
    assert_public_destination_async,
)

logger = structlog.get_logger(__name__)


class SsrfBlockedError(httpx.ConnectError):
    """Dispatch refused: the target resolves to a non-public address.

    Connect-time arm of the target SSRF guard
    (:mod:`meho_backplane.targets.ssrf_guard`,
    evoila-bosnia/meho-internal#153): raised by
    :meth:`HttpConnector._http_client` *before* any pooled client is
    built or request issued, when the stored ``host`` is — or now
    resolves to — a private / loopback / link-local (``169.254.0.0/16``
    metadata) / reserved address that the operator-configured
    ``MEHO_TARGET_SSRF_ALLOWLIST`` does not exempt. Re-screening the
    **resolved** address here (not just the create-time literal) closes
    the DNS-rebind window between create and dispatch.

    Subclasses :class:`httpx.ConnectError` so the dispatcher's existing
    ``ConnectError`` arm flattens it into the structured
    ``connector_error`` shape carrying the guard's message — no new
    dispatcher plumbing. Explicitly excluded from :func:`_retryable`:
    the rejection is deterministic policy, not a transient network
    failure, so retrying would only re-run the DNS lookup.
    """


# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _build_insecure_ssl_context() -> ssl.SSLContext:
    """Return a one-shot insecure context (verification disabled).

    Reuses the in-tree idiom (``backend/tests/acceptance/_vcsim.py``):
    start from :func:`ssl.create_default_context`, then disable
    ``check_hostname`` **before** dropping ``verify_mode`` to
    :data:`ssl.CERT_NONE`. The order is load-bearing — assigning
    ``CERT_NONE`` while ``check_hostname`` is still enabled raises
    ``ValueError`` on Python 3.12 ("Cannot set verify_mode to CERT_NONE
    when check_hostname is enabled.").
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Built once and shared across every insecure dispatch in the process.
# An ``SSLContext`` is safe to share between clients; it is never mutated
# after construction, so a single cached instance avoids rebuilding the
# context (and re-reading the default trust store it starts from) on each
# verify_tls=False target.
_INSECURE_SSL_CONTEXT = _build_insecure_ssl_context()


def _insecure_ssl_context() -> ssl.SSLContext:
    """Return the process-wide cached insecure :class:`ssl.SSLContext`."""
    return _INSECURE_SSL_CONTEXT


def _build_ca_pinned_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """Return a context that trusts *ca_pem* while keeping verification ON.

    The **secure** supersession of the insecure context (evoila/meho#1784):
    start from :func:`ssl.create_default_context` -- which leaves
    ``check_hostname=True`` and ``verify_mode=CERT_REQUIRED`` -- and
    :meth:`~ssl.SSLContext.load_verify_locations` the per-target CA/cert
    PEM into its trust store via the ``cadata`` parameter. Crucially,
    ``load_verify_locations`` does **not** touch ``check_hostname`` or
    ``verify_mode`` (verified against the installed CPython 3.12 ``ssl``),
    so the returned context still enforces chain **and** hostname -- now
    additionally trusting the pinned CA. This is the govc-``-thumbprint``
    pattern: trust *this specific* appliance's self-signed / internal-CA
    cert without weakening verification (contrast ``verify_tls=false``,
    which drops both).

    The pin is added *on top of* the default system trust store (the
    context starts from ``create_default_context``), so a pinned target
    still trusts public CAs too -- the pin is additive, not a replacement.

    The PEM is validated at the API boundary
    (:func:`meho_backplane.targets.schemas.validate_ca_pin_pem`) before it
    is ever persisted, so by the time it reaches here it loads cleanly; a
    malformed pin would have been a 422 at create/update time, never an
    opaque dispatch failure.
    """
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=ca_pem)
    return ctx


def _ca_pin_digest(ca_pem: str | None) -> str:
    """Return a stable pool-key token identifying *ca_pem* (or ``""``).

    A short SHA-256 hex digest of the PEM, or the empty string when there
    is no pin. Seeds the client-pool cache key
    (:meth:`HttpConnector.extra_cache_dimensions`) so a target whose pinned
    CA *changes* (rotation) is not served the stale client built against
    the previous pin -- the same staleness guarantee ``verify_tls`` already
    gets, extended to the pin material. A digest (not the raw PEM) keeps
    the key compact and avoids carrying certificate bytes around as dict
    keys. It deliberately matches the digest the audit trail records
    (:func:`meho_backplane.api.v1.targets._ca_pin_digest`) so "a different
    pin" means the same thing to the pool and the audit log.
    """
    if not ca_pem:
        return ""
    return hashlib.sha256(ca_pem.encode("utf-8")).hexdigest()[:16]


def _retryable(exc: BaseException) -> bool:
    """Retry on connection errors and 5xx; never on 4xx."""
    if isinstance(exc, SsrfBlockedError):
        # Deterministic SSRF-guard rejection, not a transient network
        # failure — retrying cannot change the verdict.
        return False
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


# Hard cap on the same-origin redirect chain followed by
# :class:`_SameOriginRedirectClient`. Vendor canonicalisation never chains
# more than one trailing-slash hop in practice; the cap is a cheap guard
# against a same-origin redirect loop (a reverse proxy that bounces
# ``/a`` -> ``/a/`` -> ``/a`` would otherwise spin until the read timeout).
# httpx's own default ``max_redirects`` is 20; this is stricter because the
# only redirects we ever follow are benign single-hop canonicalisations.
_MAX_SAME_ORIGIN_REDIRECTS = 5


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _effective_port(url: httpx.URL) -> int | None:
    """Return *url*'s port, substituting the scheme default when implicit.

    :attr:`httpx.URL.port` is ``None`` when the URL omits the port, so a
    bare ``https://h/`` and an explicit ``https://h:443/`` would otherwise
    compare unequal. Both name the same origin, so collapse them onto the
    scheme's default port before comparison.
    """
    return url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    """Return ``True`` when *a* and *b* share scheme, host, and port.

    An origin is the ``(scheme, host, port)`` triple (RFC 6454). Default
    ports are normalised via :func:`_effective_port` so a vendor redirect
    that names the port explicitly (``https://h:443/api/session/``) is
    still recognised as same-origin against the implicit-port base URL
    ``HttpConnector._base_url`` builds (``https://h``) — otherwise the
    benign trailing-slash canonicalisation this client exists to preserve
    would be wrongly refused as cross-origin.
    """
    return (a.scheme, a.host, _effective_port(a)) == (
        b.scheme,
        b.host,
        _effective_port(b),
    )


class _SameOriginRedirectClient(httpx.AsyncClient):
    """An ``AsyncClient`` that follows redirects **only within one origin**.

    The shared pooled client must not transparently follow ``httpx``'s
    default redirect chain across origins: a malicious or misconfigured
    vendor appliance can answer a session-create / login ``POST`` with a
    ``3xx`` whose ``Location`` points at an attacker host, and ``httpx``
    will replay the request there. ``httpx`` strips the ``Authorization``
    header on a cross-origin hop, but it does **not** strip vendor-specific
    auth headers (NSX's ``X-XSRF-TOKEN``) nor — on a method-preserving
    ``307``/``308`` — the credential request body, so the login secret and
    session token leak to the redirect target (open-redirect SSRF /
    auth-token forwarding, evoila/meho-internal#101 row L11).

    This client is built with ``follow_redirects=False`` and re-implements
    a *bounded, same-origin-only* redirect loop in :meth:`send`. A legitimate
    same-origin canonicalisation (the vendor trailing-slash ``301`` that
    vcsim's ``/rest`` mount and some appliances emit) is still followed, so
    no vendor flow regresses. Any cross-origin ``Location`` terminates the
    loop and the ``3xx`` response is returned to the caller unfollowed — the
    request is **never** replayed off-origin, so no header or body crosses
    the origin boundary.
    """

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        # Always issue the underlying request without httpx's own redirect
        # following; this class owns the redirect decision so the
        # same-origin invariant cannot be undone by a per-call
        # ``follow_redirects=True`` slipping through a convenience method.
        response = await super().send(request, stream=stream, auth=auth, follow_redirects=False)
        for _ in range(_MAX_SAME_ORIGIN_REDIRECTS):
            if not response.has_redirect_location:
                return response
            # ``next_request`` is httpx's own correctly-built follow-up
            # request (method downgrade on 301/302, body/headers carried on
            # 307/308) — reuse it rather than reconstructing the hop.
            next_request = response.next_request
            if next_request is None or not _same_origin(request.url, next_request.url):
                # Cross-origin (or unresolvable) redirect: refuse to
                # follow. Returning the 3xx unfollowed means no header or
                # body — and so no ``X-XSRF-TOKEN`` / credential — is ever
                # replayed to the off-origin ``Location``.
                logger.warning(
                    "connector_redirect_not_followed_cross_origin",
                    method=request.method,
                    from_url=str(request.url),
                    location=response.headers.get("Location"),
                )
                return response
            await response.aclose()
            request = next_request
            response = await super().send(request, stream=stream, auth=auth, follow_redirects=False)
        # Exhausted the same-origin hop budget — surface httpx's own error
        # so the caller sees a redirect loop rather than a silent 3xx. Close
        # the final 3xx first so its connection is released back to the pool
        # rather than held checked-out until GC (a looping target would
        # otherwise slowly starve the pooled client).
        await response.aclose()
        raise httpx.TooManyRedirects(
            f"exceeded the same-origin redirect budget ({_MAX_SAME_ORIGIN_REDIRECTS})",
            request=request,
        )


class HttpConnector(Connector):
    """Abstract HTTP-API connector with retry/timeout/cert-bundle support.

    Subclasses MUST override ``auth_headers()`` and the three ABC methods
    (``fingerprint``, ``probe``, ``execute``).
    """

    def __init__(self) -> None:
        # Keyed on the tenant-unique ``(tenant_id, id)`` tuple
        # (``target_cache_key``) plus ``extra_cache_dimensions`` (the
        # ``(verify_tls, ca_pin_digest)`` TLS-trust dimensions), not
        # ``target.name``: each pooled client is host-bound via
        # ``base_url``, and two tenants may legitimately own same-named
        # targets pointing at different hosts. Name-keying would route the
        # second tenant's request to the first tenant's host and leak
        # credentials across the boundary (evoila/meho#1682). The TLS-trust
        # suffix keeps a PATCH that flips ``verify_tls`` or rotates the
        # ``tls_ca_pin`` from being served the stale client built under the
        # old trust material.
        self._clients: dict[tuple[str, ...], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def extra_cache_dimensions(self, target: Target) -> tuple[object, ...]:
        """Return extra pool-key dimensions appended to ``target_cache_key``.

        The base appends two TLS-trust dimensions so a target whose TLS
        trust *changes* (via PATCH) gets a freshly built client rather than
        the stale one cached under the previous trust material — a client's
        ``verify`` context is fixed at construction:

        1. the resolved ``verify_tls`` flag (evoila/meho#1781), and
        2. a digest of the per-target ``tls_ca_pin`` CA material
           (evoila/meho#1784) — empty string when unpinned, so an
           unpinned target's key is unchanged from the #1781 shape in its
           pin slot.

        The append preserves the ``(tenant_id, id)`` prefix, so the
        cross-tenant isolation guarantee (evoila/meho#1682/#1642) is
        unaffected. Subclasses that reach into :attr:`_clients` directly
        MUST build their lookup key the same way
        (``target_cache_key(target) + self.extra_cache_dimensions(target)``)
        so they index the same entry the base created.
        """
        return (
            bool(getattr(target, "verify_tls", True)),
            _ca_pin_digest(getattr(target, "tls_ca_pin", None)),
        )

    def _client_cache_key(self, target: Target) -> tuple[str, ...]:
        """Return the full pooled-client key for *target*.

        ``target_cache_key`` (``(tenant_id, id)``) plus
        :meth:`extra_cache_dimensions` (``(verify_tls, ca_pin_digest)``).
        Stringified into a flat ``tuple[str, ...]`` so the key is hashable
        and a subclass that derives it the same way produces an identical
        tuple.
        """
        return target_cache_key(target) + tuple(
            str(dim) for dim in self.extra_cache_dimensions(target)
        )

    async def _http_client(self, target: Target) -> httpx.AsyncClient:
        """Return the per-target pooled client, creating it on first use.

        TLS-trust precedence, highest first:

        1. **CA-pin** (``tls_ca_pin`` set, evoila/meho#1784) — the secure
           path. Build a context that trusts the pinned CA while keeping
           ``CERT_REQUIRED`` + ``check_hostname`` ON. Takes precedence over
           ``verify_tls=false`` (and the API rejects the two together), so
           a pin can never be silently undone by a stray insecure flag.
        2. **Insecure opt-out** (``verify_tls=false``, evoila/meho#1781) —
           the audited last resort: an explicit insecure context (no chain,
           no hostname).
        3. **Default** (``verify_tls=true``, no pin) — pass **no**
           ``verify=`` argument so httpx keeps its default ``verify=True``
           and the global ``SSL_CERT_FILE`` / chart-trust-bundle path
           (evoila/meho#209) stays byte-identical to a connector with no
           TLS config at all.
        """
        # SSRF guard (evoila-bosnia/meho-internal#153): re-screen the
        # *resolved* destination on every acquisition, before the pooled
        # client is built or served. The create/update schema validators
        # already rejected a non-public literal, but the stored value can
        # be a hostname whose DNS answer changed since create (rebind) or
        # was unresolvable then — this is the enforcement point. The
        # guard screens the httpx-normalized *dialed* host, never the
        # raw string, so a stored value that embeds URL structure —
        # including rows that predate the guard — is screened on the
        # host the transport actually reaches. Runs before the cache
        # lookup so a previously-pooled client for a now-rebinding
        # hostname is refused too.
        try:
            await assert_public_destination_async(str(target.host))
        except TargetDestinationBlockedError as exc:
            raise SsrfBlockedError(str(exc)) from exc
        cache_key = self._client_cache_key(target)
        verify_tls = bool(getattr(target, "verify_tls", True))
        ca_pin = getattr(target, "tls_ca_pin", None)
        async with self._lock:
            if cache_key not in self._clients:
                # Pass ``verify=`` only when the target pins a CA (secure)
                # or opts out of verification (insecure). With neither we
                # omit the kwarg so the client defaults to httpx's
                # ``verify=True``, keeping the global ``SSL_CERT_FILE`` path
                # (evoila/meho#209) byte-identical to a connector with no
                # TLS config at all.
                verify_kwargs: dict[str, Any] = {}
                if ca_pin:
                    # Secure supersession (evoila/meho#1784): trust the
                    # pinned CA while keeping CERT_REQUIRED + hostname
                    # verification on. Built per pool entry (not module-
                    # cached like the insecure context) because the PEM is
                    # per-target; the pool key carries the pin digest so the
                    # context is rebuilt only when the pin actually changes,
                    # not per request. Logged at INFO (not WARN): unlike the
                    # insecure opt-out, a CA-pin keeps the channel verified,
                    # so it is the recommended state, not a footgun.
                    logger.info(
                        "connector_tls_ca_pinned",
                        target=getattr(target, "name", None),
                        host=getattr(target, "host", None),
                        ca_pin_digest=_ca_pin_digest(ca_pin),
                    )
                    verify_kwargs["verify"] = _build_ca_pinned_ssl_context(ca_pin)
                elif not verify_tls:
                    # Per-target, audited last resort (evoila/meho#1774):
                    # the dispatch forwards a Vault-resolved credential over
                    # an unverified channel, so make it loud and queryable.
                    # The audit row is written on target create/update
                    # (T1 #1780); this WARN marks the actual insecure
                    # dispatch construction.
                    logger.warning(
                        "connector_tls_verification_disabled",
                        target=getattr(target, "name", None),
                        host=getattr(target, "host", None),
                    )
                    verify_kwargs["verify"] = _insecure_ssl_context()
                self._clients[cache_key] = _SameOriginRedirectClient(
                    base_url=self._base_url(target),
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                    # Same-origin-only redirect following (see
                    # ``_SameOriginRedirectClient``). Vendor REST surfaces
                    # routinely canonicalise to a trailing slash with a
                    # ``301`` (the govmomi/vcsim legacy ``/rest`` mount does
                    # this; some real appliances do too behind a normalising
                    # reverse proxy) — those benign single-origin hops are
                    # still followed, so no vendor flow regresses. A
                    # *cross-origin* ``3xx`` is refused: ``httpx``'s default
                    # ``follow_redirects=True`` would replay the request off
                    # origin and forward NSX's ``X-XSRF-TOKEN`` and the
                    # login body to an attacker-controlled ``Location``
                    # (open-redirect SSRF, evoila/meho-internal#101 row L11).
                    **verify_kwargs,
                )
            return self._clients[cache_key]

    async def mount_op_path(self, target: Target, path: str, operator: Operator) -> str:
        """Map an ingested-descriptor *path* onto the wire path for *target*.

        Dispatcher hook for ``source_kind='ingested'`` ops: the G0.7
        pipeline stores spec-relative descriptor paths, and some vendor
        APIs expose those under a mount prefix the spec omits (vCenter
        REST: ``/api`` on modern, ``/rest`` on legacy/vcsim). The
        default is identity — most ingested APIs are reachable at the
        descriptor path verbatim. Vendor connectors that need a mount
        (see ``VmwareRestConnector``) override this; the override is a
        separate seam from ``_request_json`` / ``_post_json`` so their
        tenacity ``@retry`` wrapper stays intact.

        ``operator`` is threaded so an override that has to establish a
        session to learn the live mount (vCenter's modern→legacy
        fallback) authenticates under the same operator identity the
        transport call will use, rather than a credential-less stand-in.
        """
        del target, operator
        return path

    def _base_url(self, target: Target) -> str:
        scheme = "https"
        port = f":{target.port}" if target.port and target.port != 443 else ""
        return f"{scheme}://{target.host}{port}"

    def _request_extensions(self, target: Target) -> dict[str, Any]:
        """Return per-request httpx ``extensions`` for *target* (or ``{}``).

        Decouples the TLS SNI / certificate-verification name from the
        URL-derived ``host`` (evoila/meho#2002). httpx derives the TCP
        connect address, the wire ``Host:`` header **and** the TLS SNI +
        cert-CN/SAN verification name all from the client's ``base_url``
        host (httpcore: ``server_hostname =
        request.extensions["sni_hostname"] or origin.host``). When a
        target sets ``tls_server_name`` we keep ``base_url=https://<host>``
        (so connect + ``Host:`` stay the operator-routed value -- the IP a
        cert-CN-pinning appliance accepts) and pass
        ``extensions={"sni_hostname": <name>}`` so the handshake offers the
        override as SNI and verifies the presented cert's CN/SAN against
        it. This lets an operator keep ``verify_tls=true`` (and optionally
        ``tls_ca_pin``) against an appliance that pins its cert to an FQDN
        but demands ``Host: <IP>``, instead of dropping to the insecure
        ``verify_tls=false`` to dodge the hostname mismatch.

        Returns an empty dict when the target sets no override, so the
        request is dispatched byte-identically to today (the SNI / verify
        name derives from ``base_url`` as before). Threading it at this
        shared :class:`HttpConnector` layer covers both request helpers
        and the profiled-dispatch path automatically.
        """
        server_name = getattr(target, "tls_server_name", None)
        if server_name:
            return {"sni_hostname": server_name}
        return {}

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Return auth headers for the request.

        Vendor connectors MUST override per ``target.auth_model``. The
        full :class:`~meho_backplane.auth.operator.Operator` is threaded
        here (not just ``operator.raw_jwt``) so a connector's credential
        loader can perform an operator-context Vault read via
        ``vault_client_for_operator(operator)`` — the locked decision in
        [docs/architecture/connector-auth.md](docs/architecture/connector-auth.md).
        ``Operator`` is frozen, so passing it down carries no
        confused-deputy risk.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override auth_headers() — "
            f"target {target.name!r} uses {target.auth_model}"
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    async def _request_json(
        self,
        target: Target,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Retryable JSON request. Only idempotent verbs (GET, HEAD, OPTIONS).

        Raises :exc:`ValueError` for non-idempotent verbs so a caller that
        accidentally passes ``POST``/``PATCH`` never gets silent retry of a
        side-effecting operation. Non-idempotent callers must use
        ``_post_json`` or call the httpx client directly. ``operator`` is
        forwarded to :meth:`auth_headers` so the connector can resolve
        credentials under the operator's identity. ``extra_headers`` are
        merged onto the connector-supplied :meth:`auth_headers` (e.g.
        header-located op params; per-call values win on a key clash).
        """
        method = method.upper()
        if method not in _IDEMPOTENT_METHODS:
            raise ValueError(
                f"_request_json only accepts idempotent methods "
                f"{sorted(_IDEMPOTENT_METHODS)}; got {method!r}"
            )
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        if extra_headers:
            headers = {**headers, **extra_headers}
        resp = await client.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            extensions=self._request_extensions(target),
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _get_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON."""
        return await self._request_json(target, "GET", path, operator=operator, params=params)

    async def _post_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Non-retried request for a non-idempotent verb, returning parsed JSON.

        Despite the name, this seam honours the *actual* non-idempotent
        verb passed in ``verb`` (``POST`` / ``PUT`` / ``PATCH`` /
        ``DELETE``) rather than hardcoding ``POST`` -- an ingested
        ``PUT``/``PATCH``/``DELETE`` op must reach the wire with its
        declared method, not be silently downgraded to a ``POST``. The
        verb is validated against :data:`_IDEMPOTENT_METHODS` so a caller
        that accidentally routes an idempotent verb here (which would skip
        the tenacity retry on :meth:`_request_json`) fails loudly.

        Two body shapes are supported and are mutually exclusive: ``json``
        serialises a JSON request body (``application/json``); ``data``
        serialises a form-encoded body (``application/x-www-form-urlencoded``
        -- the shape OAuth2 token grants and session-login POSTs require).

        ``extra_headers`` are merged onto the connector-supplied
        :meth:`auth_headers` (header-located op params; the per-call values
        win on a key clash). Retry on non-idempotent verbs is the caller's
        responsibility -- this seam deliberately stays outside the
        :meth:`_request_json` tenacity wrapper.
        """
        verb = verb.upper()
        if verb in _IDEMPOTENT_METHODS:
            raise ValueError(
                f"_post_json only accepts non-idempotent methods; got {verb!r} "
                f"(idempotent verbs {sorted(_IDEMPOTENT_METHODS)} must use _request_json)"
            )
        if json is not None and data is not None:
            raise ValueError("_post_json accepts json= or data=, not both")
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        if extra_headers:
            headers = {**headers, **extra_headers}
        resp = await client.request(
            verb,
            path,
            json=json,
            data=data,
            headers=headers,
            extensions=self._request_extensions(target),
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        """Close all pooled clients. Called by lifespan or per-target cleanup."""
        async with self._lock:
            for client in self._clients.values():
                await client.aclose()
            self._clients.clear()
