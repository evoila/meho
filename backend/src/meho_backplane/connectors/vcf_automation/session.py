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

* Production deploys override the default loader at construction time
  once the operator-context per-target Vault credential read is wired
  for this connector (tracked under the open
  `Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_).
* Unit tests inject their own (mock) loader returning a pre-built dict.
* Integration tests against a recorded fixture or live VCFA appliance
  pass a loader that yields the appropriate service-account credentials.

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until the live read lands -- same posture as
the NSX / SDDC Manager / vSphere precedents.

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
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None
    fqdn: str | None
    domain: str | None
    provider_username: str | None
    provider_secret_ref: str | None


VcfAutomationCredentialsLoader = Callable[[VcfAutomationTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's
:meth:`VcfAutomationConnector._load_credentials` invokes the loader on
first session-establish per ``(target.name, secret_ref)`` and caches the
result for the connector instance lifetime. The same loader is invoked
a second time with ``target.provider_secret_ref`` when that override is
set, so production deploys can resolve both via a single Vault read
path. The return type is the looser ``dict[str, str]`` (not
:class:`SessionCredentials`) because Python :class:`Protocol` instances
aren't runtime-constructible without a matching class -- production
code returns a plain dict and the connector reads ``creds["username"]``
/ ``creds["password"]`` by key.
"""


async def load_credentials_from_vault(
    target: VcfAutomationTargetLike,
) -> dict[str, str]:
    """Default credential loader -- Vault read by ``target.secret_ref``.

    Deliberate stub: the operator-context per-target Vault credential
    read is not yet wired for the VCF Automation connector. Raising
    :exc:`NotImplementedError` here keeps the wiring shape stable -- a
    production caller without an override receives a clear error rather
    than a silent fallback or a hallucinated credential pair. The
    supported workaround is to inject a custom ``credentials_loader``
    on ``VcfAutomationConnector`` at construction time. The live read
    is tracked under the open Goal #214 (Connector parity).

    Once the read lands, this function becomes the live implementation
    that reads the ``vcf-automation/<target.name>`` Vault path (or
    ``target.secret_ref`` directly when set) and returns the parsed
    ``{"username": ..., "password": ...}`` dict.
    """
    raise NotImplementedError(
        "load_credentials_from_vault is a deliberate stub: the operator-context "
        "per-target Vault credential read is not yet wired for the VCF Automation "
        f"connector; target={target.name!r} secret_ref={target.secret_ref!r}. "
        "Workaround: inject a custom credentials_loader on VcfAutomationConnector. "
        "Tracked under open Goal #214 (Connector parity): "
        "https://github.com/evoila/meho/issues/214"
    )
