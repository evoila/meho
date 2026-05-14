# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault typed-op handlers + ``endpoint_descriptor`` registration helper.

Op-id namespace (v0.2): ``vault.kv.<verb>``.

Future ops (``vault.kv.write``, ``vault.kv.list``, ``vault.policy.read``,
``vault.transit.encrypt``) are intentionally out of scope for the G0.6-T-
Refactor-Vault Task — the acceptance criteria specify only ``vault.kv.read``
as the existing surface to round-trip through the new substrate. G3.3
(#366) covers the full KV-v2 + sys + auth read/list surface.

Each handler is an ``async def`` module-level function with the
``(target, params) -> dict[str, Any]`` shape the G0.6 dispatcher (T5
#396) expects from a typed op (see
:class:`~meho_backplane.operations.typed_register.TypedOpHandler`). The
dispatcher validates ``params`` against the registered
``parameter_schema`` before invoking the handler; the handler's only
job is the Vault HTTP call + the success-payload shape.

Failure handling differs from the pre-G0.6 ``OP_MAP`` model: handlers
now **raise** rather than returning a structured ``OperationResult``.
The dispatcher catches the exception and produces a structured
``connector_error`` :class:`OperationResult` with the exception class
name in ``extras["exception_class"]``. Callers that need to distinguish
login-phase failure (Vault unreachable, role denied) from read-phase
failure (KV miss, malformed payload) read ``extras["exception_class"]``
and map :class:`~meho_backplane.auth.vault.VaultClientError` and
subclasses to "login phase"; everything else is "read phase". The
:mod:`meho_backplane.api.v1.health` route does exactly this mapping for
the federation-proof endpoint.

The ``_auth_vault`` module reference is used throughout so that the
test seam (``monkeypatch.setattr(vault_module, "_build_client", fake)``
and ``monkeypatch.setattr(vault_module, "vault_client_for_operator",
fake)``) applies transparently. Binding the helpers by name (``from ...
import _build_client``) would break the monkeypatch because the local
name would still point at the original object.
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VAULT_KV_READ_PARAMETER_SCHEMA",
    "register_vault_typed_operations",
    "vault_kv_read",
]


#: JSON Schema 2020-12 for the ``vault.kv.read`` op's ``params`` argument.
#: Captures the same input-validation discipline the pre-G0.6 handler
#: enforced inline (non-empty, non-whitespace string ``path``), but does
#: so via the dispatcher's :class:`Draft202012Validator` rather than an
#: in-handler ``isinstance`` + ``strip()`` check:
#:
#: * ``minLength=1`` rejects ``""``.
#: * ``pattern="\\S"`` rejects whitespace-only strings (``"   "``).
#: * ``additionalProperties=False`` rejects unexpected keys so a typo
#:   like ``{"paht": "..."}`` surfaces as a clear validation error
#:   instead of silently dispatching with a missing ``path``.
VAULT_KV_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "KV v2 secret path relative to the mount root, e.g. "
                "'meho/test/federation'. The handler delegates to "
                "client.secrets.kv.v2.read_secret_version(path=...) verbatim."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


#: JSON Schema for the ``vault.kv.read`` response payload. Informational
#: in v0.2 (the dispatcher's :class:`PassThroughReducer` does not validate
#: outbound payloads); declared so the meta-tools (T8 #399) can surface
#: it on ``describe_operation`` calls without a schema-construction
#: round-trip.
_VAULT_KV_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "data": {
            "type": "object",
            "description": "KV v2 secret data dict — the actual key/value payload.",
        },
        "version": {
            "type": ["integer", "null"],
            "description": "KV v2 metadata version, or null if the metadata lacked a version key.",
        },
    },
    "required": ["data"],
}


#: ``llm_instructions`` blob the meta-tools (T8) inline verbatim when an
#: LLM is choosing whether to call this op. The shape mirrors the
#: discipline G3.3 (#366) will enforce for the full Vault op surface:
#: when-to-use prose + a parameter-hint block + an output-shape sketch.
_VAULT_KV_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read a single KV v2 secret from HashiCorp Vault. Use when the "
        "operator's question names a specific secret path (e.g. "
        "'what's the value of meho/test/federation?'). Read-only — never "
        "mutates the secret store. Operator identity is forwarded to "
        "Vault via OIDC, so every read shows up in Vault's audit log "
        "attributed to the calling operator."
    ),
    "parameter_hints": {
        "path": (
            "Required. The path under the KV v2 mount (no leading "
            "slash, no mount prefix). The mount is configured "
            "deployment-wide; the operator only supplies the path "
            "below the mount."
        ),
    },
    "output_shape": (
        "On success: {'data': <secret key/values>, 'version': <int|null>}. "
        "On failure: the dispatcher wraps the raised exception into a "
        "connector_error OperationResult with extras.exception_class set "
        "to the Vault-side error type ('VaultUnreachableError', "
        "'VaultRoleDeniedError', etc.)."
    ),
}


