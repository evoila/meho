# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault ``sys`` read op group — typed handlers + ``endpoint_descriptor``
registration helper.

Op-id namespace (v0.2): ``vault.sys.<verb>``. The four ops here are the
read-only diagnostics surface the consumer's wrappers exercise daily:

* ``vault.sys.health`` — cluster health (``GET /v1/sys/health``).
* ``vault.sys.seal_status`` — seal state (``GET /v1/sys/seal-status``).
* ``vault.sys.mounts.list`` — enabled secret backends (``GET /v1/sys/mounts``).
* ``vault.sys.auth.list`` — enabled auth backends (``GET /v1/sys/auth``).

All four are ``safety_level='safe'`` and classify as ``op_class='read'``
via :func:`~meho_backplane.broadcast.events.classify_op` (the ``.health``
and ``.seal_status`` suffixes were added to that classifier's read-suffix
tuple so the diagnostics surface broadcasts at the same sensitivity as
``.list`` / ``.get`` ops — there is no secret content in any of these
payloads). ``sys`` writes (unseal, mount/unmount, policy write) are out
of scope for v0.2.

Handler shape mirrors :mod:`meho_backplane.connectors.vault.ops`: each
handler is a module-level ``async def`` typed op. Handlers that forward
the operator JWT to Vault (``seal_status`` / ``mounts.list`` /
``auth.list``) take ``(operator, target, params)`` and read the
request-scoped token from ``operator.raw_jwt`` (G0.8-T3 #629);
``vault.sys.health`` hits Vault's *unauthenticated* ``/v1/sys/health``
and keeps the ``(target, params)`` shape (no operator needed). The
dispatcher's :func:`~meho_backplane.operations._branches.dispatch_typed`
introspects each signature independently and threads ``operator`` only
when the handler names it. The dispatcher validates ``params`` against
the registered ``parameter_schema`` before invoking; the handler's only
job is the Vault HTTP call plus the success-payload shape. Handlers **raise** on
failure; the dispatcher's ``connector_error`` branch turns the raised
exception into a structured :class:`OperationResult` with the exception
class name in ``extras["exception_class"]`` — so an unreachable or
sealed Vault never surfaces a raw traceback to the agent (DoD:
structured dispatcher errors).

``vault.sys.health`` is **not** a second copy of the probe-path health
check. It reuses the exact same three seams the
:class:`~meho_backplane.connectors.vault.connector.VaultConnector`
``probe`` / ``fingerprint`` methods use —
:func:`~meho_backplane.auth.vault._build_client` (unauthenticated
client, settings-driven address/namespace/timeout),
:func:`~meho_backplane.auth.vault._to_thread_read_health` (the
off-event-loop ``GET /v1/sys/health`` call), and
:func:`~meho_backplane.auth.vault._classify_health_response` (the
200/429/472/473/501/503 → ``(ok, detail)`` contract). Sharing those
helpers means the op and the readiness probe cannot drift: a change to
Vault's health contract is fixed in one place. The ``_auth_vault``
module reference (not a ``from ... import`` of the helpers) is used so
the existing test seam — ``monkeypatch.setattr(vault_module,
"_build_client", fake)`` — applies transparently here too.

The three authenticated ops (``seal_status`` is unauthenticated on
Vault's side, but routing it through the operator client keeps the
audit attribution uniform and costs one extra OIDC login) forward the
operator's JWT via
:func:`~meho_backplane.auth.vault.vault_client_for_operator`, exactly
like ``vault.kv.read``. Their hvac calls are wrapped in
:func:`asyncio.to_thread` because hvac is synchronous (``requests``-
based) and FastAPI does not auto-offload blocking I/O inside an
``async def``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService
from meho_backplane.settings import get_settings

__all__ = [
    "register_vault_sys_typed_operations",
    "vault_sys_auth_list",
    "vault_sys_health",
    "vault_sys_mounts_list",
    "vault_sys_seal_status",
]


#: Shared empty-parameter schema. None of the four sys read ops take a
#: parameter — they each return a fixed cluster-wide view. ``additional
#: Properties=False`` rejects a stray key (e.g. a typo'd ``{"mount":
#: ...}``) with a clear dispatcher-side validation error rather than
#: silently ignoring it.
_NO_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_VAULT_SYS_HEALTH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {
            "type": "boolean",
            "description": (
                "True when Vault is serving requests (HTTP 200/429/472/473); "
                "False when sealed or uninitialized."
            ),
        },
        "detail": {
            "type": "string",
            "description": (
                "Structured reason string ('sealed=False', 'sealed', "
                "'uninitialized', 'http_429', ...). Never echoes Vault's "
                "URL or namespace."
            ),
        },
        "version": {
            "type": ["string", "null"],
            "description": (
                "Vault server version from the health payload, or null on a non-200 response."
            ),
        },
        "cluster_name": {
            "type": ["string", "null"],
            "description": "Cluster name from the health payload, or null when absent.",
        },
        "sealed": {
            "type": ["boolean", "null"],
            "description": "Seal flag from the health payload, or null on a non-200 response.",
        },
        "initialized": {
            "type": ["boolean", "null"],
            "description": (
                "Initialized flag from the health payload, or null on a non-200 response."
            ),
        },
    },
    "required": ["ok", "detail"],
}


