# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared operator-context Vault KV-v2 basic-credentials reader.

The single reusable helper every REST connector loader uses to resolve a
target's ``secret_ref`` to vendor credentials, reading a KV-v2 secret
**under the operator's identity**. The vmware loader (G3.9-T3 #942) and
the REST fan-out (#G3.10) both consume :func:`load_basic_credentials`
rather than each re-deriving the hvac call — one implementation, one
tested error contract.

Why this exists as its own module
=================================

The exact read already exists inside the Vault connector op
(:func:`meho_backplane.connectors.vault.ops._vault_kv_read`,
``vault/ops.py:294``): ``async with vault_client_for_operator(operator)
as client: client.secrets.kv.v2.read_secret_version(...)`` then a
structural unwrap of ``data["data"]``. That handler is coupled to the
op-dispatch surface — it returns ``{"data", "version"}``, a scalar shape
the dispatcher's default reducer passes through verbatim, and is
registered as a typed op with a JSON schema. A connector *loader* needs something narrower: a plain
``dict[str, str]`` of named fields and an error contract distinct from
the dispatcher's ``connector_error`` branch. So this helper reuses the
lower-level primitive (:func:`vault_client_for_operator` +
``read_secret_version``), not the op handler.

The locked decision
===================

Per :doc:`docs/architecture/connector-auth.md` (Option A,
operator-context), the per-target Vault read forwards the operator's
validated Keycloak JWT to Vault's JWT/OIDC auth method via
:func:`~meho_backplane.auth.vault.vault_client_for_operator`. That gives
per-operator RBAC (templated ACL policy) and per-operator audit (Vault
attributes the read to the operator's Identity entity) through the single
``meho-mcp`` role.

System-initiated calls have no operator JWT
-------------------------------------------

Background/scheduled work runs as a synthesised system operator with
``raw_jwt=""``. Such a call cannot perform an operator-context read, so
:func:`load_basic_credentials` **fails closed** with a clear error rather
than silently falling back. A backplane-AppRole fallback is a later,
additive option to file only when a concrete need exists — not built
speculatively now.

The ``secret_ref`` shape
========================

``secret_ref`` is a Vault path **string** (``Target.secret_ref`` is
``str | None``; the DB column is ``Text``) — *not* an embedded dict (the
bind9 anti-shape; see the research doc §1). It addresses a KV-v2 secret
under the ``mount`` (default ``"secret"``, the dev-mode + consumer
convention; pass ``mount=`` for a non-default mount). The secret's data
dict must carry the requested ``fields`` (default ``("username",
"password")``).

No secret in logs
=================

The helper's structlog events carry only ``target`` / ``host`` / the
requested field *names* — never a credential value. The returned dict is
treated as ephemeral in-memory state (the Terraform discipline from the
research doc §5): it never enters a log event, an ``OperationResult``, or
any durable artifact.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import vault_client_for_operator

__all__ = [
    "DEFAULT_BASIC_CREDENTIAL_FIELDS",
    "DEFAULT_KV_MOUNT",
    "BasicCredentialsTargetLike",
    "VaultCredentialsReadError",
    "load_basic_credentials",
    "load_vault_secret_data",
]

#: KV-v2 mount the consumer convention addresses secrets under. Dev mode
#: mounts ``secret/`` as v2 by default and the consumer ``targets.yaml``
#: ``secret_ref`` paths live under it, so the helper defaults here and a
#: caller only passes ``mount=`` for a non-default mount. Mirrors
#: ``vault/ops.py``'s ``_DEFAULT_KV_MOUNT``.
DEFAULT_KV_MOUNT: str = "secret"

#: The basic-credentials field names a vendor session-establish call
#: needs. Kept as a module constant so connector loaders and tests share
#: one source of truth.
DEFAULT_BASIC_CREDENTIAL_FIELDS: tuple[str, ...] = ("username", "password")


@runtime_checkable
class BasicCredentialsTargetLike(Protocol):
    """Minimum target shape :func:`load_basic_credentials` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224) satisfies this unchanged,
    as does any per-connector duck-typed target stub.

    Fields:

    * ``name`` — names the target in error messages and log events.
    * ``host`` — logged alongside ``name`` for operator attribution
      (never a credential value).
    * ``secret_ref`` — the Vault KV-v2 path string the helper reads.
      ``None`` is rejected with a clear error (an unconfigured target).
    """

    name: str
    host: str
    secret_ref: str | None


class VaultCredentialsReadError(Exception):
    """Read-phase failure resolving a target's KV-v2 basic credentials.

    Raised when the operator-context Vault *login* succeeded (or was
    refused before it started) but the *read* could not produce the
    requested credential fields:

    * empty ``operator.raw_jwt`` — the fail-closed system-call carve-out
      (no authenticated operator to read under);
    * ``target.secret_ref`` is unset — the target is unconfigured;
    * the KV-v2 payload is missing a requested field — a misconfigured
      Vault secret.

    This is deliberately distinct from
    :class:`~meho_backplane.auth.vault.VaultClientError` and its
    subclasses, which signal *login-phase* failures (Vault unreachable,
    role denied). The two-phase split mirrors ``vault/ops.py``'s
    ``VaultClientError``-vs-other contract: a caller that needs to render
    an operator-actionable detail string can catch
    :class:`VaultClientError` for "login phase" and
    :class:`VaultCredentialsReadError` for "read phase". A read of a
    missing field never surfaces as a bare ``KeyError``.

    The message names the target and (where relevant) the missing field.
    It **never** echoes a credential value.
    """


def _structural_unwrap(payload: object, *, target_name: str) -> dict[str, object]:
    """Unwrap hvac's ``read_secret_version`` payload to the secret data dict.

    KV-v2's GET on ``/{mount}/data/{path}`` returns
    ``{"data": {"data": {<secret kv>}, "metadata": {...}}}``. The secret
    content is the *nested* ``data["data"]`` — the same double-unwrap
    ``vault/ops.py:308`` performs. A malformed payload (missing either
    ``data`` level) raises :class:`VaultCredentialsReadError` naming the
    target rather than a bare ``KeyError`` deep inside hvac's response.
    """
    outer = payload.get("data") if isinstance(payload, dict) else None
    secret_data = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(secret_data, dict):
        raise VaultCredentialsReadError(
            f"vault KV-v2 read for target {target_name!r} returned a malformed "
            "payload: expected a nested 'data.data' object holding the secret fields"
        )
    return secret_data


def _is_api_path_shaped(secret_ref: str) -> bool:
    """Return ``True`` when *secret_ref* carries a KV-v2 API-path signature.

    ``secret_ref`` must be the *logical* KV-v2 path relative to the mount —
    hvac's ``read_secret_version`` builds the wire URL as
    ``/{mount_point}/data/{path}``, inserting the ``/data/`` segment itself
    (verified against hvac 2.4.0). A value that already embeds the mount or
    the ``/data/`` API segment (``secret/data/foo``, ``kv/data/foo``,
    ``data/foo``) therefore double-resolves to ``secret/data/<mount>/data/foo``
    and 404s.

    The predicate is **specific** to that signature so it never trips on a
    logical segment legitimately named ``data`` deeper in the path: it
    matches only when the **first** path segment is ``data`` (``data/foo``)
    or the **second** is ``data`` (``<mount>/data/foo`` — covers
    ``secret/data/…`` and ``kv/data/…``). A path like
    ``targets/data-center-01/host`` (second segment ``data-center-01``) or
    ``vsphere/vcenter-a`` (second segment ``vcenter-a``) is accepted.
    """
    segments = secret_ref.split("/")
    if segments[0] == "data":
        return True
    return len(segments) >= 2 and segments[1] == "data"


def _resolve_secret_ref(target: BasicCredentialsTargetLike, operator: Operator) -> str:
    """Validate the pre-read preconditions and return the KV-v2 path.

    Three fail-closed guards, all raising :class:`VaultCredentialsReadError`
    *before* Vault is touched:

    * empty ``operator.raw_jwt`` — an operator-context read requires an
      authenticated operator. System-initiated calls (topology scheduler,
      readiness probe) carry ``raw_jwt=""`` and must error here rather
      than silently falling back to a backplane identity (the decision's
      system-call carve-out).
    * unset ``target.secret_ref`` — the target is unconfigured.
    * API-path-shaped ``secret_ref`` — a value embedding the mount or the
      ``/data/`` API segment (``secret/data/…``, ``kv/data/…``,
      ``data/…``). hvac inserts ``/data/`` itself, so such a value
      silently double-resolves to a 404; we reject it with an actionable
      error rather than guessing operator intent (no auto-stripping).

    Returns the stripped ``secret_ref`` path so trailing whitespace never
    slips into the hvac call.
    """
    if not operator.raw_jwt:
        raise VaultCredentialsReadError(
            "operator-context credential read requires an authenticated operator; "
            f"target={target.name!r} has no operator JWT (system-initiated calls "
            "cannot read per-target vendor credentials)"
        )
    if not target.secret_ref:
        raise VaultCredentialsReadError(
            f"target {target.name!r} has no secret_ref configured; cannot read "
            "its basic credentials from Vault"
        )
    secret_ref = target.secret_ref.strip()
    if _is_api_path_shaped(secret_ref):
        raise VaultCredentialsReadError(
            f"target {target.name!r} has a KV-v2 API-path-shaped secret_ref "
            f"{secret_ref!r}; secret_ref must be the logical KV-v2 path relative "
            "to the mount (e.g. 'targets/<id>'). Drop the 'secret/data/' / "
            "'kv/data/' / 'data/' prefix — Vault adds the '/data/' segment for "
            "KV-v2 reads, so a prefixed value double-resolves to a 404."
        )
    return secret_ref


def _extract_fields(
    secret_data: dict[str, object],
    fields: tuple[str, ...],
    *,
    target_name: str,
    secret_ref: str,
) -> dict[str, str]:
    """Pull *fields* out of the secret data dict as a flat ``{field: str}``.

    A missing field raises :class:`VaultCredentialsReadError` naming the
    target + field + ``secret_ref`` — never a bare ``KeyError``. Present
    values are coerced to ``str`` so a numeric secret field round-trips as
    the string a vendor Basic-auth header expects.
    """
    credentials: dict[str, str] = {}
    for field in fields:
        if field not in secret_data:
            raise VaultCredentialsReadError(
                f"vault secret for target {target_name!r} (secret_ref={secret_ref!r}) "
                f"is missing required field {field!r}"
            )
        credentials[field] = str(secret_data[field])
    return credentials


async def load_basic_credentials(
    target: BasicCredentialsTargetLike,
    operator: Operator,
    *,
    fields: tuple[str, ...] = DEFAULT_BASIC_CREDENTIAL_FIELDS,
    mount: str = DEFAULT_KV_MOUNT,
) -> dict[str, str]:
    """Read *target*'s basic credentials from Vault under *operator*'s identity.

    Opens :func:`~meho_backplane.auth.vault.vault_client_for_operator`
    (JWT/OIDC login forwarding ``operator.raw_jwt``), reads
    ``target.secret_ref`` as a KV-v2 secret off the event loop
    (``asyncio.to_thread`` — hvac is synchronous, matching
    ``vault/ops.py:295``), structurally unwraps the nested
    ``data["data"]``, and returns the requested *fields* as a flat
    ``{field: value}`` dict.

    Parameters
    ----------
    target
        The target whose ``secret_ref`` (a KV-v2 path string) holds the
        vendor credentials.
    operator
        The request-scoped operator. ``operator.raw_jwt`` is forwarded to
        Vault's JWT/OIDC auth method — the read happens under the
        operator's Vault Identity entity, giving per-operator RBAC and
        audit (the locked Option A decision).
    fields
        The credential field names to extract from the KV-v2 secret.
        Defaults to ``("username", "password")``. Every named field must
        be present in the secret or :class:`VaultCredentialsReadError` is
        raised.
    mount
        The KV-v2 mount point. Defaults to ``"secret"`` (the consumer
        convention); pass a different value only for a non-default mount.

    Returns
    -------
    dict[str, str]
        ``{field: value}`` for every name in *fields*. Values are
        coerced to ``str`` so a numeric secret field round-trips as the
        string a vendor Basic-auth header expects.

    Raises
    ------
    VaultCredentialsReadError
        Read-phase failure: ``operator.raw_jwt`` is empty (the
        fail-closed system-call carve-out), ``target.secret_ref`` is
        unset, the KV-v2 payload is malformed, or a requested field is
        missing. Never a bare ``KeyError``.
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure raised by
        :func:`vault_client_for_operator` —
        :class:`~meho_backplane.auth.vault.VaultUnreachableError`
        (network/TLS) or
        :class:`~meho_backplane.auth.vault.VaultRoleDeniedError` (Vault
        rejected the JWT for the role). Propagated verbatim so callers
        can distinguish login-phase from read-phase failure.
    """
    # Fail-closed precondition guards (empty JWT / unset secret_ref) run
    # before Vault is touched; returns the stripped KV-v2 path.
    path = _resolve_secret_ref(target, operator)

    async with vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=path,
            mount_point=mount,
            raise_on_deleted_version=False,
        )

    secret_data = _structural_unwrap(payload, target_name=target.name)
    credentials = _extract_fields(secret_data, fields, target_name=target.name, secret_ref=path)

    # Log only non-secret attribution: target / host / the field *names*
    # requested — never a credential value. The returned dict is
    # ephemeral in-memory state and must not enter any log event,
    # OperationResult, or durable artifact.
    #
    # Resolve the logger per-call rather than from a module-level proxy:
    # the production `configure_logging` sets `cache_logger_on_first_use=
    # True`, so a cached module-level BoundLogger pins the processor list
    # it was built with and `structlog.testing.capture_logs` cannot reach
    # it (the no-secret-in-logs test relies on capture). Same precedent +
    # rationale as `meho_backplane.auth.rbac.require_role`.
    structlog.get_logger(__name__).info(
        "vault_basic_credentials_loaded",
        target=target.name,
        host=target.host,
        fields=list(fields),
    )
    return credentials


async def load_vault_secret_data(
    target: BasicCredentialsTargetLike,
    operator: Operator,
    *,
    mount: str = DEFAULT_KV_MOUNT,
) -> dict[str, object]:
    """Read *target*'s KV-v2 secret payload and return the raw data dict.

    Same operator-context Vault read, same fail-closed precondition
    guards, same structural unwrap as :func:`load_basic_credentials`,
    but **without** the named-field extraction — the caller is
    responsible for inspecting the returned dict and surfacing its own
    structured error when the payload shape is wrong. Used when the
    connector picks an upstream credential protocol by inspecting
    which fields the operator stored (e.g. the gh-rest connector's
    App-vs-PAT discriminator).

    The structured-log event carries only ``target`` / ``host`` and the
    **set of field names** present — never a credential value. The
    returned dict is ephemeral in-memory state and must not enter any
    log event, :class:`OperationResult`, or durable artifact.

    Raises the same two-phase error contract as
    :func:`load_basic_credentials`: login-phase failures propagate as
    :class:`~meho_backplane.auth.vault.VaultClientError` subclasses;
    read-phase precondition / unwrap failures raise
    :class:`VaultCredentialsReadError`.
    """
    path = _resolve_secret_ref(target, operator)

    async with vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=path,
            mount_point=mount,
            raise_on_deleted_version=False,
        )

    secret_data = _structural_unwrap(payload, target_name=target.name)

    # Log only the field-name set (no values). Sorted for a stable log
    # shape that diff-friendly observability tooling can pattern-match
    # without re-ordering noise.
    structlog.get_logger(__name__).info(
        "vault_secret_data_loaded",
        target=target.name,
        host=target.host,
        fields=sorted(secret_data.keys()),
    )
    return secret_data
