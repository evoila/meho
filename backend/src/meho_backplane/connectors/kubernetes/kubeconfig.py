# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Kubeconfig loading for the Kubernetes connector.

The connector reads its target through a narrow
:class:`KubernetesTargetLike` Protocol and resolves a kubeconfig via an
injectable ``kubeconfig_loader`` callable. A concrete target model that
exposes ``name``/``host``/``port``/``secret_ref`` satisfies the Protocol
structurally with no edits here.

The default loader, :func:`load_kubeconfig_from_vault`, performs the
**live** operator-context KV-v2 read: it forwards the operator's
validated Keycloak JWT to Vault and reads ``target.secret_ref`` for the
``kubeconfig`` field, parses the YAML into the dict shape
``kubernetes_asyncio.config.new_client_from_config_dict`` accepts, and
returns it. This is the rubric **State 2** wiring (`shared_service_account`
only) per `Goal #214 (Connector parity)
<https://github.com/evoila/meho/issues/214>`_. A custom
``kubeconfig_loader`` can still be injected on ``KubernetesConnector``
at construction time; unit and integration tests inject their own (mock)
loader the same way.

Why kubeconfig isn't shaped like the basic-credentials helper
=============================================================

The shared :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
helper returns a flat ``{field: str}`` dict — the right shape for the
HTTP-basic ``{username, password}`` pairs every REST connector consumes.
A kubeconfig is structurally different: it's a YAML document with nested
``clusters`` / ``contexts`` / ``users`` arrays under a single Vault field
``kubeconfig`` (decision #8 convention). So this loader reuses the lower-
level Vault primitive (:func:`~meho_backplane.auth.vault.vault_client_for_operator`
+ ``read_secret_version``) directly and does its own YAML parse via
:func:`parse_kubeconfig_yaml`, mirroring the shape of the shared helper
but with a kubeconfig-shaped return type and a kubeconfig-specific error
contract (a malformed YAML field is a value error, not a missing
credential field).

No kubeconfig content in logs
=============================

The loader's structlog event carries only ``target`` / ``host`` / the
``secret_ref`` path / the field *name* — never the kubeconfig YAML, the
server URL, the client certificate / token, or any other content from
the parsed dict. The returned dict is treated as ephemeral in-memory
state: it never enters a log event, an ``OperationResult``, or any
durable artifact.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import structlog
import yaml

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import vault_client_for_operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

__all__ = [
    "DEFAULT_KUBECONFIG_FIELD",
    "DEFAULT_KUBECONFIG_KV_MOUNT",
    "KubeconfigLoader",
    "KubernetesTargetLike",
    "load_kubeconfig_from_vault",
    "parse_kubeconfig_yaml",
]


#: KV-v2 mount the consumer convention addresses kubeconfig secrets
#: under. Mirrors :data:`~meho_backplane.connectors._shared.vault_creds.DEFAULT_KV_MOUNT`;
#: dev mode mounts ``secret/`` as v2 by default and the consumer
#: ``targets.yaml`` ``secret_ref`` paths live under it.
DEFAULT_KUBECONFIG_KV_MOUNT: str = "secret"

#: KV-v2 field name the kubeconfig YAML lives under (decision #8). The
#: operator stores the kubeconfig at ``<secret_ref>`` with this single
#: field; the loader reads exactly this key.
DEFAULT_KUBECONFIG_FIELD: str = "kubeconfig"


@runtime_checkable
class KubernetesTargetLike(Protocol):
    """Minimum target shape :class:`KubernetesConnector` reads.

    Structural Protocol — any concrete ``Target`` model in
    :mod:`meho_backplane.targets` that exposes these attributes
    satisfies it without code changes here. ``secret_ref`` is the Vault
    path the operator-context Vault read resolves to a kubeconfig YAML
    string under the ``kubeconfig`` field (consumer's ``targets.yaml``
    convention, locked in decision #8).
    """

    name: str
    host: str
    port: int | None
    secret_ref: str


KubeconfigLoader = Callable[[KubernetesTargetLike, Operator], Awaitable[dict[str, Any]]]
"""Async callable resolving a (target, operator) pair to a kubeconfig dict.

Injection point for the connector's auth flow. Tests pass a mock
returning a pre-built dict; production passes
:func:`load_kubeconfig_from_vault`. The dict shape matches what
``kubernetes_asyncio.config.new_client_from_config_dict`` accepts —
top-level keys ``apiVersion`` / ``clusters`` / ``contexts`` /
``current-context`` / ``users``.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` so the live loader
reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the locked decision in
:doc:`docs/architecture/connector-auth.md`. An injected test loader
receives the same ``(target, operator)`` pair so the wiring is
exercised by both the default and the injected path.
"""


def parse_kubeconfig_yaml(kubeconfig_text: str) -> dict[str, Any]:
    """Parse a kubeconfig YAML string into the dict shape k_a consumes.

    Wraps :func:`yaml.safe_load` so callers get a single failure path
    when a Vault secret's ``kubeconfig`` field is malformed. Both
    failure shapes — syntactically invalid YAML (parser/scanner
    errors) and structurally wrong YAML (scalar, empty, list) — raise
    :exc:`ValueError`, never the underlying :exc:`yaml.YAMLError`
    subclass, so callers don't need to import ``yaml`` just to catch
    parse failures.
    """
    try:
        parsed = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"kubeconfig YAML failed to parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"kubeconfig YAML must parse to a mapping, got {type(parsed).__name__}")
    return parsed


def _resolve_secret_ref(target: KubernetesTargetLike, operator: Operator) -> str:
    """Validate the pre-read preconditions and return the KV-v2 path.

    Two fail-closed guards, both raising
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    *before* Vault is touched:

    * empty ``operator.raw_jwt`` — an operator-context read requires an
      authenticated operator. System-initiated calls (topology scheduler,
      readiness probe) carry ``raw_jwt=""`` and must error here rather
      than silently falling back to a backplane identity (the decision's
      system-call carve-out, mirroring
      :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`).
    * unset ``target.secret_ref`` — the target is unconfigured.

    Returns the stripped ``secret_ref`` path so trailing whitespace never
    slips into the hvac call.
    """
    if not operator.raw_jwt:
        raise VaultCredentialsReadError(
            "operator-context credential read requires an authenticated operator; "
            f"target={target.name!r} has no operator JWT (system-initiated calls "
            "cannot read per-target kubeconfig credentials)"
        )
    if not target.secret_ref:
        raise VaultCredentialsReadError(
            f"target {target.name!r} has no secret_ref configured; cannot read "
            "its kubeconfig from Vault"
        )
    return target.secret_ref.strip()


def _structural_unwrap(payload: object, *, target_name: str) -> dict[str, object]:
    """Unwrap hvac's ``read_secret_version`` payload to the secret data dict.

    KV-v2's GET on ``/{mount}/data/{path}`` returns
    ``{"data": {"data": {<secret kv>}, "metadata": {...}}}``. The secret
    content is the *nested* ``data["data"]`` — the same double-unwrap the
    shared :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper performs. A malformed payload (missing either ``data`` level)
    raises :class:`VaultCredentialsReadError` naming the target rather
    than a bare ``KeyError`` deep inside hvac's response.
    """
    outer = payload.get("data") if isinstance(payload, dict) else None
    secret_data = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(secret_data, dict):
        raise VaultCredentialsReadError(
            f"vault KV-v2 read for target {target_name!r} returned a malformed "
            "payload: expected a nested 'data.data' object holding the kubeconfig field"
        )
    return secret_data


def _extract_kubeconfig_text(
    secret_data: dict[str, object],
    *,
    target_name: str,
    secret_ref: str,
    field: str,
) -> str:
    """Pull the kubeconfig YAML string out of the secret data dict.

    Missing field or wrong-type field both raise
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    naming the target + field + ``secret_ref`` — never a bare
    ``KeyError`` or ``AttributeError``.
    """
    if field not in secret_data:
        raise VaultCredentialsReadError(
            f"vault secret for target {target_name!r} (secret_ref={secret_ref!r}) "
            f"is missing required field {field!r} (expected kubeconfig YAML)"
        )
    kubeconfig_text = secret_data[field]
    if not isinstance(kubeconfig_text, str):
        raise VaultCredentialsReadError(
            f"vault secret for target {target_name!r} (secret_ref={secret_ref!r}) "
            f"has field {field!r} of type {type(kubeconfig_text).__name__}, "
            "expected a YAML string"
        )
    return kubeconfig_text


async def load_kubeconfig_from_vault(
    target: KubernetesTargetLike,
    operator: Operator,
    *,
    field: str = DEFAULT_KUBECONFIG_FIELD,
    mount: str = DEFAULT_KUBECONFIG_KV_MOUNT,
) -> dict[str, Any]:
    """Default kubeconfig loader — live operator-context Vault KV-v2 read + YAML parse.

    Opens :func:`~meho_backplane.auth.vault.vault_client_for_operator`
    (JWT/OIDC login forwarding ``operator.raw_jwt``), reads
    ``target.secret_ref`` as a KV-v2 secret off the event loop
    (``asyncio.to_thread`` — hvac is synchronous), structurally unwraps
    the nested ``data["data"]``, extracts the kubeconfig YAML from the
    ``kubeconfig`` field, and returns the parsed dict.

    This is the rubric **State 2** wiring (`shared_service_account` only)
    per `Goal #214 (Connector parity) <https://github.com/evoila/meho/issues/214>`_.
    A custom loader can still be injected via ``kubeconfig_loader`` on
    :class:`KubernetesConnector`; this default is what production
    targets use.

    Parameters
    ----------
    target
        The target whose ``secret_ref`` (a KV-v2 path string) holds the
        kubeconfig YAML.
    operator
        The request-scoped operator. ``operator.raw_jwt`` is forwarded
        to Vault's JWT/OIDC auth method — the read happens under the
        operator's Vault Identity entity, giving per-operator RBAC and
        audit (the locked Option A decision).
    field
        The KV-v2 secret field holding the kubeconfig YAML. Defaults to
        ``"kubeconfig"`` (decision #8 convention); pass a different
        value only for a non-default field name.
    mount
        The KV-v2 mount point. Defaults to ``"secret"`` (the consumer
        convention); pass a different value only for a non-default mount.

    Returns
    -------
    dict[str, Any]
        The parsed kubeconfig in the shape
        ``kubernetes_asyncio.config.new_client_from_config_dict`` accepts.

    Raises
    ------
    VaultCredentialsReadError
        Read-phase failure: ``operator.raw_jwt`` is empty (the
        fail-closed system-call carve-out), ``target.secret_ref`` is
        unset, the KV-v2 payload is malformed, or the requested
        ``field`` is missing.
    ValueError
        Raised by :func:`parse_kubeconfig_yaml` when the kubeconfig
        field is not parseable YAML or does not parse to a mapping.
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
    kubeconfig_text = _extract_kubeconfig_text(
        secret_data, target_name=target.name, secret_ref=path, field=field
    )

    # Log only non-secret attribution: target / host / secret_ref / field
    # name — never any kubeconfig content. The parsed dict is ephemeral
    # in-memory state and must not enter any log event, OperationResult,
    # or durable artifact. Resolve the logger per-call so
    # ``structlog.testing.capture_logs`` can reach it (same precedent +
    # rationale as ``_shared.vault_creds.load_basic_credentials``).
    structlog.get_logger(__name__).info(
        "vault_kubeconfig_loaded",
        target=target.name,
        host=target.host,
        secret_ref=path,
        field=field,
    )

    return parse_kubeconfig_yaml(kubeconfig_text)