_VAULT_SYS_SEAL_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Seal type, e.g. 'shamir'."},
        "initialized": {"type": "boolean"},
        "sealed": {"type": "boolean"},
        "t": {"type": "integer", "description": "Unseal threshold."},
        "n": {"type": "integer", "description": "Total key shares."},
        "progress": {"type": "integer", "description": "Unseal-key submission progress."},
        "version": {"type": "string"},
        "build_date": {"type": ["string", "null"]},
        "migration": {"type": ["boolean", "null"]},
        "cluster_name": {"type": ["string", "null"]},
        "cluster_id": {"type": ["string", "null"]},
        "recovery_seal": {"type": ["boolean", "null"]},
        "storage_type": {"type": ["string", "null"]},
    },
    "required": ["sealed", "initialized"],
}


_VAULT_SYS_MOUNTS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mounts": {
            "type": "object",
            "description": (
                "Map of mount path ('secret/', 'cubbyhole/', ...) to the "
                "mount descriptor (type, description, accessor, options, "
                "config, ...). The raw 'data' object from GET /v1/sys/mounts."
            ),
        },
    },
    "required": ["mounts"],
}


_VAULT_SYS_AUTH_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "auth_methods": {
            "type": "object",
            "description": (
                "Map of auth mount path ('token/', 'userpass/', ...) to the "
                "method descriptor (type, description, accessor, config, "
                "...). The raw 'data' object from GET /v1/sys/auth."
            ),
        },
    },
    "required": ["auth_methods"],
}


_VAULT_SYS_HEALTH_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Check whether a Vault target is reachable, unsealed, and serving "
        "requests. Use for an operator's 'is Vault up?' / 'is Vault "
        "sealed?' question before attempting a secret read. Read-only and "
        "unauthenticated on Vault's side — never mutates state and never "
        "returns secret content. Shares its implementation with the "
        "backplane's own Vault readiness probe."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'ok': <bool>, 'detail': <str>, 'version': <str|null>, "
        "'cluster_name': <str|null>, 'sealed': <bool|null>, "
        "'initialized': <bool|null>}. On unreachable Vault: a "
        "connector_error OperationResult with extras.exception_class set "
        "to the requests/hvac error type."
    ),
}


_VAULT_SYS_SEAL_STATUS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read Vault's seal state and unseal progress (type, threshold t, "
        "shares n, progress). Use when an operator asks about seal status "
        "or unseal progress specifically — finer-grained than "
        "vault.sys.health. Read-only; no secret content."
    ),
    "parameter_hints": {},
    "output_shape": (
        "The raw seal-status object: {'type', 'sealed', 'initialized', "
        "'t', 'n', 'progress', 'version', ...}. On failure: a "
        "connector_error OperationResult with extras.exception_class."
    ),
}


