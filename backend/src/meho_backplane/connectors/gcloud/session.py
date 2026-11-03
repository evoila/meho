# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GCP ADC + impersonated-credential session management for GcloudConnector.

Auth model: the operator's ADC source credentials (``google.auth.default()``)
are used to impersonate a designated GCP service account via
``google.auth.impersonated_credentials.Credentials``. The impersonated bearer
token is cached per ``target.name`` and refreshed automatically on 401 or
near-expiry via ``creds.refresh()``.

SA-JSON-key refusal
-------------------

Org policy ``constraints/iam.disableServiceAccountKeyCreation`` is active
on the consumer's GCP organisation. The connector enforces this policy in
code: if the Vault ``secret_ref`` payload contains fields that look like a
service-account JSON key (a ``type`` field of ``service_account`` plus a
``private_key`` field, or any of the ``_SA_KEY_FIELDS`` JSON-key field
names — see :func:`_contains_sa_key_fields`), ``auth_headers()`` raises
:exc:`ValueError` with a clear message and no token is built.

The only accepted credential material in ``secret_ref`` is:
``{"gcp_impersonate_sa": "<sa-email>"}`` (the SA to impersonate), or an
empty record. A target without a ``secret_ref`` has nothing to read: the
loader answers ``{}`` and all auth comes from ADC + impersonation.

Target shape
------------

``GcloudTargetLike`` captures the fields the connector reads:

* ``name`` — per-target cache key.
* ``secret_ref`` — Vault path for the credential record (the connector
  validates the record does NOT carry SA-JSON-key fields). ``None`` means
  there is no record.
* ``auth_model`` — locked to ``AuthModel.IMPERSONATION`` or ``None``
  (pre-G0.3 sentinel).

Two settings have no column on the ``Target`` row. A registered target
carries them in ``extras``; a test stub may carry them as attributes:

* ``gcp_project`` — GCP project ID (``host`` is unused).
* ``gcp_impersonate_sa`` — SA email to impersonate.

:func:`gcp_project` and :func:`gcp_impersonate_sa` read them from either
place, so the same handler code runs for a database target (the normal
CLI / MCP path) and for a stub.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_vault_secret_data

__all__ = [
    "GcloudCredentialsLoader",
    "GcloudTargetLike",
    "gcp_impersonate_sa",
    "gcp_project",
    "load_credentials_from_vault",
]

# SA JSON key field names that identify a service-account key file.
# The connector refuses any secret_ref payload containing these fields.
_SA_KEY_FIELDS: frozenset[str] = frozenset(
    {
        "private_key",
        "private_key_id",
        "client_email",
        "client_id",
        "auth_uri",
        "token_uri",
        "auth_provider_x509_cert_url",
        "client_x509_cert_url",
    }
)


@runtime_checkable
class GcloudTargetLike(Protocol):
    """Minimum target shape :class:`GcloudConnector` reads.

    Structural Protocol — the concrete ``Target`` model (G0.3 #224) satisfies
    this unchanged. The project and the service account are not attributes
    here: a database target keeps them in ``extras`` and a test stub may
    keep them as attributes. Read them with :func:`gcp_project` and
    :func:`gcp_impersonate_sa`. ``target.host`` is intentionally unused
    (GCP REST APIs are reached via well-known public hostnames, not a
    per-target host field).

    ``auth_model`` is checked at the boundary: only ``IMPERSONATION`` or
    ``None`` (pre-G0.3 sentinel) are accepted. Any other value raises a
    clear :exc:`NotImplementedError`.

    ``id`` / ``tenant_id`` form the tenant-unique ``(tenant_id, id)``
    cache key (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    the token / credential / lock caches use, so two same-named targets in
    different tenants never share a cached token (#1642/#1672).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    secret_ref: str | None
    auth_model: str | None


def _target_setting(target: GcloudTargetLike, key: str, what: str) -> str:
    """Read *key* from the target: its attribute first, else ``extras[key]``."""
    value = getattr(target, key, None)
    if not value:
        extras = getattr(target, "extras", None)
        value = extras.get(key) if isinstance(extras, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Target {target.name!r} has no {what}. Set extras.{key} on the target "
            f"(for example in targets.yaml: {key}: <value>) and try again."
        )
    return value.strip()


def gcp_project(target: GcloudTargetLike) -> str:
    """The Google project ID of *target* (``extras.gcp_project`` on a registered target)."""
    return _target_setting(target, "gcp_project", "Google project ID (gcp_project)")


def gcp_impersonate_sa(target: GcloudTargetLike) -> str:
    """The service account *target* impersonates (``extras.gcp_impersonate_sa``)."""
    return _target_setting(
        target, "gcp_impersonate_sa", "service account to impersonate (gcp_impersonate_sa)"
    )


GcloudCredentialsLoader = Callable[[GcloudTargetLike, Operator], Awaitable[dict[str, Any]]]
"""Async callable resolving a target to its Vault-stored credential record.

The ``operator`` authenticates the Vault read under the operator's identity
(:func:`~meho_backplane.auth.vault.vault_client_for_operator`); it is NOT
forwarded to GCP — IMPERSONATION auth drives GCP calls through the
google-auth chain, not the operator's OIDC JWT.

The record is validated for SA-JSON-key fields before use. Expected shape for
a compliant record: ``{}`` (empty — all auth is ADC-derived) or at most
``{"gcp_impersonate_sa": "<sa-email>"}``. The SA the connector impersonates
comes from the target (:func:`gcp_impersonate_sa`); the record is loaded
only for the SA-JSON-key-refusal gate.
"""


def _contains_sa_key_fields(record: dict[str, Any]) -> bool:
    """Return ``True`` if *record* contains any SA JSON key field names."""
    return bool(_SA_KEY_FIELDS & record.keys())


async def load_credentials_from_vault(
    target: GcloudTargetLike,
    operator: Operator,
) -> dict[str, Any]:
    """Default credential loader — the target's credential record.

    A target without a ``secret_ref`` has no record to read; the loader
    answers ``{}`` without calling Vault, because the connector's auth
    comes from ADC + impersonation and the record exists only for the
    SA-JSON-key-refusal gate. A target with a ``secret_ref`` is read with
    the shared operator-context reader
    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`,
    so a system-initiated call without a person's token fails closed the
    same way it does for every other connector.
    """
    if not target.secret_ref or not target.secret_ref.strip():
        return {}
    return dict(await load_vault_secret_data(target, operator))
