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
``raw_jwt=""``. Such a call cannot perform an operator-context *Vault*
read, so the Vault backend **fails closed** with a clear error rather than
silently falling back. A backplane-AppRole fallback is a later, additive
option to file only when a concrete need exists — not built speculatively
now.

That guard is per-backend, not shared (#2642). A store MEHO can read under
a *deployment* identity (GSM SA-direct ADC) is able to serve a
system-initiated call, and the check-runner can be given a Keycloak
service-principal token of its own
(:mod:`meho_backplane.auth.runner_identity`) so background dispatch carries
a real operator-context JWT. Hoisting the empty-JWT check into the shared
dispatch would fail both of those closed before the backend is even
resolved, so it lives on the backend whose auth model requires it.

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
from meho_backplane.connectors._shared.credential_backend import (
    CredentialsReadError,
    register_credential_backend,
    resolve_credential_backend,
    split_credential_ref,
)
from meho_backplane.settings import get_settings

__all__ = [
    "DEFAULT_BASIC_CREDENTIAL_FIELDS",
    "DEFAULT_KV_MOUNT",
    "BasicCredentialsTargetLike",
    "CredentialsReadError",
    "VaultCredentialBackend",
    "VaultCredentialsReadError",
    "load_basic_credentials",
    "load_vault_secret_data",
    "strip_credential_value",
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


def strip_credential_value(value: object) -> str:
    """Coerce a Vault secret field to the credential string sent upstream.

    Coerces to ``str`` (a numeric secret field round-trips as the string a
    vendor expects) and strips surrounding whitespace -- above all a trailing
    newline, the single most common secret-storage artifact: ``echo`` without
    ``-n``, ``jq -r``, a text editor's final newline, ``vault kv put k=@file``
    on a file ending in ``\\n``, a ``k=-`` heredoc. A connector forwards the
    field **verbatim** in a Basic-auth header, a Bearer token, or a
    token-request body, so a stray ``\\n`` turns a valid secret into an
    upstream 401/``unauthorized_client`` that reads like a permissions, realm,
    or grant-config problem -- a multi-hour chase for a one-byte artifact. No
    vendor credential legitimately carries leading or trailing whitespace, so
    stripping is always safe and is applied to every credential field every
    connector loads from Vault. Internal whitespace is preserved -- only the
    surrounding artifact is removed.
    """
    return str(value).strip()


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


class VaultCredentialsReadError(CredentialsReadError):
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

    Subclasses the backend-neutral
    :class:`~meho_backplane.connectors._shared.credential_backend.CredentialsReadError`
    (#2642) so a caller that means "the credential could not be read" catches
    the base and still sees a GSM-backend failure, while everything that
    already catches this class keeps working unchanged.
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


def _require_secret_ref(target: BasicCredentialsTargetLike) -> str:
    """Return *target*'s stripped ``secret_ref`` or raise if unconfigured.

    The one **backend-agnostic** precondition — every credential backend
    needs a ref to resolve — so it runs in the shared loader before the
    scheme is split and the backend is dispatched. A target with
    ``secret_ref=None`` is unconfigured → :class:`VaultCredentialsReadError`.
    The stripped value is returned so trailing whitespace never slips into
    the scheme split or the downstream store read.

    Backend-specific guards (the operator-JWT carve-out, the Vault KV-v2
    API-path shape check) live in the resolved backend, not here — a
    non-Vault backend does not share them.
    """
    if not target.secret_ref:
        raise VaultCredentialsReadError(
            f"target {target.name!r} has no secret_ref configured; cannot resolve its credentials"
        )
    return target.secret_ref.strip()


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
    values run through :func:`strip_credential_value` (coerced to ``str``
    and surrounding-whitespace-stripped) so a numeric secret field
    round-trips as a string and a trailing newline — the most common
    secret-storage artifact — never reaches a vendor Basic-auth header or
    token-request body as a verbatim ``\n``.
    """
    credentials: dict[str, str] = {}
    for field in fields:
        if field not in secret_data:
            raise VaultCredentialsReadError(
                f"vault secret for target {target_name!r} (secret_ref={secret_ref!r}) "
                f"is missing required field {field!r}"
            )
        credentials[field] = strip_credential_value(secret_data[field])
    return credentials


class VaultCredentialBackend:
    """The Vault KV-v2 credential backend — today's behaviour, unchanged.

    Registered under kind ``"vault"`` (and the schemeless default), so a
    schemeless ``targets/<id>`` ref and an explicit ``vault:targets/<id>``
    ref resolve through exactly this read. It owns the Vault-specific
    fail-closed guards:

    * API-path-shaped ``secret_ref`` — a value embedding the mount or the
      ``/data/`` API segment (``secret/data/…``, ``kv/data/…``,
      ``data/…``). hvac inserts ``/data/`` itself, so such a value
      silently double-resolves to a 404; reject it with an actionable
      error rather than guessing operator intent (no auto-stripping).
      This is a KV-v2 wire-format concern, so it stays on the Vault path
      only (AC #4).

    * empty ``operator.raw_jwt`` — the fail-closed system-call carve-out.
      Vault reads run **only** under an operator identity here (the locked
      Option A decision), so a system-initiated call has nothing to log in
      with and must error rather than silently fall back to a backplane
      identity. This precondition used to live in the shared loader
      (:func:`_resolve_and_load`), which made it fire for *every* backend —
      including one that has a deployment identity of its own and could
      have served the call (#2642). It now lives here, on the backend whose
      auth model actually requires it. The message and exception class are
      unchanged, and it still fires before any Vault network round-trip.
    """

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str = DEFAULT_KV_MOUNT,
    ) -> dict[str, object]:
        """Read *secret_ref* as a KV-v2 secret under *operator*'s identity.

        Applies the operator-context and API-path guards, opens
        :func:`~meho_backplane.auth.vault.vault_client_for_operator`
        (JWT/OIDC login forwarding ``operator.raw_jwt``), reads the secret
        off the event loop (``asyncio.to_thread`` — hvac is synchronous),
        and structurally unwraps the nested ``data["data"]`` to the flat
        secret-field dict.
        """
        # System-initiated calls (topology scheduler, readiness probe, the
        # runbook verify dispatch's synthetic operator, and — unless a
        # check-runner principal is configured, #2642 — the sensor runner)
        # carry ``raw_jwt=""``. Vault's only auth model here is the
        # operator's JWT, so fail closed before any network round-trip
        # rather than reaching for a backplane identity.
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target_name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        if _is_api_path_shaped(secret_ref):
            raise VaultCredentialsReadError(
                f"target {target_name!r} has a KV-v2 API-path-shaped secret_ref "
                f"{secret_ref!r}; secret_ref must be the logical KV-v2 path relative "
                "to the mount (e.g. 'targets/<id>'). Drop the 'secret/data/' / "
                "'kv/data/' / 'data/' prefix — Vault adds the '/data/' segment for "
                "KV-v2 reads, so a prefixed value double-resolves to a 404."
            )

        async with vault_client_for_operator(operator) as client:
            payload = await asyncio.to_thread(
                client.secrets.kv.v2.read_secret_version,
                path=secret_ref,
                mount_point=mount,
                raise_on_deleted_version=False,
            )

        return _structural_unwrap(payload, target_name=target_name)


#: The Vault backend is stateless, so a single shared instance serves every
#: read. Registered at import time under ``"vault"``; ``vault_creds`` is
#: imported by every connector session module, so the kind is always
#: present before any credential resolution runs.
register_credential_backend("vault", VaultCredentialBackend())


async def _resolve_and_load(
    target: BasicCredentialsTargetLike,
    operator: Operator,
    *,
    mount: str,
) -> tuple[str, dict[str, object]]:
    """Resolve *target*'s ``secret_ref`` to its secret-field dict via dispatch.

    The shared backend-agnostic path both public loaders funnel through:

    1. Require a configured ``secret_ref``.
    2. Split the scheme — schemeless refs resolve through the deployment
       default (``config.credentialBackend`` / ``CREDENTIAL_BACKEND``,
       default ``vault``); an explicit ``<kind>:`` prefix selects that
       backend.
    3. Dispatch to the resolved backend (:class:`UnknownCredentialBackendError`
       on an unregistered kind) to read the store secret.

    The operator-context precondition is **not** here (#2642). It used to
    run first, fail-closing every system-initiated call before the scheme
    was even split — which is right for Vault (its only auth model is the
    operator's JWT) and wrong for a store MEHO can read under a deployment
    identity: on a ``credentialBackend=gsm`` install it blocked the SA-direct
    read that would have worked, so no Sensor could ever evaluate. Each
    backend now enforces its own precondition inside ``load_secret_data``,
    with its own (backend-named) error class.

    Returns ``(store_ref, secret_data)`` — the scheme-stripped store ref
    is handed back so the caller can name it in a missing-field error.
    """
    ref = _require_secret_ref(target)
    kind, store_ref = split_credential_ref(ref, default_backend=get_settings().credential_backend)
    backend = resolve_credential_backend(kind)
    secret_data = await backend.load_secret_data(
        store_ref, operator, target_name=target.name, mount=mount
    )
    return store_ref, secret_data


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
        ``{field: value}`` for every name in *fields*. Values are coerced
        to ``str`` and surrounding-whitespace-stripped via
        :func:`strip_credential_value` so a numeric secret field
        round-trips as a string and a trailing newline never reaches a
        vendor Basic-auth header verbatim.

    Raises
    ------
    CredentialsReadError
        Read-phase failure. ``target.secret_ref`` is unset or a requested
        field is missing → :class:`VaultCredentialsReadError` (the shared
        loader's own errors keep the historical class). Otherwise the
        resolved backend's subclass: the Vault backend raises
        :class:`VaultCredentialsReadError` when ``operator.raw_jwt`` is
        empty (the fail-closed system-call carve-out) or the KV-v2 payload
        is malformed; a ``gsm:`` ref raises ``GcpSecretManagerReadError``.
        Never a bare ``KeyError``.
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure raised by
        :func:`vault_client_for_operator` —
        :class:`~meho_backplane.auth.vault.VaultUnreachableError`
        (network/TLS) or
        :class:`~meho_backplane.auth.vault.VaultRoleDeniedError` (Vault
        rejected the JWT for the role). Propagated verbatim so callers
        can distinguish login-phase from read-phase failure.
    """
    # Scheme-dispatched resolution: the unset-secret_ref guard runs before
    # any backend is touched, then the ref's scheme (schemeless/``vault:``
    # → the Vault KV-v2 read) selects the backend. ``store_ref`` is the
    # scheme-stripped ref, named in a missing-field error.
    store_ref, secret_data = await _resolve_and_load(target, operator, mount=mount)
    credentials = _extract_fields(
        secret_data, fields, target_name=target.name, secret_ref=store_ref
    )

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
    _store_ref, secret_data = await _resolve_and_load(target, operator, mount=mount)

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