_VAULT_SYS_MOUNTS_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List the secret backends enabled on the Vault target (KV mounts, "
        "transit, pki, ...). Use to discover which mount path to read "
        "from before a vault.kv.read. Read-only; returns mount metadata, "
        "not secret values."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'mounts': {<mount-path>: {'type', 'description', 'accessor', "
        "'options', 'config', ...}}}. On failure: connector_error with "
        "extras.exception_class."
    ),
}


_VAULT_SYS_AUTH_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List the auth methods enabled on the Vault target (token, "
        "userpass, approle, jwt, ...). Use to discover the auth surface "
        "before an identity-read op. Read-only; returns method metadata, "
        "not credentials."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'auth_methods': {<mount-path>: {'type', 'description', "
        "'accessor', 'config', ...}}}. On failure: connector_error with "
        "extras.exception_class."
    ),
}


async def vault_sys_health(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read Vault cluster health — shares the probe-path implementation.

    Op-id: ``vault.sys.health``.

    This handler does **not** duplicate the readiness-probe health
    check. It calls the same three
    :mod:`meho_backplane.auth.vault` seams the
    :meth:`~meho_backplane.connectors.vault.connector.VaultConnector.probe`
    method uses:

    * :func:`~meho_backplane.auth.vault._build_client` — an
      unauthenticated client built from settings (address, namespace,
      timeout). ``/v1/sys/health`` is an unauthenticated Vault
      endpoint, so no operator JWT / OIDC login is needed; the
      ``target`` argument is accepted to satisfy the dispatcher's
      typed-handler contract but is intentionally unused.
    * :func:`~meho_backplane.auth.vault._to_thread_read_health` — runs
      hvac's blocking ``sys.read_health_status(method="GET")`` off the
      event loop.
    * :func:`~meho_backplane.auth.vault._classify_health_response` —
      the canonical 200/429/472/473/501/503 → ``(ok, detail)``
      mapping. One change to Vault's health contract is fixed once and
      both the op and the probe inherit it.

    Parameters
    ----------
    target
        Unused. ``/v1/sys/health`` needs no operator credential; the
        parameter is part of the dispatcher's typed-handler signature.
    params
        Validated empty object (the schema forbids any key).

    Returns
    -------
    dict[str, Any]
        ``{"ok": <bool>, "detail": <str>, "version": <str|None>,
        "cluster_name": <str|None>, "sealed": <bool|None>,
        "initialized": <bool|None>}``. The classification fields come
        from :func:`_classify_health_response`; the descriptive fields
        are pulled from the JSON payload when Vault returned HTTP 200
        (a ``dict``) and are ``None`` on a non-200 response (hvac
        returns a :class:`requests.Response` there, which carries no
        decoded body).

    Raises
    ------
    requests.exceptions.ConnectionError | requests.exceptions.Timeout
        Vault unreachable (DNS/TCP/TLS/timeout). The dispatcher's
        ``connector_error`` branch records the class name in
        ``extras["exception_class"]`` so the agent sees a structured
        error, not a traceback.
    hvac.exceptions.VaultError
        Any Vault-side error hvac raises from the health read.
    """
    # ``target`` is unused on purpose — see the docstring. Binding it to
    # ``_`` documents the intent at the call boundary and keeps
    # unused-argument linters quiet without an inline suppression.
    _ = target

    settings = get_settings()
    client = _auth_vault._build_client(settings)
    payload = await _auth_vault._to_thread_read_health(client)
    ok, detail = _auth_vault._classify_health_response(payload)

    # hvac returns a decoded dict only for HTTP 200; non-200 responses
    # come back as a requests.Response with no decoded body, so the
    # descriptive fields are None there. ``_classify_health_response``
    # already extracted the ok/detail signal from either shape.
    if isinstance(payload, dict):
        return {
            "ok": ok,
            "detail": detail,
            "version": payload.get("version"),
            "cluster_name": payload.get("cluster_name"),
            "sealed": payload.get("sealed"),
            "initialized": payload.get("initialized"),
        }
    return {
        "ok": ok,
        "detail": detail,
        "version": None,
        "cluster_name": None,
        "sealed": None,
        "initialized": None,
    }


async def vault_sys_seal_status(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read Vault's seal status (``GET /v1/sys/seal-status``).

    Op-id: ``vault.sys.seal_status``.

    Routes through :func:`~meho_backplane.auth.vault.vault_client_for_operator`
    for uniform per-operator audit attribution even though the Vault
    endpoint itself is unauthenticated. The hvac
    ``sys.read_seal_status()`` call is offloaded with
    :func:`asyncio.to_thread` because hvac is synchronous.

    Returns the raw seal-status object verbatim (``{"type", "sealed",
    "initialized", "t", "n", "progress", "version", ...}``).

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure (Vault unreachable, role denied). The
        dispatcher wraps it into a ``connector_error`` result.
    Exception
        Any error hvac raises from the seal-status read.
    """
    _ = params  # schema-validated empty object; no inputs to extract.
    async with _auth_vault.vault_client_for_operator(operator) as client:
        return await asyncio.to_thread(client.sys.read_seal_status)


async def vault_sys_mounts_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List enabled secret backends (``GET /v1/sys/mounts``).

    Op-id: ``vault.sys.mounts.list``.

    Forwards the operator JWT via
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` (the
    mounts list is authenticated on Vault's side). The blocking hvac
    call runs in a worker thread.

    Returns ``{"mounts": <data>}`` where ``<data>`` is the ``data``
    object hvac returns from ``sys.list_mounted_secrets_engines()`` —
    a map of mount path to mount descriptor. The ``mounts`` wrapper
    key keeps the response a JSON object (JSONFlux/result-handle
    wrapping operates on object/array shapes) and names the payload
    for the agent.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the mounts read.
    """
    _ = params
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.sys.list_mounted_secrets_engines)
        # hvac returns the full Vault envelope ({request_id, data, ...}).
        # The mount map lives under ``data``; fall back to the whole
        # response on the (older-hvac) chance the envelope is unwrapped.
        mounts = response.get("data", response) if isinstance(response, dict) else response
        return {"mounts": mounts}


async def vault_sys_auth_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List enabled auth methods (``GET /v1/sys/auth``).

    Op-id: ``vault.sys.auth.list``.

    Same shape and rationale as :func:`vault_sys_mounts_list`: operator
    JWT forwarded, blocking hvac call offloaded, the ``data`` object
    returned under an ``auth_methods`` wrapper key.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the auth-methods read.
    """
    _ = params
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.sys.list_auth_methods)
        auth_methods = response.get("data", response) if isinstance(response, dict) else response
        return {"auth_methods": auth_methods}


async def register_vault_sys_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the Vault ``sys`` read ops into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list from the package
    ``__init__`` alongside
    :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`.
    Idempotent on restart: a second call with unchanged descriptions
    is a no-op for the embedding pipeline (the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    The ``embedding_service`` test seam matches the KV-v2 registrar so
    chassis tests can inject a stub instead of loading the ONNX model.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.health",
        handler=vault_sys_health,
        summary="Check HashiCorp Vault cluster health (reachable / sealed / serving).",
        description=(
            "Reads GET /v1/sys/health via the unauthenticated, settings-"
            "driven client and classifies the 200/429/472/473/501/503 "
            "response into (ok, detail). Shares its implementation with "
            "the backplane's Vault readiness probe — they cannot drift. "
            "Read-only; never returns secret content. Unreachable Vault "
            "surfaces as a connector_error OperationResult with the "
            "requests/hvac error class in extras.exception_class."
        ),
        parameter_schema=_NO_PARAMS_SCHEMA,
        response_schema=_VAULT_SYS_HEALTH_RESPONSE_SCHEMA,
        group_key="sys",
        tags=["read-only", "diagnostics", "health"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_VAULT_SYS_HEALTH_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.seal_status",
        handler=vault_sys_seal_status,
        summary="Read HashiCorp Vault seal status and unseal progress.",
        description=(
            "Reads GET /v1/sys/seal-status and returns the raw seal-"
            "status object (type, sealed, initialized, threshold t, "
            "shares n, progress, version). Read-only; no secret "
            "content. Login/transport failures surface as a structured "
            "connector_error OperationResult."
        ),
        parameter_schema=_NO_PARAMS_SCHEMA,
        response_schema=_VAULT_SYS_SEAL_STATUS_RESPONSE_SCHEMA,
        group_key="sys",
        tags=["read-only", "diagnostics", "seal"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_VAULT_SYS_SEAL_STATUS_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.mounts.list",
        handler=vault_sys_mounts_list,
        summary="List the secret backends enabled on a HashiCorp Vault target.",
        description=(
            "Reads GET /v1/sys/mounts via the operator's OIDC-forwarded "
            "JWT and returns the mount map under a 'mounts' key (path -> "
            "{type, description, accessor, options, config, ...}). Read-"
            "only; returns mount metadata, not secret values."
        ),
        parameter_schema=_NO_PARAMS_SCHEMA,
        response_schema=_VAULT_SYS_MOUNTS_LIST_RESPONSE_SCHEMA,
        group_key="sys",
        tags=["read-only", "diagnostics", "mounts"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_VAULT_SYS_MOUNTS_LIST_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.auth.list",
        handler=vault_sys_auth_list,
        summary="List the auth methods enabled on a HashiCorp Vault target.",
        description=(
            "Reads GET /v1/sys/auth via the operator's OIDC-forwarded "
            "JWT and returns the auth-method map under an 'auth_methods' "
            "key (path -> {type, description, accessor, config, ...}). "
            "Read-only; returns method metadata, not credentials."
        ),
        parameter_schema=_NO_PARAMS_SCHEMA,
        response_schema=_VAULT_SYS_AUTH_LIST_RESPONSE_SCHEMA,
        group_key="sys",
        tags=["read-only", "diagnostics", "auth-methods"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_VAULT_SYS_AUTH_LIST_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
