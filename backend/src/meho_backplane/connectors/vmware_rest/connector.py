# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing >600-line connector (session
# lifecycle + per-op mount hooks + 6 typed-op delegators accreted across
# #2257/#2258/#2300/#2329/#2396/#2398, the VI-JSON vmomi transport seam
# #2466, and the vCenter-capable fingerprint fallback chain #2765).
# Splitting the module by responsibility is separate refactor work, out
# of scope for a probe-chain bug fix.

"""VmwareRestConnector — hand-rolled HttpConnector subclass for vSphere REST.

Replaces the future :class:`GenericRestConnector` auto-shim that G0.7's
ingestion pipeline synthesises on first ingest of ``vcenter.yaml``. The
auto-shim makes the connector resolvable so ingestion can land the
``endpoint_descriptor`` rows; this class makes those ops dispatchable.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.vmware_rest.__init__`. The auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`
once #408's pipeline lands in main) then no-ops on subsequent ingests
against the same ``(product="vmware", version="9.0",
impl_id="vmware-rest")`` triple.

Per-target sessions
-------------------

The class caches one ``vmware-api-session-id`` token per ``target.name``.
First call to :meth:`auth_headers` against a given target invokes the
:class:`VsphereSessionLoader` (default
:func:`load_session_credentials_from_vault`) for the service-account
credentials, then issues ``POST /api/session`` with HTTP basic auth. If
the modern endpoint responds with HTTP 404, the connector retries
against the legacy ``POST /rest/com/vmware/cis/session`` path before
declaring failure — real vCenter serves both, but the upstream
``vmware/vcsim`` simulator (used by the integration test in T8) wires
the handler under the legacy path only. The successful endpoint is
cached per-target so :meth:`aclose` revokes against the same path. The
JSON-string-body response (or legacy ``{"value": "<token>"}`` shape) is
the session token; subsequent calls reuse the cached value. Per-target
isolation is the load-bearing invariant: two targets must never share a
session token even if their names collide across tenants — the cache is
keyed on the tenant-unique ``(tenant_id, target.id)`` tuple via the
shared :func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
helper (#1642/#1672), so two same-named targets in different tenants
never collapse onto one cached session.

The session-establish flow runs under an :class:`asyncio.Lock` so two
concurrent first-use callers against the same target don't both POST to
``/api/session``. The lock is held only across the cache check + token
fetch + cache write; subsequent reads after the cache is populated take
the fast path under the same lock and exit immediately.

Session lifecycle
-----------------

vSphere's default idle timeout is ~5 minutes; the connector does not
proactively refresh tokens. The dispatcher's tenacity decorator on
:meth:`HttpConnector._request_json` retries connection errors and 5xx
responses but not 401 — a 401 from a subsequent call would surface to
the caller. Explicit 401-driven session refresh is intentionally
deferred to v0.2.next (per the task body's *Out of scope* section);
operator-facing dispatch sees re-authentication as a clean retry
through the dispatcher's caller-side retry path rather than a hidden
retry inside the connector.

:meth:`aclose` revokes every cached session via ``DELETE`` against the
endpoint that minted the token (modern ``/api/session`` for production,
legacy ``/rest/com/vmware/cis/session`` for vcsim-served targets) before
closing the per-target httpx clients. A revoke failure is logged and
proceeds — the operator-facing concern at shutdown is "tear down the
httpx pool"; an in-flight 5xx during DELETE doesn't block that.

Auth model gating
-----------------

The task body's *Session lifecycle* section locks v0.2 to
:attr:`AuthModel.SHARED_SERVICE_ACCOUNT`. :meth:`auth_headers` rejects
any other ``target.auth_model`` value with a clear :exc:`NotImplementedError`
that names both the target and the requested mode. ``None`` is accepted
because targets that predate G0.3's ``auth_model`` column legitimately
have no value — the column defaults to the shared-service-account model
once G0.3 ships, but until then ``None`` is the "no model declared,
fall back to v0.2 default" sentinel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import SESSION_TOKEN_OBJECT_KEY
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    session_establish_auth_error,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vmware_rest._mount import (
    API_MOUNT_LEGACY,
    SESSION_PATH_LEGACY,
    SESSION_PATH_MODERN,
    adapt_filter_params,
    api_mount_for_session_path,
    mounted_path,
    vmomi_mounted_path,
    vmomi_release_from_version,
)
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.session import (
    VsphereSessionLoader,
    VsphereTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.vmware_rest.soap import (
    SOAP_CONTENT_TYPE,
    SoapFault,
    build_create_nas_datastore_envelope,
    build_login_envelope,
    build_logout_envelope,
    build_mark_ssd_envelope,
    build_query_boot_devices_envelope,
    build_retrieve_properties_ex_envelope,
    build_service_content_envelope,
    parse_boot_devices,
    parse_moref_result,
    parse_retrieve_result,
    parse_service_content,
    parse_soap_fault,
    soap_action_for_version,
)
from meho_backplane.flight_recorder import capture as flight_recorder_capture

__all__ = ["VmwareRestConnector", "product_from_line_id", "service_versions_api_version"]

_log = structlog.get_logger(__name__)

# vmware-api-session-id header name per Broadcom's vSphere Automation
# API security schema (Basic / API-key / Bearer). The same header
# carries the session token across both vCenter REST (vcenter.yaml-
# sourced ops) and vi-json (vi-json.yaml-sourced ops once #503 lands),
# per docs/vcenter-9.0/MANIFEST.md. Lifted to a module constant so the
# revoke path in aclose() and the auth path in _session_token can't
# drift apart.
_SESSION_HEADER = "vmware-api-session-id"

# vSphere 8.0+'s /api/session POST returns the session token as a JSON
# string body (e.g. ``"abc123def456"``). Older 6.7/7.0 vCenter via the
# deprecated /rest/com/vmware/cis/session path returned
# ``{"value": "abc123def456"}``. The class's supported_version_range
# is ``">=8.5,<10.0"`` so the JSON-string shape is the load-bearing
# one, but :meth:`_extract_session_token` handles both defensively —
# vcsim has been known to swap between shapes between minor releases,
# and a defensive read here costs nothing. The object-shape key is the
# shared :data:`SESSION_TOKEN_OBJECT_KEY` so the typed and profiled
# (``session_login_basic``) extractors can't drift apart (#2047).
_SESSION_TOKEN_OBJECT_KEY = SESSION_TOKEN_OBJECT_KEY

# Session endpoints + the spec-relative-op → /api-or-/rest mount
# mapping live in ``._mount`` (extracted to keep this module within
# the code-quality size budget; see that module's docstring for the
# full modern-vs-legacy + vcsim rationale). ``SESSION_PATH_MODERN`` /
# ``SESSION_PATH_LEGACY`` drive session establishment + the
# ``aclose()`` revoke; ``mounted_path`` maps an ingested descriptor
# path onto the mount the target's established session selected.

# Verbs that go through HttpConnector._request_json's tenacity retry
# decorator. Non-idempotent verbs (POST / PUT / PATCH / DELETE) route
# through _post_json instead — see HttpConnector for the policy.
# Lifted here so :meth:`auth_headers`-level callers can introspect.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Fingerprint probe chain (#2765). ``GET /api/about`` is the ESXi host
# REST surface — vCenter's Automation API spec has no such path (its
# only "about" is ``/api/vcenter/phm/about``) and answers HTTP 404 —
# so on a 404 the probe falls back to the unauthenticated version-
# discovery document at ``/sdk/vimServiceVersions.xml``, served by
# both vCenter and ESXi since the SOAP era (it is how pyVim/govmomi
# negotiate API versions), then best-effort enriches the result from
# the authenticated appliance-version read only vCenter (VCSA) serves.
_ABOUT_PATH = "/api/about"
_ABOUT_PROBE = "GET /api/about"
_SERVICE_VERSIONS_PATH = "/sdk/vimServiceVersions.xml"
_SERVICE_VERSIONS_PROBE = "GET /sdk/vimServiceVersions.xml"
_APPLIANCE_VERSION_PATH = "/api/appliance/system/version"
_APPLIANCE_VERSION_PROBE = "GET /api/appliance/system/version"

#: The ``vimServiceVersions.xml`` namespace entry whose ``<version>``
#: carries the current vim25 API version (four-part, e.g. ``8.0.3.0``
#: — the same value the VI-JSON ``/sdk/vim25/{release}`` base uses).
_VIM25_NAMESPACE = "urn:vim25"

# ESXi-native SOAP session (#3363). A standalone ESXi host — a host no
# vCenter manages yet, the pre-vCenter case #3332 exists to support —
# does **not** serve the VI-JSON surface (``/sdk/vim25/{release}/…`` is
# vCenter-only; every VI-JSON POST there 500s with a SOAP expat fault
# because ``/sdk`` XML-parses the body), and its ``/api/*`` is a JSON-RPC
# 2.0 handler, not the vSphere-Automation vAPI: a bodyless
# ``POST /api/session`` there answers HTTP 400 (``{"jsonrpc":"2.0", …}`` /
# "Unsupported content type"), never a token, so the modern→legacy 404
# fallback dead-ends. ESXi serves vmomi only as **hand-rolled SOAP 1.1
# over ``POST /sdk``** (proven live: ``RetrieveServiceContent`` → 200
# ``apiType=HostAgent``; ``SessionManager.Login`` → 200 sets a
# ``vmware_soap_session`` cookie; ``RetrievePropertiesEx`` → 200). The
# builders/parsers live in :mod:`.soap`; this connector wires them onto the
# pooled ``httpx`` client (inheriting its TLS-pin posture for free) and
# routes the existing ``_post_vmomi_json`` callers through
# :meth:`_post_soap`, deserialising the response envelopes back into the
# exact VI-JSON dict shapes the unchanged downstream consumers already read.
#: The single vmomi SOAP endpoint on a HostAgent (and on vCenter).
_SDK_PATH = "/sdk"
#: SessionManager.Login's locale arg (govmomi defaults it to ``en_US``).
_ESXI_LOGIN_LOCALE = "en_US"
#: The auth cookie ``SessionManager.Login`` sets; carried in the pooled
#: client's cookie jar (never a request header), so :meth:`auth_headers`
#: adds nothing for an ESXi-flavored session and the cached sentinel token
#: is this opaque cookie value — never the password.
_ESXI_SOAP_SESSION_COOKIE = "vmware_soap_session"
#: The vCenter ``propertyCollector`` moid literal every VI-JSON
#: ``RetrievePropertiesEx`` caller bakes into its path. On a standalone
#: HostAgent the property-collector moid is **not** this literal (it comes
#: from ServiceContent, e.g. ``ha-property-collector``), so :meth:`_post_soap`
#: substitutes the ServiceContent-provided moid only when the caller's moid
#: equals this literal (a guarded substitution — any other PC moid is left
#: untouched).
_VCENTER_PROPERTY_COLLECTOR_MOID = "propertyCollector"
#: vim ``detail`` fault localNames that mean "the credential was rejected"
#: — mapped to :class:`ConnectorAuthError` (restage remediation + the
#: dispatcher's cold-re-login recovery), the SOAP analogue of a vCenter
#: 401/403 at ``POST /api/session``.
_SOAP_AUTH_FAULT_TYPES = frozenset({"InvalidLogin", "NoPermission", "NotAuthenticated"})
#: ``probe_method`` stamped on a standalone-ESXi fingerprint.
_ESXI_SOAP_PROBE = "GET /api/about (400) -> soap-retrieveservicecontent"

# JSON-RPC 2.0 discriminator ESXi's ``/api`` handler stamps on its error
# bodies. Recognising it (or the "Unsupported content type" message text)
# on the modern session path's HTTP-400 response is how the connector picks
# the ESXi SOAP branch on the *very first probe*, before any fingerprint
# exists to classify the target. Status 400 is itself diagnostic (vCenter
# answers 401 / a token there, never 400); the body check is confirmation.
_JSONRPC_KEY = "jsonrpc"
_JSONRPC_VERSION = "2.0"
_ESXI_UNSUPPORTED_CONTENT_TYPE = "Unsupported content type"


def _xml_local_name(tag: str) -> str:
    """Return *tag* without any ``{uri}`` XML-namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def service_versions_api_version(document: str) -> str | None:
    """Extract the current vim25 API version from a ``vimServiceVersions.xml`` body.

    The version-discovery document has the shape::

        <namespaces version="1.0">
          <namespace>
            <name>urn:vim25</name>
            <version>8.0.3.0</version>
            <priorVersions><version>8.0.2.0</version>...</priorVersions>
          </namespace>
        </namespaces>

    Older products serve a variant whose tags carry the
    ``http://www.vmware.com/vi/versions`` XML namespace (the second
    format pyVim's ``__VersionIsSupported`` handles); matching on the
    tags' local names covers both. Only *direct* children of a
    ``namespace`` element are read, so ``priorVersions`` entries never
    shadow the current version.

    Returns the ``urn:vim25`` entry's version, falling back to the
    first namespace entry carrying one; ``None`` when the document is
    malformed or yields no version — the caller treats that as "no
    version discovered", never an exception. ``DefusedXmlException``
    (``EntitiesForbidden`` on an entity-bearing document; a
    :exc:`ValueError`, not a :exc:`ParseError`) is caught alongside
    ``ParseError`` — defusedxml *rejecting* a hostile document is the
    same "no version discovered" outcome as failing to parse one.
    """
    try:
        root = fromstring(document)
    except (ParseError, DefusedXmlException):
        return None
    first_version: str | None = None
    for element in root.iter():
        if _xml_local_name(element.tag) != "namespace":
            continue
        name: str | None = None
        version: str | None = None
        for child in element:
            local = _xml_local_name(child.tag)
            if local == "name":
                name = (child.text or "").strip()
            elif local == "version":
                version = (child.text or "").strip() or None
        if version is None:
            continue
        if name == _VIM25_NAMESPACE:
            return version
        if first_version is None:
            first_version = version
    return first_version


