# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading for the vcf-automation connector.

The hand-rolled
:class:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector`
spans two auth planes on the same appliance (vCloud-Director-derived
*provider* plane + Aria-IaaS-derived *tenant* plane); each plane has its
own login flow but both ultimately resolve to a single ``{"username": ...,
"password": ...}`` credential pair sourced from the target's Vault secret.
A single :class:`VcfAutomationCredentialsLoader` is therefore sufficient
-- the connector applies the same loaded pair against whichever plane is
being established. When the provider-plane account differs from the SSO
account (typical: ``admin@System`` for provider vs ``svc-meho`` for
tenant) the operator supplies ``target.provider_username`` /
``target.provider_secret_ref`` and the connector reads them at
``auth_headers`` time; the loader interface stays single-pair.

The credential fetch (Vault path -> ``{"username": ..., "password": ...}``
dict) is split out behind a narrow :class:`VcfAutomationCredentialsLoader`
callable so:

* Production deploys use the default loader,
  :func:`load_credentials_from_vault`, which performs the **live**
  operator-context KV-v2 read via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper (G3.9-T2 #941). This is the rubric **State 2** wiring
  (``shared_service_account`` only) per
  `Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_.
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests against a recorded fixture or live VCFA appliance
  pass a loader that yields the appropriate service-account credentials.

The loader's signature carries the request-scoped
:class:`~meho_backplane.auth.operator.Operator` so the default
implementation can forward ``operator.raw_jwt`` to Vault's JWT/OIDC
auth method (the locked Option A decision in
:doc:`docs/architecture/connector-auth.md`). Injected test loaders
receive the same ``(target, operator)`` pair and are free to ignore
``operator`` when the test does not need per-operator attribution.

The :class:`VcfAutomationTargetLike` Protocol captures the minimum
target shape the connector reads: ``name`` (per-target token cache key),
``host`` (the addressable host -- may be an IP), ``port``,
``secret_ref`` (the Vault path the default loader resolves),
``auth_model`` (boundary-checked), ``fqdn`` (the vhost the appliance
expects in the ``Host:`` header -- load-bearing when ``host`` is an
IP because VCFA returns 404 with empty body before the application
sees the request otherwise), ``domain`` (optional org / SSO realm
forwarded on the tenant login body), ``provider_username`` /
``provider_secret_ref`` (optional provider-plane credential overrides
for the ``admin@System`` vs ``svc-meho`` split documented in the
consumer wrapper). The concrete ``Target`` model in
:mod:`meho_backplane.targets` (G0.3 #224 -- closed) satisfies this
Protocol structurally; the four optional fields default to ``None``
so a target that doesn't carry them is still acceptable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "SessionCredentials",
    "VcfAutomationCredentialsLoader",
    "VcfAutomationTargetLike",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`VcfAutomationCredentialsLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values feed both plane logins -- the
    provider plane sends them as HTTP Basic, the tenant plane as a
    JSON body field pair -- modulo the optional provider-plane
    override on ``VcfAutomationTargetLike``.
    """

    username: str
    password: str


@runtime_checkable
class VcfAutomationTargetLike(Protocol):
    """Minimum target shape :class:`VcfAutomationConnector` reads.

    Structural Protocol -- the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224 -- closed) satisfies this
    Protocol unchanged once the four optional fields land in the model.
    ``auth_model`` is checked at the boundary so a target tagged
    ``per_user`` or ``impersonation`` raises a clear error rather than
    silently authenticating as the shared service account.

    ``fqdn`` is the vhost the appliance expects in the ``Host:`` header.
    VCFA 9.x enforces strict ``Host:`` matching; when ``host`` is an IP
    and ``fqdn`` is unset, every path returns 404 with empty body before
    the application sees the request (the consumer wrapper's documented
    failure mode -- see ``scripts/vcf-automation.sh`` § Vhost routing).
    The connector raises a clear configuration error at session-establish
    time rather than emitting a confusing post-login 404 storm.

    ``domain`` is the org name (provider plane) or SSO realm / org
    (tenant plane). Forwarded verbatim on the tenant login JSON body
    as the optional ``domain`` field; ignored on the provider plane
    when ``provider_username`` is set (because that field already
    carries the ``<user>@System`` form).

    ``provider_username`` is the verbatim Basic-auth user the provider
    plane needs (typically ``admin@System``); when unset, the connector
    falls back to ``f"{creds['username']}@{domain or 'System'}"``.
    ``provider_secret_ref`` is an optional Vault path the loader can
    resolve to a *different* credential pair when the provider-plane
    password differs from the SSO secret -- the connector reads the
    provider plane via the override loader when set, the default loader
    otherwise.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    the per-plane token caches use, so two same-named targets in different
    tenants never share a cached token (#1642/#1672).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    fqdn: str | None
    domain: str | None
    provider_username: str | None
    provider_secret_ref: str | None


VcfAutomationCredentialsLoader = Callable[
    [VcfAutomationTargetLike, Operator], Awaitable[dict[str, str]]
]
"""Async callable resolving a ``(target, operator)`` pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector's
:meth:`VcfAutomationConnector._provider_session_token` /
:meth:`VcfAutomationConnector._tenant_session_token` invoke the loader
on first session-establish per ``(target.name, secret_ref)``; the
provider plane invokes it a second time with the override
:attr:`VcfAutomationTargetLike.provider_secret_ref` when set, so a
production deploy can resolve both via a single read path. The return
type is the looser ``dict[str, str]`` (not :class:`SessionCredentials`)
because Python :class:`Protocol` instances aren't runtime-constructible
without a matching class -- production code returns a plain dict and
the connector reads ``creds["username"]`` / ``creds["password"]`` by
key.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader (:func:`load_credentials_from_vault`) can read the per-target
secret under the operator's identity via
``vault_client_for_operator(operator)`` -- the locked decision in
:doc:`docs/architecture/connector-auth.md`.
"""


async def load_credentials_from_vault(
    target: VcfAutomationTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default credential loader -- live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (``operator.raw_jwt`` is forwarded to Vault's JWT/OIDC
    auth method) and returns the ``{"username": ..., "password": ...}``
    pair the connector feeds into both planes' login flows. Delegates
    to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2 #941) so the read, the no-secret-in-logs discipline,
    and the two-phase error contract are defined once for every REST
    connector -- this loader is the thin vcf-automation entry point.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      -- read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``username``/``password`` field).
      Never a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and its
      subclasses) -- login-phase failure (Vault unreachable, role
      denied). Propagated verbatim so callers can distinguish login
      from read.

    The connector invokes this loader once per plane on first
    session-establish; the provider plane re-invokes it with the
    override ``target.provider_secret_ref`` when that field is set so
    distinct provider-plane credentials (``admin@System`` vs the
    SSO/tenant account) resolve from a different Vault path.

    A custom :class:`VcfAutomationCredentialsLoader` can still be
    injected via ``credentials_loader`` on ``VcfAutomationConnector``
    (tests do exactly that); this default is what production targets
    at rubric State 2 (``shared_service_account``) use.
    """
    return await load_basic_credentials(target, operator)