async def vault_kv_read(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read a KV v2 secret from Vault via OIDC-forwarded operator JWT.

    Op-id: ``vault.kv.read``.

    Parameters
    ----------
    target
        Duck-typed target. Only ``target.raw_jwt`` is read, by way of
        :func:`~meho_backplane.auth.vault.vault_client_for_operator`.
        :class:`~meho_backplane.connectors.vault.connector.VaultTarget`
        is the concrete shape today; once G0.3 (#224) lands its
        ``Target`` model the connector resolver picks the
        :class:`VaultConnector` and the dispatcher binds this handler
        against the resolved target row.
    params
        Already validated by the dispatcher against
        :data:`VAULT_KV_READ_PARAMETER_SCHEMA`. The handler only re-
        extracts ``params["path"]`` -- the schema guarantees it's a
        non-empty, non-whitespace string.

    Returns
    -------
    dict[str, Any]
        ``{"data": <secret data dict>, "version": <int|None>}``. The
        dispatcher's reducer (v0.2 :class:`PassThroughReducer`) lands
        this dict as :attr:`OperationResult.result` verbatim.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        ``VaultUnreachableError`` (network/TLS) or ``VaultRoleDeniedError``
        (Vault rejected the JWT for the configured role). The
        dispatcher catches these in its ``connector_error`` branch and
        reports ``extras["exception_class"]`` so callers can map them
        to the login-failure phase.
    KeyError
        A malformed hvac payload (e.g. ``{"data": {}}`` missing the
        nested ``data`` or ``metadata`` keys). Raised by the structural
        unwrap so the dispatcher's ``connector_error`` branch surfaces
        ``exception_class="KeyError"``; callers map non-VaultClientError
        exceptions to the read-failure phase.
    Exception
        Any error raised by hvac's ``read_secret_version`` (permission
        denied, path missing, transient network blip after login). The
        dispatcher's ``connector_error`` branch handles these uniformly.

    The two-phase distinction (login vs read) is preserved through the
    exception class hierarchy: every login-side failure raises a
    :class:`VaultClientError` subclass; every read-side failure raises
    something else. Callers that need to render an operator-actionable
    detail string (``/api/v1/health``) inspect
    ``OperationResult.extras["exception_class"]`` and check
    ``issubclass(...)`` on the class name -- the exception class name
    is the contract, not the hierarchy at runtime.
    """
    # Schema-enforced: ``path`` is a non-empty non-whitespace string.
    # We re-strip so trailing whitespace doesn't slip into the hvac
    # call -- the schema permits ``"a "`` (one non-whitespace char) and
    # we want the wire shape to match what the operator typed.
    path: str = str(params["path"]).strip()

    # vault_client_for_operator is accessed via the module reference so
    # that test monkeypatches on vault_module._build_client propagate
    # through the call chain. The target object is duck-typed:
    # vault_client_for_operator only accesses target.raw_jwt, which
    # VaultTarget provides.
    async with _auth_vault.vault_client_for_operator(target) as client:
        secret_payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=path,
            raise_on_deleted_version=False,
        )
        # Structural unwrap -- raises KeyError on a malformed hvac
        # payload (e.g. ``{"data": {}}`` missing the nested ``data`` or
        # ``metadata`` keys). The dispatcher's ``connector_error``
        # branch turns the KeyError into a structured ``OperationResult``;
        # callers read ``extras["exception_class"]`` to distinguish
        # this read-phase failure from a login-phase failure (which
        # raises a ``VaultClientError`` subclass instead).
        data = secret_payload["data"]
        secret_data = data["data"]
        metadata = data["metadata"]
        version = metadata.get("version")
        return {"data": secret_data, "version": version}


async def register_vault_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every Vault typed op into ``endpoint_descriptor``.

    Called once per process from the FastAPI lifespan after the
    connector registry is populated (see
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`).
    The helper is idempotent: a second call with the same args is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    Test seam: the ``embedding_service`` parameter lets test fixtures
    inject a stub so the chassis tests (``test_connectors_vault.py``,
    ``test_api_v1_health.py``) don't have to load the ONNX model.
    Production callers leave it ``None`` and the helper resolves the
    process-wide singleton via the ``register_typed_operation`` body.

    Scope: today this registers only ``vault.kv.read``. G3.3 (#366)
    will add the full KV-v2 + sys + auth read/list surface on top of
    this helper without further substrate changes.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=vault_kv_read,
        summary="Read a single KV v2 secret from HashiCorp Vault.",
        description=(
            "Reads the secret at the supplied KV v2 path via the operator's "
            "OIDC-forwarded JWT. Returns the secret data dict plus the "
            "KV v2 metadata version. Read-only — never mutates the "
            "secret store. Login-side failures (Vault unreachable, role "
            "denied) raise VaultClientError subclasses; read-side "
            "failures (KV miss, malformed payload, hvac raise) raise "
            "the underlying exception. Either failure mode lands as a "
            "connector_error OperationResult from the dispatcher with "
            "extras.exception_class naming the failure class."
        ),
        parameter_schema=VAULT_KV_READ_PARAMETER_SCHEMA,
        response_schema=_VAULT_KV_READ_RESPONSE_SCHEMA,
        group_key="kv",
        tags=["read-only", "secret-read"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_VAULT_KV_READ_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