def product_from_line_id(line_id: str) -> str:
    """Map vCenter's ``product_line_id`` to the canonical product slug.

    ``GET /api/about`` returns a ``product_line_id`` like ``"vpx"`` for
    vCenter, ``"embeddedEsx"`` / ``"esx"`` for ESXi. The canonical
    fingerprint shape demands ``product="vcenter"`` / ``"esxi"`` per
    the consumer's wrapper contract. Unknown values fall through to
    the raw line_id so an ESXi-on-Arm or a future vCenter rebrand is
    still recorded faithfully rather than misclassified as
    ``"unknown"``.
    """
    if line_id == "vpx":
        return "vcenter"
    if line_id in ("embeddedEsx", "esx"):
        return "esxi"
    return line_id or "unknown"


def _extract_session_token(payload: Any, target_name: str) -> str:
    """Coerce a ``POST /api/session`` JSON response to the session token string.

    Handles the two shapes vSphere has shipped across recent releases:

    * **JSON string body** — vSphere 7.0+ modern ``/api/session`` returns
      the token as a JSON-quoted string. ``response.json()`` returns
      :class:`str`.
    * **JSON object body** — pre-7.0 ``/rest/com/vmware/cis/session``
      returned ``{"value": "<token>"}``. Some vcsim builds straddle the
      two shapes; supporting the legacy shape defensively keeps the
      integration test green across simulator versions.

    Anything else raises :exc:`RuntimeError` with the target name in
    the message so the operator can identify the misbehaving endpoint.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        value = payload.get(_SESSION_TOKEN_OBJECT_KEY)
        if isinstance(value, str):
            return value
    raise RuntimeError(
        f"unexpected /api/session response shape for target {target_name!r}: "
        f"got {type(payload).__name__} (expected str or "
        f"{{'{_SESSION_TOKEN_OBJECT_KEY}': str}})"
    )


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3
    targets). Any other value (``"per_user"``, ``"impersonation"``,
    a typo, an int) is rejected by the caller.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


class VmwareRestConnector(HttpConnector):
    """vSphere REST connector for vCenter 8.5+ / ESXi 8.5+ targets.

    Per-target session cached in ``self._session_tokens`` keyed on the
    tenant-unique ``(tenant_id, target.id)`` tuple (#1642/#1672); token
    established on first call to :meth:`auth_headers` via
    ``POST /api/session`` with HTTP basic (service-account creds from
    the injectable :class:`VsphereSessionLoader`); revoked on
    :meth:`aclose` via ``DELETE /api/session``.

    The :attr:`priority` is set to ``1`` so a future :class:`GenericRestConnector`
    auto-shim that somehow registers for the same triple (e.g. a stale
    ingest before this class's module imports) loses the registry's
    tie-break ladder. The auto-shim's idempotency check should prevent
    that case in practice; the priority is defence in depth.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"vmware-rest-9.0"`` -> (``"vmware"``, ``"9.0"``, ``"vmware-rest"``).
    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=8.5,<10.0"
    # Outranks the GenericRestConnector auto-shim's priority=0 if both
    # somehow register for the same triple; the idempotency check in
    # ensure_connector_class_registered should make this unreachable
    # in production, but a defence-in-depth tie-break keeps the
    # resolver behaviour deterministic if the check is ever bypassed.
    priority = 1

    def __init__(
        self,
        *,
        session_loader: VsphereSessionLoader | None = None,
    ) -> None:
        super().__init__()
        # Keyed on the tenant-unique ``(tenant_id, target.id)`` tuple
        # (``target_cache_key``) so two same-named targets in different
        # tenants never share a cached session (#1642/#1672).
        self._session_tokens: dict[tuple[str, str], str] = {}
        # Tracks which session endpoint minted each cached token so
        # :meth:`aclose` can DELETE against the same path. Production
        # vCenter serves both ``/api/session`` and the legacy
        # ``/rest/com/vmware/cis/session``; vcsim serves only the legacy
        # path. See ``SESSION_PATH_MODERN`` / ``SESSION_PATH_LEGACY``
        # for the rationale and source citations. Keyed on the same
        # tenant-unique tuple as ``_session_tokens``.
        self._session_paths: dict[tuple[str, str], str] = {}
        # Records which session *flavor* minted each cached token so
        # teardown picks the right revoke surface (#3363). Absent (the
        # vCenter default) means the vSphere-Automation vAPI —
        # ``aclose`` issues ``DELETE /api/session`` / the recorded legacy
        # path; :data:`HOST_FLAVOR_ESXI` means the VI-JSON
        # ``SessionManager`` — ``aclose`` / ``invalidate_session`` issue
        # ``Logout`` on the SessionManager moid instead. Keyed on the same
        # tenant-unique tuple as ``_session_tokens``.
        self._session_flavors: dict[tuple[str, str], str] = {}
        # ESXi SOAP session bookkeeping (#3363), read from the unauthenticated
        # ``RetrieveServiceContent`` at establish time and keyed on the same
        # tenant-unique tuple. ``_esxi_pc_moids`` is the HostAgent's
        # PropertyCollector moid (NOT the vCenter ``propertyCollector``
        # literal) that :meth:`_post_soap` substitutes into a
        # ``RetrievePropertiesEx`` path; ``_esxi_session_manager_moids`` is
        # the SessionManager moid teardown POSTs ``Logout`` on. Both empty
        # for a vCenter target.
        self._esxi_pc_moids: dict[tuple[str, str], str] = {}
        self._esxi_session_manager_moids: dict[tuple[str, str], str] = {}
        # The host's ``ServiceContent.about.apiVersion`` (e.g. ``9.1.0.0``),
        # read from the unauthenticated ``RetrieveServiceContent`` at establish
        # time. Every ``/sdk`` op past the ``RetrieveServiceContent`` + Login
        # bootstrap MUST announce it as ``SOAPAction: urn:vim25/<apiVersion>``
        # or the host resolves the method against its baseline (2.5u2) schema
        # and 500s ``InvalidRequest`` on ``RetrievePropertiesEx`` /
        # ``MarkAs*_Task`` / ``CreateNasDatastore`` (#3363 State-2). Empty for a
        # vCenter target.
        self._esxi_api_versions: dict[tuple[str, str], str] = {}
        # Per-target httpx ``extensions`` (the ``tls_server_name`` SNI /
        # cert-verify override, evoila/meho#2398) captured at establish
        # time, keyed on the same tenant-unique tuple. :meth:`aclose`
        # replays it on the best-effort session-revoke DELETE, which has
        # no ``Target`` in scope (it iterates cached tokens by key), so a
        # by-IP appliance that pins its cert to an FQDN is revoked over
        # the same SNI-corrected handshake the establish call used.
        self._session_extensions: dict[tuple[str, str], dict[str, Any]] = {}
        # Per-target version for the VI-JSON ``{release}`` segment
        # (``GET /api/about``'s ``version``, or the vim25 API version
        # from ``/sdk/vimServiceVersions.xml`` when ``/api/about`` 404s
        # on vCenter, #2765), resolved once and cached so
        # :meth:`_post_vmomi_json` costs one probe per target rather than
        # one per vmomi read. Keyed on the same tenant-unique tuple as
        # ``_session_tokens``. ``None`` records "probe failed / no version"
        # so the vmomi path falls back to the ``/api`` mount without
        # re-probing on every call.
        self._about_versions: dict[tuple[str, str], str | None] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: VsphereSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: VsphereTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"vmware-api-session-id": <token>}`` for the request.

        Lazily establishes the session on first call against *target*;
        subsequent calls reuse the cached token. The full ``operator`` is
        threaded to the :class:`VsphereSessionLoader` so the default
        loader (G3.9-T3's :func:`load_session_credentials_from_vault`)
        can read the service-account credentials from Vault under the
        operator's identity (``vault_client_for_operator(operator)``). An
        injected test loader receives the same ``(target, operator)``
        pair.

        Raises :exc:`NotImplementedError` (with ``target.name`` and the
        requested mode in the message) if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        Per-user and impersonation modes are deferred to v0.2.next.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VmwareRestConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        if self._session_flavors.get(target_cache_key(target)) == HOST_FLAVOR_ESXI:
            # ESXi (#3363): auth rides the ``vmware_soap_session`` cookie kept
            # in the pooled client's jar (set by ``SessionManager.Login``), not
            # a request header. Establishing the session (above) is the whole
            # job here; the cached ``token`` is the opaque cookie sentinel, not
            # a header value, so add no header.
            return {}
        return {_SESSION_HEADER: token}

    async def mount_op_path(self, target: VsphereTargetLike, path: str, operator: Operator) -> str:
        """Map a spec-relative ingested-op *path* onto *target*'s live mount.

        Overrides the identity :meth:`HttpConnector.mount_op_path` hook
        the dispatcher calls for ``source_kind='ingested'`` ops. Ingested
        descriptors carry spec-relative paths (``/vcenter/vm``); the
        vCenter REST API is mounted at ``/api`` on modern vCenter and
        ``/rest`` on legacy vCenter / vcsim. Establishing the session is
        what records the live mount in :attr:`_session_paths` (the
        modern→legacy 404 fallback in :meth:`_session_token`); it's
        idempotent + cached, so calling it here costs nothing on the
        warm path and is what lets the *first* op against a legacy-only
        target (vcsim) mount correctly instead of defaulting to ``/api``
        and 404ing. The pure mapping — including the already-mounted
        pass-through — lives in :func:`._mount.mounted_path`.

        ``operator`` is the dispatch op's operator; it is forwarded to
        :meth:`_session_token` so a cold-cache session establish here
        authenticates under the same identity the subsequent transport
        call will.

        This is a dedicated dispatcher hook rather than a
        ``_request_json`` / ``_post_json`` override on purpose: those
        carry tenacity's ``@retry`` (and the ``.retry`` attribute that
        retry-aware tests + callers introspect), and ``fingerprint()``
        reaches ``GET /api/about`` through ``_get_json`` *pre-session*
        — overriding the transport methods would both strip ``.retry``
        and force a spurious session establish on the pre-auth probe.
        """
        await self._session_token(target, operator)
        session_path = self._session_paths.get(target_cache_key(target), SESSION_PATH_MODERN)
        return mounted_path(session_path, path)

    async def adapt_op_query(
        self,
        target: VsphereTargetLike,
        query: Mapping[str, Any] | None,
        operator: Operator,
    ) -> dict[str, Any] | None:
        """Key a ``filter.*`` query bucket off *target*'s live mount flavor.

        The composite sub-call seam (:func:`._read._read_sub_op`) and the
        typed-op listing legs (:func:`.typed_ops.host_usage_impl`,
        :func:`.typed_ops_host_network_uplinks.host_network_uplinks_impl`)
        author their query params in the legacy ``/rest`` style
        (``filter.datastores``, ``filter.hosts``, ...). Modern ``/api``
        vCenter 8.x returns HTTP 400 for that prefixed form and expects the
        bare parameter name; the legacy ``/rest`` mount (and ``vmware/vcsim``)
        requires the prefix. Resolve the live mount the same way
        :meth:`mount_op_path` does — off the established session — and
        delegate the pure key rewrite to :func:`._mount.adapt_filter_params`.

        The session establish is idempotent + cached (mirrors
        :meth:`mount_op_path`), so calling this right after a
        ``mount_op_path`` at the same call site costs nothing on the warm
        path; ``operator`` is forwarded so a cold-cache establish
        authenticates under the dispatch op's identity. Empty / ``None``
        query short-circuits to ``None`` (no session establish needed) so
        an unfiltered listing stays a bare, param-less GET.
        """
        if not query:
            return None
        await self._session_token(target, operator)
        session_path = self._session_paths.get(target_cache_key(target), SESSION_PATH_MODERN)
        return adapt_filter_params(api_mount_for_session_path(session_path), query)

    async def _post_vmomi_json(
        self,
        target: VsphereTargetLike,
        vmomi_path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a typed vmomi (VI-JSON) method on the documented ``/sdk/vim25`` base.

        The vmomi method paths (``/{MoType}/{moId}/{method}`` —
        ``RetrievePropertiesEx``, ``VsanQueryVcClusterHealthSummary``,
        ``QueryEvents``, ``QueryPerf`` …) are served by vCenter under the
        release-versioned VI-JSON base ``/sdk/vim25/{release}`` (Broadcom
        Web Services SDK guide, "Building JSON Request URLs"), **not** the
        vSphere Automation ``/api`` mount that :meth:`mount_op_path`
        resolves for ``/vcenter/*`` paths. Mounting a vmomi method on
        ``/api`` 404s on vCenter 8.0.x — the ``/api``-served form works
        only on the 9.0.2 fleet as an undocumented accommodation, so it is
        kept solely as a single fallback (#2466).

        Resolution, off the target's established session:

        * **legacy / vcsim** (session minted at ``/rest/...``): VI-JSON is
          not served, so the vmomi method mounts on the legacy ``/rest``
          form via :func:`._mount.mounted_path` — the pre-#2466 behaviour,
          unchanged, so the vcsim integration lane stays green.
        * **modern** (session minted at ``/api/session``): derive
          ``{release}`` via :meth:`_about_version` (``GET /api/about``,
          falling back to the version-discovery document on vCenter,
          #2765) and POST ``/sdk/vim25/{release}{vmomi_path}``. On HTTP 404 there
          (a deployment that does not serve VI-JSON at the derived
          release), fall back **once** to the ``/api``-mounted form. When
          the release can't be derived, skip straight to the ``/api`` form.

        When both mounts 404, raises :exc:`RuntimeError` naming both
        attempted URLs and the vCenter version, so a best-effort caller's
        ``read_note`` is self-explanatory (``vi-json unavailable: POST
        /sdk/vim25/8.0.3.0/... and /api/... both 404 on vCenter 8.0.3``)
        rather than a bare 404. Non-404 failures (401 / 403 / 5xx /
        transport) propagate unchanged — those are not "this mount isn't
        served".
        """
        await self._session_token(target, operator)
        # ESXi SOAP branch (#3363). ``_session_token`` established the flavor
        # (and the SOAP session cookie + moid caches) above; a standalone ESXi
        # host serves vmomi only as SOAP over ``POST /sdk``, so route every
        # caller (storage_devices, task polls, config-manager reads, the host
        # write composites) through :meth:`_post_soap`. Everything below is
        # unreached on ESXi and unchanged for vCenter (no vCenter target ever
        # carries the esxi flavor).
        if self._session_flavors.get(target_cache_key(target)) == HOST_FLAVOR_ESXI:
            return await self._post_soap(target, vmomi_path, operator=operator, json=json)
        session_path = self._session_paths.get(target_cache_key(target), SESSION_PATH_MODERN)
        if api_mount_for_session_path(session_path) == API_MOUNT_LEGACY:
            legacy_path = mounted_path(session_path, vmomi_path)
            return await self._post_json(target, legacy_path, operator=operator, json=json)

        api_path = mounted_path(session_path, vmomi_path)
        version = await self._about_version(target, operator)
        release = vmomi_release_from_version(version)
        if release is None:
            return await self._post_json(target, api_path, operator=operator, json=json)

        vijson_path = vmomi_mounted_path(release, vmomi_path)
        try:
            return await self._post_json(target, vijson_path, operator=operator, json=json)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        # Single fallback to the /api-mounted vmomi form (the observed 9.x
        # accommodation); if that also 404s, surface both attempts.
        try:
            return await self._post_json(target, api_path, operator=operator, json=json)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(
                    f"vi-json unavailable: POST {vijson_path} and {api_path} both 404 "
                    f"on vCenter {version}"
                ) from exc
            raise

    async def _about_version(self, target: VsphereTargetLike, operator: Operator) -> str | None:
        """Return *target*'s version string for the VI-JSON ``{release}`` segment.

        Reads ``GET /api/about`` (the same probe :meth:`fingerprint`
        starts with) for its ``version`` field. When that endpoint
        answers HTTP 404 — vCenter serves no ``/api/about`` (#2765) —
        falls back to the unauthenticated version-discovery document's
        vim25 API version, which is exactly the four-part value the
        VI-JSON base is versioned by, so the ``{release}`` derivation
        in :meth:`_post_vmomi_json` works on vCenter too. Cached per
        tenant-unique ``target_cache_key`` so repeated vmomi reads share
        one probe. Any other transport / status / shape failure caches
        and returns ``None`` — the vmomi caller then falls back to the
        ``/api`` mount rather than failing, and the failed probe is not
        retried on every subsequent read.
        """
        cache_key = target_cache_key(target)
        if cache_key in self._about_versions:
            return self._about_versions[cache_key]
        resolved: str | None = None
        try:
            payload = await self._get_json(target, _ABOUT_PATH, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                resolved = await self._service_versions_or_none(target)
        except (httpx.HTTPError, OSError, RuntimeError):
            resolved = None
        else:
            version = payload.get("version") if isinstance(payload, dict) else None
            resolved = version if isinstance(version, str) and version else None
        self._about_versions[cache_key] = resolved
        return resolved

    async def _fetch_service_versions_api_version(self, target: VsphereTargetLike) -> str | None:
        """GET the version-discovery document and return its vim25 API version.

        Issues a bare GET on the pooled per-target client — deliberately
        not :meth:`_get_json`: the document is XML (not JSON) and both
        vCenter and ESXi serve it without authentication (pyVim fetches
        it pre-login), so injecting :meth:`auth_headers` would force a
        session establish the read doesn't need. Transport / status
        errors propagate to the caller, which decides reachability; a
        200 whose body yields no parsable version returns ``None``.
        """
        client = await self._http_client(target)
        resp = await client.get(_SERVICE_VERSIONS_PATH, extensions=self._request_extensions(target))
        resp.raise_for_status()
        return service_versions_api_version(resp.text)

    async def _service_versions_or_none(self, target: VsphereTargetLike) -> str | None:
        """Error-swallowing :meth:`_fetch_service_versions_api_version` wrapper.

        For callers (the ``{release}`` derivation) where a failed
        discovery read must degrade to "no version" rather than raise —
        :meth:`_fingerprint_via_service_versions` by contrast needs the
        error to report reachability truthfully, so it calls the
        raising form directly.
        """
        try:
            return await self._fetch_service_versions_api_version(target)
        except (httpx.HTTPError, OSError):
            return None

    async def _session_token(self, target: VsphereTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, establishing one on first use.

        The lock serialises concurrent first-use for one target; the
        cache fast-path means subsequent callers are bounded only by
        the lock acquisition itself. The slow ``POST /api/session`` call
        runs under the lock so two concurrent first-use callers against
        the same target don't both pay the round-trip cost.

        Endpoint fallback: POSTs to the modern ``/api/session`` first;
        on HTTP 404 (only) falls back to ``/rest/com/vmware/cis/session``.
        Real vCenter serves both paths, so production targets succeed on
        the first attempt; the upstream ``vmware/vcsim`` simulator
        registers only the legacy path (per ``govmomi/vapi/simulator``)
        and exercises the fallback. The successful path is recorded in
        ``self._session_paths`` so :meth:`aclose` DELETEs the matching
        endpoint. 401 / 403 / 5xx on the modern path are *not* retried
        on the legacy path — those are auth/server failures, not "this
        deployment doesn't have the modern endpoint".

        ``operator`` is forwarded to the
        :class:`VsphereSessionLoader` so the credential read runs under
        the operator's identity (G3.9-T3's live read). The default loader
        (:func:`load_session_credentials_from_vault`) performs that live
        operator-context Vault read; injected test loaders accept the
        same ``(target, operator)`` pair.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check mirroring the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`.
        The primary fail-closed gate against empty ``raw_jwt`` is the
        loader's ``vault_client_for_operator`` / ``load_basic_credentials``
        call chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return a cached vSphere
        session token to an unauthenticated caller via a cache hit.
        :meth:`auth_headers` enforces only the ``auth_model`` boundary
        (rejects ``per_user`` / ``impersonation`` under
        ``shared_service_account`` scoping). Raised before the cache lookup
        so a primed token from an authenticated caller cannot leak to a
        system-initiated caller. See ``docs/architecture/connector-auth.md``
        § "Cache scoping under ``shared_service_account``" for the contract.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None:
                return cached
            return await self._establish_and_cache_session(target, operator, cache_key)

    async def _establish_and_cache_session(
        self,
        target: VsphereTargetLike,
        operator: Operator,
        cache_key: tuple[str, str],
    ) -> str:
        """Establish a fresh vSphere session for *target* and cache it.

        Called by :meth:`_session_token` under ``self._session_lock`` on a
        cold cache. Resolves credentials via the loader, POSTs to the modern
        session endpoint (falling back to the legacy path on a 404 only),
        and records the token and the endpoint that minted it against the
        tenant-unique *cache_key* — the same key the shared
        ``HttpConnector._clients`` pool now uses (evoila/meho#1682), so
        :meth:`aclose` can locate the per-target client directly by
        *cache_key* without a name reverse-map.
        """
        creds = await self._session_loader(target, operator)
        client = await self._http_client(target)
        try:
            username = creds["username"]
            password = creds["password"]
        except KeyError as exc:
            # Surface a clear error if the loader returned a dict
            # missing one of the two required keys — a typo in a
            # production loader implementation otherwise surfaces
            # as a confusing TypeError deep inside httpx's auth
            # builder.
            raise RuntimeError(
                f"vsphere session loader for target {target.name!r} returned "
                f"a dict missing required key {exc.args[0]!r}; need "
                "{'username': str, 'password': str}"
            ) from exc
        auth = (username, password)
        extensions = self._request_extensions(target)
        # ESXi-native branch (#3363): a target whose probe fingerprint
        # already names ``product=esxi`` (#3332) mints over VI-JSON —
        # ``/api/session`` is a JSON-RPC handler on a standalone ESXi host
        # and never yields a token. vCenter targets (fingerprint absent /
        # vcenter / unreachable) keep the vAPI path below byte-for-byte.
        if self._target_is_esxi(target):
            return await self._establish_esxi_session(
                target, cache_key, auth=auth, extensions=extensions
            )
        resp = await client.post(SESSION_PATH_MODERN, auth=auth, extensions=extensions)
        established_path = SESSION_PATH_MODERN
        if resp.status_code == 404:
            # Modern endpoint not served (vcsim, very old vCenter,
            # or a reverse-proxy that hasn't been updated). Try the
            # legacy path before declaring failure.
            resp = await client.post(SESSION_PATH_LEGACY, auth=auth, extensions=extensions)
            established_path = SESSION_PATH_LEGACY
        elif resp.status_code == 400 and self._is_esxi_jsonrpc_response(resp):
            # First probe, before any fingerprint exists: this is ESXi's
            # JSON-RPC 2.0 answer on the vAPI session path (#3363), the
            # signature that today dead-ends the modern→legacy fallback
            # (400, not 404). Switch to the VI-JSON login branch. vCenter
            # never returns this shape, so its path is unaffected.
            return await self._establish_esxi_session(
                target, cache_key, auth=auth, extensions=extensions
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Wrap so the operator-facing message names the target;
            # httpx's default str() shows only the URL/status, which
            # loses the per-target identification the dispatcher's
            # audit row needs. The path in the message is the last
            # one attempted, which distinguishes a real 404 (legacy
            # also missing) from auth/server failure on the modern
            # path.
            message = (
                f"vsphere session establish failed for target {target.name!r}: "
                f"POST {established_path} returned HTTP {exc.response.status_code}"
            )
            # #2329: a 401 (rotated/stale password) / 403 (locked-out account)
            # at establish is an auth-class failure -- raise the structured
            # ``ConnectorAuthError`` the dispatcher maps to
            # ``connector_auth_failed`` (restage-the-credential remediation)
            # instead of the opaque ``connector_error: RuntimeError``. A real
            # 404 / 5xx keeps the bare RuntimeError shape.
            raise (
                session_establish_auth_error(exc, message=message, target=target)
                or RuntimeError(message)
            ) from exc
        token = _extract_session_token(resp.json(), target.name)
        self._session_tokens[cache_key] = token
        self._session_paths[cache_key] = established_path
        self._session_extensions[cache_key] = extensions
        _log.info(
            "vsphere_session_established",
            target=target.name,
            host=target.host,
            session_path=established_path,
        )
        return token

    def _target_is_esxi(self, target: VsphereTargetLike) -> bool:
        """Return ``True`` iff *target*'s probe fingerprint names ``product=esxi``.

        Reuses the shared #3332 distinguisher
        (:func:`~meho_backplane.connectors.vmware_rest.host_target.classify_host_target`)
        so session establishment, the host-composite park-time preview,
        and the typed host reads all classify a target identically — the
        #3312 preview/call parity invariant. An absent / unreachable /
        vCenter fingerprint is *not* esxi (the classifier's vCenter
        default), so a managing-vCenter target — probed or not — never
        takes the ESXi login branch.
        """
        return classify_host_target(target)[0] == HOST_FLAVOR_ESXI

    @staticmethod
    def _is_esxi_jsonrpc_response(resp: httpx.Response) -> bool:
        """Return ``True`` when *resp* is ESXi's JSON-RPC answer on ``/api/session``.

        A standalone ESXi host serves ``POST /api/session`` through a
        JSON-RPC 2.0 handler that answers a bodyless Basic-auth POST with
        HTTP 400 and a ``{"jsonrpc":"2.0","error":{"code":400,"message":
        "Unsupported content type: "}}`` body — never the vSphere-Automation
        token vCenter mints. Recognising that signature is how the connector
        selects the ESXi SOAP branch on the very first probe, before any
        fingerprint exists.

        The caller has already confirmed HTTP 400 (itself diagnostic —
        vCenter answers 401 / a token there, never 400); this is the
        belt-and-suspenders body confirmation. Layered: the JSON-RPC
        discriminator (``jsonrpc == "2.0"``) is the primary body signal, the
        ``"Unsupported content type"`` message text (JSON or bare) is the
        confirmation, so a body that is JSON-parseable but not an object, or
        not JSON at all, still matches on the text last-resort. A genuine
        vCenter 400 (an HTML error page, a structured vAPI error without any
        of these markers) reads as *not* ESXi, so vCenter's establish path is
        unaffected. The body is already buffered (``client.post`` read it in
        full), so ``resp.json()`` / ``resp.text`` cost no extra I/O.
        """
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and str(body.get(_JSONRPC_KEY, "")) == _JSONRPC_VERSION:
            return True
        return _ESXI_UNSUPPORTED_CONTENT_TYPE in resp.text

    async def _establish_esxi_session(
        self,
        target: VsphereTargetLike,
        cache_key: tuple[str, str],
        *,
        auth: tuple[str, str],
        extensions: dict[str, Any],
    ) -> str:
        """Mint a session on a standalone ESXi host over SOAP ``SessionManager.Login`` (#3363).

        Two ordered SOAP posts on ``POST /sdk`` (the only vmomi endpoint a
        HostAgent serves):

        1. **unauthenticated ``RetrieveServiceContent``** on the
           ``ServiceInstance`` singleton — yields the HostAgent's
           ``propertyCollector`` + ``sessionManager`` MoRefs (which are *not*
           the vCenter literals) and ``about`` (``version`` / ``apiType ==
           "HostAgent"``). The PC / SessionManager moids are cached in
           ``_esxi_pc_moids`` / ``_esxi_session_manager_moids`` (the moid
           remap :meth:`_post_soap` and teardown ``Logout`` need); the
           ``about.version`` is cached in ``_about_versions`` so
           :meth:`_fingerprint_esxi` reads it without a re-probe.
        2. **``SessionManager.Login``** on the ServiceContent-provided
           SessionManager moid — a 200 sets a ``vmware_soap_session`` cookie,
           which ``httpx`` stores in the pooled client's cookie jar. That
           cookie is the auth for every subsequent ``/sdk`` POST (and the
           teardown ``Logout``); the cached sentinel token is the opaque
           cookie value, never the password.

        The ESXi flavor is recorded in ``_session_flavors`` so
        :meth:`auth_headers` adds no header (cookie-carried auth) and teardown
        (:meth:`aclose` / :meth:`invalidate_session`) posts ``Logout`` on the
        SessionManager moid rather than ``DELETE /api/session``.
        ``_session_paths`` is left unset (no vAPI mount applies).

        Called by :meth:`_establish_and_cache_session` under
        ``self._session_lock`` on a cold cache. Credentials never appear in
        logs, errors, results, or the flight-recorder span: the Login
        envelope is the only place the password lives (XML-escaped by
        :func:`.soap.build_login_envelope`) and it is never logged nor handed
        to the vendor-call span (:meth:`_soap_post` records the span with no
        body). A rejected credential (``InvalidLogin`` / ``NoPermission``
        fault) raises :class:`ConnectorAuthError`; any other fault / status a
        ``RuntimeError`` — both naming only the target.
        """
        client = await self._http_client(target)
        # 1. Unauthenticated ServiceContent bootstrap read.
        sc_resp = await self._soap_post(client, build_service_content_envelope(), extensions)
        sc_fault = parse_soap_fault(sc_resp.text)
        if sc_fault is not None:
            raise RuntimeError(
                f"vsphere session establish failed for target {target.name!r}: "
                f"SOAP RetrieveServiceContent faulted "
                f"({sc_fault.fault_type or sc_fault.faultcode})"
            )
        sc_resp.raise_for_status()
        content = parse_service_content(sc_resp.text)
        pc_ref = content.get("propertyCollector")
        sm_ref = content.get("sessionManager")
        pc_moid = pc_ref.get("value") if isinstance(pc_ref, dict) else None
        sm_moid = sm_ref.get("value") if isinstance(sm_ref, dict) else None
        if not isinstance(pc_moid, str) or not isinstance(sm_moid, str):
            raise RuntimeError(
                f"vsphere session establish failed for target {target.name!r}: "
                f"SOAP RetrieveServiceContent returned no propertyCollector / "
                f"sessionManager MoRef"
            )
        about = content.get("about")
        version = about.get("version") if isinstance(about, dict) else None
        api_version = about.get("apiVersion") if isinstance(about, dict) else None
        # 2. Login on the ServiceContent-provided SessionManager moid.
        username, password = auth
        login_resp = await self._soap_post(
            client,
            build_login_envelope(
                sm_moid, username=username, password=password, locale=_ESXI_LOGIN_LOCALE
            ),
            extensions,
        )
        login_fault = parse_soap_fault(login_resp.text)
        if login_fault is not None:
            # A rejected credential is an auth-class fault (the SOAP analogue
            # of a vCenter 401/403 at POST /api/session); anything else a
            # bare RuntimeError. Message names only the target — no envelope.
            message = (
                f"vsphere session establish failed for target {target.name!r}: "
                f"SOAP SessionManager.Login rejected the credential"
            )
            raise self._soap_fault_error(login_fault, target, message=message)
        login_resp.raise_for_status()
        cookie = login_resp.cookies.get(_ESXI_SOAP_SESSION_COOKIE)
        if not cookie:
            raise RuntimeError(
                f"vsphere session establish failed for target {target.name!r}: "
                f"SOAP SessionManager.Login returned HTTP {login_resp.status_code} "
                f"without a {_ESXI_SOAP_SESSION_COOKIE} cookie"
            )
        self._session_tokens[cache_key] = cookie
        self._session_flavors[cache_key] = HOST_FLAVOR_ESXI
        self._session_extensions[cache_key] = extensions
        self._esxi_pc_moids[cache_key] = pc_moid
        self._esxi_session_manager_moids[cache_key] = sm_moid
        # Pin every subsequent /sdk op to the host's own vim API version (the
        # bootstrap RetrieveServiceContent + Login above ran on the baseline
        # schema, which lacks RetrievePropertiesEx / the writes -- #3363).
        if isinstance(api_version, str) and api_version:
            self._esxi_api_versions[cache_key] = api_version
        # Cache about.version so ``_fingerprint_esxi`` reads it without a
        # re-probe (GET /api/about 400s on ESXi). Unlike the vCenter path,
        # this is the display version (e.g. "9.1.0"), not a VI-JSON release:
        # the SOAP ops never consult ``_about_version`` (the esxi guard in
        # ``_post_vmomi_json`` returns before it).
        self._about_versions[cache_key] = version
        _log.info(
            "vsphere_session_established",
            target=target.name,
            host=target.host,
            session_flavor=HOST_FLAVOR_ESXI,
        )
        return cookie

    async def _soap_post(
        self,
        client: httpx.AsyncClient,
        envelope: str,
        extensions: dict[str, Any],
        *,
        soap_action: str = "",
    ) -> httpx.Response:
        """POST a SOAP 1.1 *envelope* on ``/sdk``, recording a body-free span.

        The shared low-level wire helper for every ESXi vmomi POST
        (ServiceContent, Login, Logout, and the :meth:`_post_soap` ops). It
        **bypasses** :meth:`_post_json` deliberately: the vmomi auth is the
        ``vmware_soap_session`` cookie the pooled client carries, not the
        ``application/json`` + ``auth_headers`` the JSON seam applies, and —
        the load-bearing credential-posture point (#3363) — the
        flight-recorder vendor-call span (#3214) is recorded here with **no
        request body**, so the Login envelope's ``<password>`` can never be
        serialised into a captured span (which routing through ``_post_json``
        would do). The pooled client contributes its TLS-pin → insecure →
        default precedence and the ``sni_hostname`` extension for free.

        *soap_action* is the ``SOAPAction`` header. The bootstrap pair
        (``RetrieveServiceContent`` + ``Login``) leaves it empty — those
        resolve on the host's baseline schema and the version is not yet known
        — while :meth:`_post_soap` passes ``urn:vim25/<about.apiVersion>`` so
        the op resolves on the host's own schema instead of the baseline
        (2.5u2) one, which lacks ``RetrievePropertiesEx`` / ``MarkAs*_Task`` /
        the datastore write (#3363 State-2).
        """
        _fr_start = flight_recorder_capture.span_start()
        resp = await client.post(
            _SDK_PATH,
            content=envelope.encode("utf-8"),
            headers={"Content-Type": SOAP_CONTENT_TYPE, "SOAPAction": soap_action},
            extensions=extensions,
        )
        flight_recorder_capture.record_vendor_call(
            _fr_start,
            method="POST",
            request_headers={},
            response=resp,
            request_body=None,
            request_content_type=None,
        )
        return resp

    async def _post_soap(
        self,
        target: VsphereTargetLike,
        vmomi_path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The ESXi twin of the VI-JSON transport: one vmomi method as SOAP over ``/sdk``.

        Reached from :meth:`_post_vmomi_json`'s esxi guard for every host
        caller (``RetrievePropertiesEx`` storage reads / config-manager reads
        / task polls, ``QueryBootDevices``, ``CreateNasDatastore``,
        ``MarkAs*_Task``). Parses ``vmomi_path`` = ``/{MoType}/{moId}/{method}``,
        builds the method's SOAP envelope from the same VI-JSON body the
        vCenter path would send (:meth:`_build_esxi_soap_envelope` — with the
        guarded ``propertyCollector``-moid substitution), POSTs it on the
        pooled client (cookie-carried auth), then deserialises the response
        back into the exact VI-JSON dict shape the unchanged consumer reads
        (:meth:`_parse_esxi_soap_response`).

        **Fault ordering (belt-and-suspenders).** A vim25 SOAP fault is HTTP
        500 with a ``<soapenv:Fault>`` body, but the fault parse — not the
        status — is the authority, so :func:`.soap.parse_soap_fault` runs on
        the body for **both 200 and 500** responses *before*
        ``raise_for_status``: an ``InvalidLogin`` / ``NoPermission`` fault
        (a session that expired mid-op) becomes :class:`ConnectorAuthError`
        (which the dispatcher's auth-recovery maps to
        :meth:`invalidate_session` → one cold re-login), a write fault
        (``PlatformConfigFault`` / ``HostConfigFault`` / ``InvalidArgument`` /
        ``DuplicateName`` …) a :class:`RuntimeError` carrying the faultstring
        + the fault-type localName. Only a genuinely non-fault 5xx propagates
        through ``raise_for_status`` unchanged.
        """
        cache_key = target_cache_key(target)
        client = await self._http_client(target)
        extensions = self._request_extensions(target)
        mo_type, moid, method = self._parse_vmomi_path(vmomi_path)
        envelope = self._build_esxi_soap_envelope(cache_key, mo_type, moid, method, json or {})
        # Pin the op to the host's own vim API version (cached at establish
        # from ServiceContent.about.apiVersion); the baseline schema an empty
        # SOAPAction selects lacks these methods (#3363 State-2). Absent only
        # if establish somehow read no apiVersion -- fall back to empty so the
        # host faults loud rather than the connector silently mis-versioning.
        api_version = self._esxi_api_versions.get(cache_key)
        soap_action = soap_action_for_version(api_version) if api_version else ""
        resp = await self._soap_post(client, envelope, extensions, soap_action=soap_action)
        fault = parse_soap_fault(resp.text)
        if fault is not None:
            message = f"vmware vim {method} failed on target {target.name!r} ({mo_type}:{moid})"
            raise self._soap_fault_error(fault, target, message=message)
        resp.raise_for_status()
        return self._parse_esxi_soap_response(method, resp.text)

    @staticmethod
    def _parse_vmomi_path(vmomi_path: str) -> tuple[str, str, str]:
        """Split ``/{MoType}/{moId}/{method}`` into its three parts.

        The vmomi method path every ``_post_vmomi_json`` caller uses. The
        method is the last segment and the MoType the first; a moid with an
        embedded ``/`` (none in practice — vim moids never carry one) is
        rejoined defensively so the split can never mis-place the method.
        """
        segments = [seg for seg in vmomi_path.split("/") if seg]
        if len(segments) < 3:
            raise RuntimeError(f"malformed vmomi path {vmomi_path!r}: expected /MoType/moId/method")
        return segments[0], "/".join(segments[1:-1]), segments[-1]

    def _build_esxi_soap_envelope(
        self,
        cache_key: tuple[str, str],
        mo_type: str,
        moid: str,
        method: str,
        body: dict[str, Any],
    ) -> str:
        """Select the per-method SOAP builder for *method* and build its envelope.

        *body* is the VI-JSON method-args dict the vCenter path would POST
        (``retrieve_properties_body`` output, ``{"spec": HostNasVolumeSpec}``,
        ``{"scsiDiskUuid": …}``). ``RetrievePropertiesEx`` remaps the
        PropertyCollector moid: callers bake the vCenter ``propertyCollector``
        literal into the path, but a HostAgent's PC moid comes from
        ServiceContent — so substitute the cached moid **only** when the
        caller's moid equals that literal (any other PC moid is left
        untouched, a guarded substitution).
        """
        if method == "RetrievePropertiesEx":
            pc_moid = moid
            if moid == _VCENTER_PROPERTY_COLLECTOR_MOID:
                pc_moid = self._esxi_pc_moids.get(cache_key, moid)
            return build_retrieve_properties_ex_envelope(
                pc_moid, body.get("specSet", []) or [], body.get("options")
            )
        if method == "QueryBootDevices":
            return build_query_boot_devices_envelope(moid)
        if method == "CreateNasDatastore":
            return build_create_nas_datastore_envelope(moid, body.get("spec", {}) or {})
        if method in ("MarkAsSsd_Task", "MarkAsNonSsd_Task"):
            return build_mark_ssd_envelope(
                moid, str(body.get("scsiDiskUuid", "")), ssd=(method == "MarkAsSsd_Task")
            )
        raise RuntimeError(
            f"vmware vim method {method!r} has no SOAP builder — the standalone-ESXi "
            f"SOAP transport (#3363) implements RetrievePropertiesEx / QueryBootDevices "
            f"/ CreateNasDatastore / MarkAs*_Task only"
        )

    @staticmethod
    def _parse_esxi_soap_response(method: str, xml: str) -> dict[str, Any]:
        """Deserialise a SOAP response for *method* into its VI-JSON dict shape.

        ``RetrievePropertiesEx`` → the ``RetrieveResult`` (``{"objects": […]}``),
        ``QueryBootDevices`` → the ``HostBootDeviceInfo``,
        ``CreateNasDatastore`` / ``MarkAs*_Task`` → the returned
        ``ManagedObjectReference`` (``{"type", "value"}``) — the exact shapes
        the unchanged consumers (``_extract_host_props``, ``poll_vim_task``,
        the host composites) already read. A MoRef method with no
        ``returnval`` degrades to ``{}`` so the dict return contract holds.
        """
        if method == "RetrievePropertiesEx":
            return parse_retrieve_result(xml)
        if method == "QueryBootDevices":
            return parse_boot_devices(xml)
        # CreateNasDatastore / MarkAs*_Task -> a MoRef returnval.
        return parse_moref_result(xml, method) or {}

    def _soap_fault_error(
        self,
        fault: SoapFault,
        target: VsphereTargetLike,
        *,
        message: str,
    ) -> Exception:
        """Map a parsed :class:`SoapFault` to the error the caller should raise.

        An ``InvalidLogin`` / ``NoPermission`` / ``NotAuthenticated`` fault →
        :class:`ConnectorAuthError` (``status_code=401`` — the auth-reject
        analogue — so the dispatcher's ``connector_auth_failed`` + cold
        re-login path fires, same restage remediation as a vCenter 401/403);
        every other fault → :class:`RuntimeError` carrying the faultstring +
        the fault-type localName. *message* is the site's target-named string
        (never the envelope). ``SoapFault`` carries only
        ``faultcode`` / ``faultstring`` / ``fault_type``, none of which echo
        the Login envelope, so the credential cannot leak through either arm.
        """
        if fault.fault_type in _SOAP_AUTH_FAULT_TYPES:
            return ConnectorAuthError(
                message,
                status_code=401,
                cause=f"session_establish_{fault.fault_type}",
                target_name=getattr(target, "name", None),
                host=getattr(target, "host", None),
                secret_ref=getattr(target, "secret_ref", None),
            )
        fault_type = fault.fault_type or fault.faultcode or "SoapFault"
        detail = f": {fault.faultstring}" if fault.faultstring else ""
        return RuntimeError(f"{message}: vim fault {fault_type}{detail}")

    def _pooled_client_for(self, cache_key: tuple[str, str]) -> httpx.AsyncClient | None:
        """Return the pooled per-target client whose key carries *cache_key*'s prefix.

        ``_session_tokens`` is keyed on the tenant-unique
        ``(tenant_id, target.id)`` tuple, while the shared
        ``HttpConnector._clients`` pool keys that same prefix plus a
        ``verify_tls`` dimension (#1682/#1774). A cached token was minted
        against exactly one such client, so match the pool entry whose key
        starts with this token's prefix — the session-revoke paths need it
        without a ``Target`` in scope (they iterate cached tokens by key).
        """
        return next(
            (
                pooled
                for client_key, pooled in self._clients.items()
                if client_key[: len(cache_key)] == cache_key
            ),
            None,
        )

    async def _esxi_logout_quiet(
        self,
        cache_key: tuple[str, str],
        sm_moid: str | None,
        extensions: dict[str, Any],
    ) -> None:
        """Best-effort SOAP ``SessionManager.Logout`` for an ESXi-flavored session (#3363).

        Mirrors the vAPI ``DELETE`` revoke's best-effort discipline: a
        failure (transport, non-2xx, or a missing SessionManager moid) is
        logged via structlog and swallowed — teardown must not block on an
        unreachable host, and a stale cookie (the 401-recovery case) fails
        harmlessly. Runs *outside* ``self._session_lock`` (callers drop the
        cache entry under the lock first), matching :meth:`aclose`. POSTs the
        arg-less ``Logout`` on the ServiceContent-provided SessionManager
        moid over the same pooled client the login used; auth is the
        ``vmware_soap_session`` cookie still in that client's jar, not a
        header. A vim fault body (e.g. the cookie already expired) reads as a
        non-2xx / benign outcome — best-effort, so it is not re-raised.
        """
        if not sm_moid:
            _log.warning(
                "vsphere_session_revoke_skipped",
                target=cache_key,
                session_flavor=HOST_FLAVOR_ESXI,
                reason="no SessionManager moid cached for the SOAP Logout",
            )
            return
        client = self._pooled_client_for(cache_key)
        if client is None:
            return
        try:
            resp = await self._soap_post(client, build_logout_envelope(sm_moid), extensions)
            if resp.status_code >= 400 or parse_soap_fault(resp.text) is not None:
                _log.warning(
                    "vsphere_session_revoke_non_2xx",
                    target=cache_key,
                    status_code=resp.status_code,
                    session_flavor=HOST_FLAVOR_ESXI,
                )
        except (httpx.HTTPError, OSError) as exc:
            _log.warning(
                "vsphere_session_revoke_failed",
                target=cache_key,
                error=f"{type(exc).__name__}: {exc}",
                session_flavor=HOST_FLAVOR_ESXI,
            )

    # #2396: vmware_rest deliberately exposes NO ``invalidate_credentials``
    # hook. It caches only the session token (evicted below); the
    # service-account credentials are re-read from Vault via ``_session_loader``
    # on every ``_establish_and_cache_session``, so a restage already converges
    # on the next cold-session dispatch with no credential cache to evict.
    async def invalidate_session(self, target: VsphereTargetLike) -> None:
        """Evict the cached session token + login path for *target*.

        The duck-typed recovery hook the generic-ingested dispatch path calls
        on an auth-class status (401 / vRLI's 440) before re-dispatching the
        op once (G0.29-T2 #2067). Dropping the cached token forces the next
        :meth:`_session_token` to miss the cache and re-run
        :meth:`_establish_and_cache_session`, which re-authenticates and
        re-runs the modern->legacy ``/api/session`` 404 fallback from a clean
        state -- the path that recovers vCenter's cold-401 (the freshly minted
        token expired server-side) without a backplane restart. An ESXi-
        flavored session (#3363) re-establishes the same way, through the SOAP
        ``RetrieveServiceContent`` + ``SessionManager.Login`` branch, so a
        stale ``vmware_soap_session`` cookie cold-re-logs-in exactly as
        vCenter's token does.

        Evicts under ``self._session_lock`` keyed on the tenant-unique
        ``target_cache_key(target)`` tuple, so the per-``(tenant_id,
        target.id)`` isolation (#1642/#1672/#1684) holds across eviction and
        re-establish: two same-named targets in different tenants never share
        or clobber each other's cache slot. The recorded login path + flavor
        + SOAP moids are dropped alongside the token so the re-establish
        rediscovers them from a fresh ServiceContent read. The credentials are
        not touched -- a 401/440 means the *session token* expired or was
        rejected, not that the service-account credential is wrong. The hook
        is a no-op when no token is cached.

        For an ESXi-flavored session a best-effort SOAP ``SessionManager.Logout``
        is issued *after* the cache entry is dropped (and outside the lock),
        so the server-side session is released too (#3363). It is best-effort
        by design: a stale cookie — the common 401-recovery case — fails
        harmlessly, and the cold re-login proceeds regardless. The vCenter
        path stays network-free here (its ``DELETE /api/session`` lives in
        :meth:`aclose`), so vCenter behaviour is unchanged.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            token = self._session_tokens.pop(cache_key, None)
            flavor = self._session_flavors.pop(cache_key, None)
            self._session_paths.pop(cache_key, None)
            extensions = self._session_extensions.pop(cache_key, None)
            # Drop the cached about-version too so an auth-recovery cycle
            # re-probes -- this also un-poisons a slot where an earlier probe
            # cached ``None`` transiently. The ESXi PC moid is dropped; the
            # SessionManager moid is captured first for the SOAP Logout below.
            self._about_versions.pop(cache_key, None)
            self._esxi_pc_moids.pop(cache_key, None)
            self._esxi_api_versions.pop(cache_key, None)
            sm_moid = self._esxi_session_manager_moids.pop(cache_key, None)
        if flavor == HOST_FLAVOR_ESXI and token is not None:
            await self._esxi_logout_quiet(cache_key, sm_moid, extensions or {})

    async def fingerprint(
        self,
        target: VsphereTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from the #2765 probe chain.

        ``GET /api/about`` first — the richest source, but it is the
        ESXi host REST surface and vCenter answers HTTP 404. On that
        404 (specifically — transport/auth failures keep the
        unreachable arm) the probe falls back to the unauthenticated
        ``/sdk/vimServiceVersions.xml`` version-discovery document,
        best-effort enriched by ``GET /api/appliance/system/version``
        (VCSA-only). ``probe_method`` always names the endpoint(s) that
        produced the result. Pre-#2765 the probe stopped at
        ``/api/about``, so every vCenter target permanently read
        ``reachable=False``.

        The session token is fetched lazily by :meth:`auth_headers`
        (called transitively through :meth:`HttpConnector._request_json`).
        On transport or status failure, returns a non-reachable
        ``FingerprintResult`` whose ``extras["error"]`` carries the
        exception class + message — same pattern the K8s connector
        established for ``probe()`` failures, plumbed here through
        ``fingerprint()`` so the operator's first ``meho connector
        fingerprint`` call against an unreachable vCenter gets a
        structured response rather than a stack trace.

        ``operator`` (optional) is the request-scoped operator forwarded
        from the probe routes. When provided, the underlying
        :class:`VsphereSessionLoader` reads the per-target Vault secret
        under that identity — the same code path the dispatch surface
        uses. ``None`` falls back to a system operator whose placeholder
        JWT is rejected by the live Vault loader, preserving the
        fail-closed system-call carve-out. G0.16-T4 (#1306) converged
        probe + dispatch on this signature; pre-fix the probe path
        hard-coded the placeholder JWT and surfaced as the v0.8.0
        dogfood's ``malformed jwt: must have three parts`` finding.
        """
        probed_at = datetime.now(UTC)
        # Forward the route operator when present; fall back to the
        # system operator for background callers. The session loader's
        # fail-closed guard rejects the placeholder JWT at the live
        # Vault round-trip, so the system-call carve-out still holds
        # when no real operator is in scope.
        eff_operator = operator if operator is not None else synthesise_system_operator()
        cache_key = target_cache_key(target)
        try:
            payload = await self._get_json(target, _ABOUT_PATH, operator=eff_operator)
        except httpx.HTTPStatusError as exc:
            # A standalone ESXi host answers GET /api/about with HTTP 400
            # (empty) through its JSON-RPC handler — not 404 — so the #2765
            # 404-gated fallback below never fires. But establishing the
            # session for this GET already recognised the host as ESXi (the
            # SOAP RetrieveServiceContent + Login branch, #3363); when it did,
            # fingerprint from the ServiceContent ``about`` the establish
            # already cached rather than dead-ending unreachable on the
            # vAPI-only /api/about.
            if self._session_flavors.get(cache_key) == HOST_FLAVOR_ESXI:
                return await self._fingerprint_esxi(target, probed_at)
            if exc.response.status_code == 404:
                # vCenter serves no GET /api/about (#2765); a session
                # was already established for the GET that 404'd, so
                # the fallback's appliance read reuses it.
                return await self._fingerprint_via_service_versions(target, eff_operator, probed_at)
            return self._unreachable_fingerprint(
                target, probed_at, _ABOUT_PROBE, f"{type(exc).__name__}: {exc}"
            )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            # An ESXi session established but the /api/about GET then failed
            # for another reason: the host is reachable and authenticated, so
            # fingerprint it as ESXi rather than dead-ending unreachable.
            if self._session_flavors.get(cache_key) == HOST_FLAVOR_ESXI:
                return await self._fingerprint_esxi(target, probed_at)
            # RuntimeError catches the session-establish failures from
            # :meth:`_session_token` so an unauthenticatable target
            # surfaces as a clean ``reachable=False`` fingerprint
            # rather than propagating the wrapped exception.
            return self._unreachable_fingerprint(
                target, probed_at, _ABOUT_PROBE, f"{type(exc).__name__}: {exc}"
            )
        return FingerprintResult(
            vendor="vmware",
            product=product_from_line_id(payload.get("product_line_id", "")),
            version=payload.get("version"),
            build=payload.get("build"),
            edition=payload.get("license_product_name"),
            reachable=True,
            probed_at=probed_at,
            probe_method=_ABOUT_PROBE,
            extras={
                "uuid": payload.get("instance_uuid"),
                "full_name": payload.get("full_name"),
                "product_line_id": payload.get("product_line_id"),
                "api_type": payload.get("api_type"),
                "os_type": payload.get("os_type"),
            },
        )

    async def _fingerprint_esxi(
        self,
        target: VsphereTargetLike,
        probed_at: datetime,
    ) -> FingerprintResult:
        """Fingerprint a standalone ESXi target reached over the SOAP session (#3363).

        Reached from :meth:`fingerprint` when session establishment took the
        ESXi-native SOAP ``RetrieveServiceContent`` + ``SessionManager.Login``
        branch: ``GET /api/about`` is the vAPI surface a JSON-RPC ESXi host
        answers HTTP 400 (not 404, so the #2765 404-gated fallback never
        fires). The version is the ``about.version`` the unauthenticated
        ``RetrieveServiceContent`` already returned and the establish cached in
        ``_about_versions`` (exact, e.g. ``9.1.0``); product is stamped
        ``esxi`` (the same slug ``product_from_line_id`` maps ``embeddedEsx`` /
        ``esx`` to and ``classify_host_target`` keys off). ``probe_method``
        names the ``GET /api/about`` 400 → SOAP ``RetrieveServiceContent``
        chain (``about.apiType == "HostAgent"``). A session established, so the
        target is reachable by construction.
        """
        version = self._about_versions.get(target_cache_key(target))
        return FingerprintResult(
            vendor="vmware",
            product=HOST_FLAVOR_ESXI,
            version=version,
            reachable=True,
            probed_at=probed_at,
            probe_method=_ESXI_SOAP_PROBE,
            extras={
                "api_version": version,
                "api_type": "HostAgent",
                "session_flavor": HOST_FLAVOR_ESXI,
            },
        )

    async def _fingerprint_via_service_versions(
        self,
        target: VsphereTargetLike,
        operator: Operator,
        probed_at: datetime,
    ) -> FingerprintResult:
        """Fingerprint a target whose ``GET /api/about`` answered HTTP 404.

        Chain steps 2 + 3 of #2765: read the unauthenticated
        version-discovery document for the vim25 API version (enough
        for ``reachable=True`` + a version), then best-effort enrich
        from the authenticated appliance-version read. Only the
        appliance surface identifies the product as vCenter by
        observation; without it the result carries the target's
        registered product — the discovery document alone cannot
        distinguish vCenter from an older ESXi that predates the host
        REST surface.
        """
        chain = f"{_ABOUT_PROBE} + {_SERVICE_VERSIONS_PROBE}"
        about_extras = {"api_about": "HTTP 404 (endpoint is ESXi-only)"}
        try:
            api_version = await self._fetch_service_versions_api_version(target)
        except (httpx.HTTPError, OSError) as exc:
            return self._unreachable_fingerprint(
                target, probed_at, chain, f"{type(exc).__name__}: {exc}", extras=about_extras
            )
        if api_version is None:
            # A 200 with no parsable vim25 version is an answering
            # socket, not a verified vSphere surface — some other web
            # server on the target's port must not read as reachable.
            return self._unreachable_fingerprint(
                target,
                probed_at,
                chain,
                "vimServiceVersions.xml answered but yielded no vim25 API version",
                extras=about_extras,
            )
        appliance = await self._appliance_version(target, operator)
        if appliance is None:
            return FingerprintResult(
                vendor="vmware",
                product=getattr(target, "product", None) or "unknown",
                version=api_version,
                reachable=True,
                probed_at=probed_at,
                probe_method=_SERVICE_VERSIONS_PROBE,
                extras={"api_version": api_version},
            )
        version = appliance.get("version")
        build = appliance.get("build")
        return FingerprintResult(
            vendor="vmware",
            # The appliance surface is served by VCSA only, so an
            # answer here is observed evidence of vCenter — unlike
            # the failure arms, this is not an assumption.
            product="vcenter",
            version=version if isinstance(version, str) and version else api_version,
            build=build if isinstance(build, str) and build else None,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"{_SERVICE_VERSIONS_PROBE} + {_APPLIANCE_VERSION_PROBE}",
            extras={
                "api_version": api_version,
                "product_name": appliance.get("product"),
                "type": appliance.get("type"),
            },
        )

    async def _appliance_version(
        self, target: VsphereTargetLike, operator: Operator
    ) -> dict[str, Any] | None:
        """Best-effort authenticated ``GET /api/appliance/system/version`` read.

        Returns the appliance ``VersionStruct`` payload (``version`` /
        ``build`` / ``product`` / ``type`` / ...) — the authoritative
        vCenter version source — or ``None`` on any failure: the
        appliance surface is VCSA-only, so its absence (ESXi, vcsim, a
        transient error mid-probe) must not turn an already-verified
        reachable fingerprint red (#2765).
        """
        try:
            payload = await self._get_json(target, _APPLIANCE_VERSION_PATH, operator=operator)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            _log.debug(
                "vsphere_appliance_version_unavailable",
                target=target.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        return payload if isinstance(payload, dict) else None

    def _unreachable_fingerprint(
        self,
        target: VsphereTargetLike,
        probed_at: datetime,
        probe_method: str,
        error: str,
        extras: dict[str, Any] | None = None,
    ) -> FingerprintResult:
        """Build the ``reachable=False`` result for a failed probe chain.

        ``product`` reports the target's *registered* product when the
        concrete target model carries one, falling back to ``"unknown"``
        — a failed probe observed nothing, so the previous hardcoded
        ``"vcenter"`` could stamp a product the target never exhibited
        (#2765). ``probe_method`` names every endpoint the chain
        attempted so the operator can see how far it got.
        """
        merged: dict[str, Any] = {"error": error}
        if extras:
            merged.update(extras)
        return FingerprintResult(
            vendor="vmware",
            product=getattr(target, "product", None) or "unknown",
            reachable=False,
            probed_at=probed_at,
            probe_method=probe_method,
            extras=merged,
        )

    async def probe(self, target: VsphereTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` rather than running a separate
        probe path. The chassis registry's readiness probe and the
        operator-facing ``meho connector probe`` both want a single
        boolean ``ok`` + a reason string; ``fingerprint`` already produces
        the right shape and the extra latency from the ``/api/about``
        payload parsing is negligible compared to the auth round-trip
        ``fingerprint`` already incurs.
        """
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error", "unreachable")),
            probed_at=fp.probed_at,
        )

    async def execute(
        self,
        target: VsphereTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`VaultConnector.execute`'s shape: the connector's
        ABC :meth:`Connector.execute` predates the G0.6 operator-aware
        dispatch path, so this shim exists for pre-G0.6 callers (the
        chassis ``/api/v1/connectors/{product}/{op_id}`` route, any
        :func:`meho_backplane.connectors.resolver.resolve_connector`
        consumer that doesn't already construct an :class:`Operator`).

        Post-G0.6 callers (``/api/v1/operations/call``, MCP
        ``call_operation``, the CLI verbs from #511) construct a real
        :class:`Operator` and call :func:`meho_backplane.operations.dispatch`
        directly — they don't reach this method.

        The shim synthesises a minimal :class:`Operator` carrying a
        nil-UUID tenant_id + a fixed system sentinel ``sub``; typed-
        registrations are always ``tenant_id IS NULL`` in
        ``endpoint_descriptor`` so the dispatcher's tenant-scoped lookup
        falls through to the global row regardless of the synthesised
        value. The dispatcher's audit row records the synthesised
        identity; the real operator identity (when present) lands on
        the audit row written by :class:`AuditMiddleware` upstream of
        this call.
        """
        # Lazy import — meho_backplane.operations.dispatch transitively
        # imports the connector registry which imports this module at
        # package import time; deferring keeps that initialisation
        # order stable.
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vmware-rest-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        # Encode the connector's natural key as the dispatcher's
        # connector_id string per parse_connector_id's contract:
        # ``"vmware-rest-9.0"`` -> (product=``"vmware"``, version=``"9.0"``,
        # impl_id=``"vmware-rest"``).
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def host_usage(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.usage`` -- per-host CPU/memory load + hardware + maintenance.

        The first vmware **typed** op (``source_kind="typed"``): a bound
        method the dispatcher binds to this connector instance and invokes
        with ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        per-host ``summary.quickStats`` / ``summary.hardware`` /
        ``runtime.inMaintenanceMode`` directly on the connector session via
        PropertyCollector -- no ``dispatch_child``, no ingested descriptor
        -- so it works on a fresh boot with zero catalog ingest. The plain
        REST host summary reports only liveness, not load.

        Delegates to :func:`~meho_backplane.connectors.vmware_rest.typed_ops.host_usage_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"hosts": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops import host_usage_impl

        return await host_usage_impl(self, operator, target, params)

    async def host_network_uplinks(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.network_uplinks`` -- per-host pnic link state + uplinks.

        A ``source_kind="typed"`` op (#2258, re-shipped from the former
        ``vmware.composite.host.network_uplinks``): the dispatcher binds
        this method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Lists
        hosts then reads ``config.network.pnic`` +
        ``config.network.proxySwitch`` per host via PropertyCollector
        directly on the connector session -- no ``dispatch_child``, no
        ingested descriptor -- so it works on a fresh boot with zero
        catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks.host_network_uplinks_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"hosts": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_host_network_uplinks import (
            host_network_uplinks_impl,
        )

        return await host_network_uplinks_impl(self, operator, target, params)

    async def host_vsan_health(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.vsan_health`` -- per-cluster vSAN health roll-up.

        A ``source_kind="typed"`` op (#2258, re-shipped from the former
        ``vmware.composite.host.vsan_health``): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`).
        Queries ``VsanQueryVcClusterHealthSummary`` on the
        ``vsan-cluster-health-system`` singleton scoped to the target
        cluster's MoRef, directly on the connector session -- no
        ``dispatch_child``, no ingested descriptor -- so it works on a
        fresh boot with zero catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health.host_vsan_health_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns
        ``{"cluster": ..., "overall_health": ..., "groups": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
            host_vsan_health_impl,
        )

        return await host_vsan_health_impl(self, operator, target, params)

    async def host_storage_devices(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.host.storage_devices`` -- per-host raw SCSI storage devices (#3332).

        A ``source_kind="typed"`` op: the dispatcher binds this method to
        the connector instance and invokes it with ``(operator, target,
        params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`).
        Resolves the host -- a vCenter name/moref via ``GET:/vcenter/host``,
        or the standalone-ESXi ``ha-host`` when the target's probe
        fingerprint is ``product=esxi`` (the host is the target) -- then
        reads ``config.storageDevice.scsiLun`` via PropertyCollector
        directly on the connector session, so it works on a fresh boot with
        zero catalog ingest (the pre-vCenter standalone-ESXi case #3332
        needs). Returns the per-LUN uuid + ssd/local flags +
        capacity/model/vendor set (``is_boot`` best-effort null), the
        runtime input ``host.disk_mark_flash`` needs.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices.host_storage_devices_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns
        ``{"status": ..., "host": ..., "devices": [...], "device_count": ...}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
            host_storage_devices_impl,
        )

        return await host_storage_devices_impl(self, operator, target, params)

    async def vm_info(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.vm.info`` -- single-VM power / guest IP / Tools / heartbeat / usage.

        A ``source_kind="typed"`` incident-triage read (#2300): the
        dispatcher binds this method to the connector instance and invokes
        it with ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`).
        Reads the VirtualMachine managed object's ``runtime.powerState``,
        ``guest.*``, ``guestHeartbeatStatus``, and
        ``storage.perDatastoreUsage`` via PropertyCollector directly on the
        connector session -- no ``dispatch_child``, no ingested descriptor
        -- so it works on a fresh boot with zero catalog ingest. Addresses
        the VM by ``vm`` moid or ``name``.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_vm_info.vm_info_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns a single flat row.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import vm_info_impl

        return await vm_info_impl(self, operator, target, params)

    async def object_collect(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.object.collect`` -- bounded generic PropertyCollector read.

        A ``source_kind="typed"`` op (#2300): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        the caller-specified property paths off a single ``(type, moid)``
        object via PropertyCollector directly on the connector session --
        no ``dispatch_child``, no ingested descriptor -- so it works on a
        fresh boot with zero catalog ingest. Bounded by ``parameter_schema``
        (one object, no traversal, <=64 paths); an oversized request is a
        structured ``invalid_params`` error before the read is issued.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_object_collect.object_collect_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{type, moid, properties, missing}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_object_collect import (
            object_collect_impl,
        )

        return await object_collect_impl(self, operator, target, params)

    async def tasks_recent(
        self,
        operator: Operator,
        target: VsphereTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vmware.tasks.recent`` -- recent vCenter Task objects.

        A ``source_kind="typed"`` op (#2300): the dispatcher binds this
        method to the connector instance and invokes it with
        ``(operator, target, params)`` (see
        :func:`~meho_backplane.operations._branches.dispatch_typed`). Reads
        ``TaskManager.recentTask`` then ``Task.info`` via PropertyCollector
        directly on the connector session -- no ``dispatch_child``, no
        ingested descriptor -- so it works on a fresh boot with zero
        catalog ingest.

        Delegates to
        :func:`~meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent.tasks_recent_impl`
        (imported lazily to keep this module off the typed-ops import at
        class-load time). Returns ``{"tasks": [...]}``.
        """
        from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
            tasks_recent_impl,
        )

        return await tasks_recent_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Revoke every cached session before closing the httpx pool.

        Issues ``DELETE`` against each per-target client at the session
        path recorded by :meth:`_session_token` (modern ``/api/session``
        for production vCenter, legacy ``/rest/com/vmware/cis/session``
        for targets where the modern path 404'd at establish time) before
        delegating to :meth:`HttpConnector.aclose`. An ESXi-flavored
        session (#3363) has no ``DELETE /api/session`` on its JSON-RPC vAPI,
        so it is torn down with a SOAP ``SessionManager.Logout`` on the
        ServiceContent-provided SessionManager moid instead
        (:meth:`_esxi_logout_quiet`). A revoke
        failure (5xx, transport error, target unreachable at shutdown) is
        logged and proceeds — the operator-facing concern at shutdown is
        "tear down the httpx pool", and a hung revoke on an unreachable
        target would otherwise block lifespan exit long enough to trip
        Kubernetes' 30-second terminationGracePeriod.

        The revoke is issued before :meth:`super().aclose` so the
        cached client is still pooled when we need it. After the
        revoke loop, the parent close runs unchanged.
        """
        async with self._session_lock:
            tokens = dict(self._session_tokens)
            paths = dict(self._session_paths)
            flavors = dict(self._session_flavors)
            sm_moids = dict(self._esxi_session_manager_moids)
            extensions_by_key = dict(self._session_extensions)
            self._session_tokens.clear()
            self._session_paths.clear()
            self._session_flavors.clear()
            self._session_extensions.clear()
            self._about_versions.clear()
            self._esxi_pc_moids.clear()
            self._esxi_session_manager_moids.clear()
            self._esxi_api_versions.clear()
        for cache_key, token in tokens.items():
            extensions = extensions_by_key.get(cache_key, {})
            if flavors.get(cache_key) == HOST_FLAVOR_ESXI:
                # ESXi-flavored session: no DELETE /api/session on a
                # JSON-RPC ESXi host — SOAP Logout on the ServiceContent
                # SessionManager moid (cookie-carried, best-effort, same
                # discipline as the vCenter DELETE below).
                await self._esxi_logout_quiet(cache_key, sm_moids.get(cache_key), extensions)
                continue
            # ``_session_tokens`` is keyed on the tenant-unique
            # ``(tenant_id, target.id)`` tuple, while the shared
            # ``HttpConnector._clients`` pool keys that same prefix plus a
            # ``verify_tls`` dimension (evoila/meho#1682/#1774). The token
            # was minted against exactly one per-target client, so match
            # the pool entry whose key starts with this token's
            # ``(tenant_id, id)`` prefix — no name reverse-map needed.
            client = self._pooled_client_for(cache_key)
            if client is None:
                # Theoretically unreachable — every cached token was
                # established against a per-target client that was
                # created during _session_token. Defensive: skip
                # cleanly if the invariant ever drifts.
                continue
            # Use the same endpoint that minted the token. ``paths``
            # is populated in lock-step with ``_session_tokens`` in
            # ``_session_token``; the default keeps shutdown safe if
            # a future code path ever caches a token without recording
            # its endpoint.
            revoke_path = paths.get(cache_key, SESSION_PATH_MODERN)
            try:
                resp = await client.request(
                    "DELETE",
                    revoke_path,
                    headers={_SESSION_HEADER: token},
                    extensions=extensions,
                )
                # Log non-2xx but don't raise — shutdown proceeds.
                if resp.status_code >= 400:
                    _log.warning(
                        "vsphere_session_revoke_non_2xx",
                        target=cache_key,
                        status_code=resp.status_code,
                        session_path=revoke_path,
                    )
            except (httpx.HTTPError, OSError) as exc:
                _log.warning(
                    "vsphere_session_revoke_failed",
                    target=cache_key,
                    error=f"{type(exc).__name__}: {exc}",
                    session_path=revoke_path,
                )
        await super().aclose()
