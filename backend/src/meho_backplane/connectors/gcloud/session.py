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
service-account JSON key (``"type": "service_account"`` + ``"private_key"``,
or any of the JSON-key field names), ``auth_headers()`` raises
:exc:`ValueError` with a clear message and no token is built.

The only accepted credential material in ``secret_ref`` is:
``{"gcp_impersonate_sa": "<sa-email>"}`` (the SA to impersonate).
``gcp_project`` is read from the target, not from the Vault secret.

Injecting ADC separately from the Vault secret lets unit tests mock the
ADC source without requiring a real GCP environment.

Target shape
------------

``GcloudTargetLike`` captures the fields the connector reads:

* ``name`` — per-target cache key.
* ``gcp_project`` — GCP project ID (replaces ``host`` which is unused).
* ``gcp_impersonate_sa`` — SA email to impersonate.
* ``secret_ref`` — Vault path for the credential record (the connector
  validates the record does NOT carry SA-JSON-key fields).
* ``auth_model`` — locked to ``AuthModel.IMPERSONATION`` or ``None``
  (pre-G0.3 sentinel).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator

__all__ = [
    "GcloudCredentialsLoader",
    "GcloudTargetLike",
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
    this unchanged. ``gcp_project`` + ``gcp_impersonate_sa`` drive the
    connector; ``target.host`` is intentionally unused (GCP REST APIs are
    reached via well-known public hostnames, not a per-target host field).

    ``auth_model`` is checked at the boundary: only ``IMPERSONATION`` or
    ``None`` (pre-G0.3 sentinel) are accepted. Any other value raises a
    clear :exc:`NotImplementedError`.
    """

    name: str
    gcp_project: str
    gcp_impersonate_sa: str
    secret_ref: str
    auth_model: str | None


GcloudCredentialsLoader = Callable[[GcloudTargetLike, Operator], Awaitable[dict[str, Any]]]
"""Async callable resolving a target to its Vault-stored credential record.

The ``operator`` authenticates the Vault read under the operator's identity
(``vault_client_for_operator(operator)`` once the live loader lands, G3.10);
it is NOT forwarded to GCP — IMPERSONATION auth drives GCP calls through the
google-auth chain, not the operator's OIDC JWT.

The record is validated for SA-JSON-key fields before use. Expected shape for
a compliant record: ``{}`` (empty — all auth is ADC-derived) or at most
``{"gcp_impersonate_sa": "<sa-email>"}`` if the SA is stored per-secret
rather than on the target row directly. In the v0.2 shape, the SA email comes
from ``target.gcp_impersonate_sa`` and the secret is loaded only for the
SA-JSON-key-refusal gate.
"""


def _contains_sa_key_fields(record: dict[str, Any]) -> bool:
    """Return ``True`` if *record* contains any SA JSON key field names."""
    return bool(_SA_KEY_FIELDS & record.keys())


async def load_credentials_from_vault(
    target: GcloudTargetLike,
    operator: Operator,
) -> dict[str, Any]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    The ``operator`` carries the identity the live loader will read Vault under
    (G3.10); it is part of the stable signature so callers do not change when
    the read is wired.

    Deliberate stub: the operator-context per-target Vault credential read is
    not yet wired for the GCloud connector. Raising :exc:`NotImplementedError`
    here keeps the wiring shape stable — a production caller without an
    override receives a clear error rather than a silent fallback or a
    hallucinated credential. The supported workaround is to inject a custom
    ``credentials_loader`` on ``GcloudConnector`` at construction time.
    Tracked under open Goal #214 (Connector parity).
    """
    raise NotImplementedError(
        "load_credentials_from_vault is a deliberate stub: the operator-context "
        "per-target Vault credential read is not yet wired for the GCloud "
        f"connector; target={target.name!r} secret_ref={target.secret_ref!r} "
        f"operator={operator.sub!r}. "
        "Workaround: inject a custom credentials_loader on GcloudConnector. "
        "Tracked under open Goal #214 (Connector parity): "
        "https://github.com/evoila/meho/issues/214"
    )
