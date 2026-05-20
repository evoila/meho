# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault typed-op handlers + ``endpoint_descriptor`` registration helper.

Op-id namespace (v0.2): ``vault.kv.<verb>``.

G3.3-T1 (#366) completes the KV-v2 op group: ``vault.kv.read``
(pre-existing, G0.6-T-Refactor-Vault), plus ``vault.kv.list`` /
``vault.kv.put`` / ``vault.kv.versions`` / ``vault.kv.delete``. The
remaining ``sys`` and ``auth`` read/list groups land in sibling Tasks
under the same Initiative. Secret-engine writes beyond KV-v2
(database/PKI/transit) stay out of scope.

Mount handling: every KV-v2 handler accepts an optional ``mount``
param defaulting to ``"secret"`` (hvac's ``mount_point`` default).
The consumer wrappers address secrets as ``<mount> <path>``; the
deployment-wide default keeps the pre-existing ``vault.kv.read``
``path``-only call sites working unchanged.

Credential-sensitivity classification: ``vault.kv.read`` and
``vault.kv.list`` are ``credential_read`` per locked decision #3
(docs/planning/v0.2-decisions.md). The classifier the G6 broadcast
publisher reads is op-id-based — :func:`meho_backplane.broadcast.\
events.classify_op` consults the ``_CREDENTIAL_READ_OPS`` allowlist,
which already contains both op-ids. The shipped G0.6 substrate has no
per-row ``op_class`` column on ``endpoint_descriptor`` (decision #3
locks the classifier on op-id, not a per-descriptor field), so the
register-time signal is the op-id itself; a regression test pins the
``classify_op`` contract for both ops.

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
from meho_backplane.connectors.vault.ops_auth import register_vault_auth_operations
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VAULT_KV_READ_PARAMETER_SCHEMA",
    "register_vault_typed_operations",
    "vault_kv_delete",
    "vault_kv_list",
    "vault_kv_put",
    "vault_kv_read",
    "vault_kv_versions",
]

#: Default KV-v2 mount point. hvac's ``mount_point`` parameter defaults
#: to ``"secret"``; we mirror that so a handler call with no explicit
#: ``mount`` addresses the deployment's default KV-v2 engine. The
#: consumer wrappers pass ``<mount> <path>`` explicitly when the secret
#: lives under a non-default mount.
_DEFAULT_KV_MOUNT = "secret"

#: Shared JSON Schema fragment for the optional ``mount`` param. KV-v2
#: mount names are single path segments (no slashes); ``pattern`` keeps
#: a stray ``"secret/data"`` from being passed where hvac expects the
#: bare mount handle. The leading ``(?=.*\S)`` lookahead rejects an
#: all-whitespace value at validation time (``invalid_params``) rather
#: than letting it slip past ``minLength`` and degrade to an empty
#: mount after the handler's ``.strip()`` — a runtime
#: ``connector_error`` is a worse signal than a clear param-validation
#: failure.
_MOUNT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "^(?=.*\\S)[^/]+$",
    "default": _DEFAULT_KV_MOUNT,
    "description": (
        "KV v2 secret-engine mount point (single path segment, no "
        "slashes). Optional — defaults to 'secret', the deployment-wide "
        "KV-v2 mount. Supply only when the secret lives under a "
        "non-default mount."
    ),
}

#: Shared JSON Schema fragment for the required ``path`` param. Same
#: non-empty / non-whitespace discipline as the pre-existing
#: ``vault.kv.read`` schema.
_PATH_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "KV v2 secret path relative to the mount root, e.g. "
        "'meho/test/federation'. No leading slash, no mount prefix."
    ),
}


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
#:
#: ``mount`` is the shared optional KV-v2 mount fragment (defaults to
#: ``"secret"``), matching every sibling op (list/put/versions/delete).
#: Path-only ``vault.kv.read`` call sites keep working unchanged — the
#: default resolves the deployment-wide KV-v2 engine — but the consumer
#: wrappers can now address ``<mount> <path>`` for a non-default mount,
#: which is the whole point of Initiative #366 (retiring the
#: ``scripts/_secret-read.sh`` wrapper that derived the mount from the
#: path's first segment).
VAULT_KV_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": _MOUNT_PROPERTY,
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
        "mount": (
            "Optional. KV v2 mount point; defaults to 'secret', the "
            "deployment-wide KV-v2 engine. Supply only when the secret "
            "lives under a non-default mount."
        ),
        "path": (
            "Required. The path under the KV v2 mount (no leading "
            "slash, no mount prefix). With the mount defaulted, the "
            "operator supplies only the path below the mount."
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
        :data:`VAULT_KV_READ_PARAMETER_SCHEMA`. The handler re-extracts
        ``params["path"]`` (schema-guaranteed non-empty, non-whitespace
        string) and the optional ``params["mount"]`` (defaults to
        ``"secret"`` via :data:`_DEFAULT_KV_MOUNT`, forwarded as hvac's
        ``mount_point``).

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
    mount: str = str(params.get("mount", _DEFAULT_KV_MOUNT)).strip()
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
            mount_point=mount,
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


# ---------------------------------------------------------------------------
# vault.kv.list — list keys at a KV-v2 path
# ---------------------------------------------------------------------------

#: ``vault.kv.list`` param schema. ``path`` here is a *folder* path
#: (Vault returns the key names directly beneath it); an empty path
#: lists the mount root, so ``path`` stays required for an unambiguous
#: agent call but the value may be a single segment.
VAULT_KV_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _MOUNT_PROPERTY, "path": _PATH_PROPERTY},
    "required": ["path"],
    "additionalProperties": False,
}

_VAULT_KV_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Key names directly beneath the path. Folder entries are suffixed with '/'."
            ),
        },
    },
    "required": ["keys"],
}

_VAULT_KV_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List the key names at a KV v2 folder path (e.g. 'what secrets "
        "exist under meho/test/?'). Read-only — never mutates the "
        "store. Returns only key names, never values: Vault does not "
        "expose secret content through the list endpoint. Folder "
        "entries are suffixed with '/'."
    ),
    "parameter_hints": {
        "mount": (
            "Optional. KV v2 mount point; defaults to 'secret'. Supply "
            "only for a non-default mount."
        ),
        "path": (
            "Required. The folder path under the mount whose immediate "
            "children to list. Listing a leaf (a secret, not a folder) "
            "returns an empty key list."
        ),
    },
    "output_shape": (
        "On success: {'keys': [<name>, ...]}. On failure: the "
        "dispatcher wraps the raised exception into a connector_error "
        "OperationResult with extras.exception_class set to the "
        "Vault-side error type."
    ),
}


async def vault_kv_list(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List key names at a KV v2 folder path.

    Op-id: ``vault.kv.list``. Read-only. Classified ``credential_read``
    (decision #3) — the broadcast publisher emits aggregate-only for
    this op-id even though the response carries no secret values, since
    key names themselves can leak structure.

    Delegates to hvac's ``secrets.kv.v2.list_secrets`` (the underlying
    ``LIST /v1/<mount>/metadata/<path>`` call). Raises on a malformed
    hvac payload so the dispatcher's ``connector_error`` branch surfaces
    a structured error rather than an unhandled exception.
    """
    mount: str = str(params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path: str = str(params["path"]).strip()

    async with _auth_vault.vault_client_for_operator(target) as client:
        list_payload = await asyncio.to_thread(
            client.secrets.kv.v2.list_secrets,
            path=path,
            mount_point=mount,
        )
        # Structural unwrap -- raises KeyError on a malformed hvac
        # payload (missing the nested ``data``/``keys`` keys). The
        # dispatcher's ``connector_error`` branch turns the KeyError
        # into a structured OperationResult.
        keys = list_payload["data"]["keys"]
        return {"keys": keys}


# ---------------------------------------------------------------------------
# vault.kv.put — write a new secret version
# ---------------------------------------------------------------------------

#: ``vault.kv.put`` param schema. ``data`` is the secret key/value
#: object; ``cas`` is the optional Check-And-Set version guard (0 ⇒
#: create-only, N ⇒ update-only-if-current-version-is-N).
VAULT_KV_PUT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": _MOUNT_PROPERTY,
        "path": _PATH_PROPERTY,
        "data": {
            "type": "object",
            "minProperties": 1,
            "description": (
                "Secret key/value object to write as a new version. "
                "Replaces the latest version wholesale — KV v2 does not "
                "merge; carry forward any keys that must survive."
            ),
        },
        "cas": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Optional Check-And-Set guard. 0 ⇒ write only if the "
                "key does not yet exist. N ⇒ write only if the current "
                "version is exactly N. Omit to write unconditionally."
            ),
        },
    },
    "required": ["path", "data"],
    "additionalProperties": False,
}

_VAULT_KV_PUT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {
            "type": ["integer", "null"],
            "description": "The newly written KV v2 version number.",
        },
    },
    "required": ["version"],
}

_VAULT_KV_PUT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Write a new version of a KV v2 secret. Mutating — creates a "
        "new version (KV v2 keeps history). The write REPLACES the "
        "latest version wholesale; KV v2 does not merge, so include "
        "every key that must survive. Use 'cas' to make the write "
        "conditional and avoid clobbering a concurrent change."
    ),
    "parameter_hints": {
        "mount": "Optional. KV v2 mount point; defaults to 'secret'.",
        "path": "Required. The secret path under the mount.",
        "data": (
            "Required. The full key/value object for the new version. "
            "Not a patch — omitted keys are dropped from the new "
            "version."
        ),
        "cas": (
            "Optional. 0 to require the key be absent (create), N to "
            "require the current version be N (optimistic lock)."
        ),
    },
    "output_shape": (
        "On success: {'version': <new int version>}. On failure: a "
        "connector_error OperationResult with extras.exception_class — "
        "a CAS mismatch surfaces as hvac's InvalidRequest class."
    ),
}


async def vault_kv_put(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Write a new version of a KV v2 secret.

    Op-id: ``vault.kv.put``. Mutating (``op_class=write``,
    ``safety_level=caution``). Delegates to hvac's
    ``secrets.kv.v2.create_or_update_secret`` (``POST
    /v1/<mount>/data/<path>``). The structural unwrap raises on a
    malformed hvac payload so the dispatcher reports a structured
    ``connector_error``.
    """
    mount: str = str(params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path: str = str(params["path"]).strip()
    secret: dict[str, Any] = params["data"]
    cas = params.get("cas")

    async with _auth_vault.vault_client_for_operator(target) as client:
        write_payload = await asyncio.to_thread(
            client.secrets.kv.v2.create_or_update_secret,
            path=path,
            secret=secret,
            cas=cas,
            mount_point=mount,
        )
        version = write_payload["data"]["version"]
        return {"version": version}


# ---------------------------------------------------------------------------
# vault.kv.versions — version metadata for a secret
# ---------------------------------------------------------------------------

VAULT_KV_VERSIONS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _MOUNT_PROPERTY, "path": _PATH_PROPERTY},
    "required": ["path"],
    "additionalProperties": False,
}

_VAULT_KV_VERSIONS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "current_version": {
            "type": ["integer", "null"],
            "description": "The latest version number for the secret.",
        },
        "versions": {
            "type": "object",
            "description": (
                "Per-version metadata keyed by version string: "
                "created_time, deletion_time, destroyed."
            ),
        },
    },
    "required": ["versions"],
}

_VAULT_KV_VERSIONS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Browse the version history of a KV v2 secret — when each "
        "version was created, which are soft-deleted or destroyed. "
        "Read-only metadata; never returns secret values. Use before a "
        "kv.put with a 'cas' guard, or to find a version to undelete."
    ),
    "parameter_hints": {
        "mount": "Optional. KV v2 mount point; defaults to 'secret'.",
        "path": "Required. The secret path under the mount.",
    },
    "output_shape": (
        "On success: {'current_version': <int|null>, 'versions': "
        "{'<n>': {created_time, deletion_time, destroyed}, ...}}. On "
        "failure: a connector_error OperationResult with "
        "extras.exception_class."
    ),
}


async def vault_kv_versions(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read the version metadata for a KV v2 secret.

    Op-id: ``vault.kv.versions``. Read-only (``op_class=read``,
    ``safety_level=safe``). Delegates to hvac's
    ``secrets.kv.v2.read_secret_metadata`` (``GET
    /v1/<mount>/metadata/<path>``). Returns only metadata — never the
    secret values — so it is NOT in the ``credential_read`` allowlist.
    """
    mount: str = str(params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path: str = str(params["path"]).strip()

    async with _auth_vault.vault_client_for_operator(target) as client:
        meta_payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_metadata,
            path=path,
            mount_point=mount,
        )
        data = meta_payload["data"]
        return {
            "current_version": data.get("current_version"),
            "versions": data["versions"],
        }


# ---------------------------------------------------------------------------
# vault.kv.delete — soft-delete specific versions
# ---------------------------------------------------------------------------

VAULT_KV_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": _MOUNT_PROPERTY,
        "path": _PATH_PROPERTY,
        "versions": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "minItems": 1,
            "description": (
                "Version numbers to soft-delete. Soft-delete is "
                "reversible via Vault's undelete path; the underlying "
                "data is retained until destroyed."
            ),
        },
    },
    "required": ["path", "versions"],
    "additionalProperties": False,
}

_VAULT_KV_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deleted_versions": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Echo of the version numbers that were soft-deleted.",
        },
    },
    "required": ["deleted_versions"],
}

_VAULT_KV_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Soft-delete specific versions of a KV v2 secret. Mutating and "
        "dangerous — but reversible: Vault marks the versions deleted "
        "and stops returning them from reads while retaining the "
        "underlying data (undeletable until destroyed). Use to retire "
        "a leaked or stale version without losing recoverability."
    ),
    "parameter_hints": {
        "mount": "Optional. KV v2 mount point; defaults to 'secret'.",
        "path": "Required. The secret path under the mount.",
        "versions": (
            "Required. A non-empty list of version numbers to "
            "soft-delete. This op never deletes 'all versions' — name "
            "each version explicitly."
        ),
    },
    "output_shape": (
        "On success: {'deleted_versions': [<n>, ...]} echoing the "
        "requested versions (Vault returns a 204 with no body). On "
        "failure: a connector_error OperationResult with "
        "extras.exception_class."
    ),
}


async def vault_kv_delete(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Soft-delete specific versions of a KV v2 secret.

    Op-id: ``vault.kv.delete``. Mutating (``op_class=write``,
    ``safety_level=dangerous``). Delegates to hvac's
    ``secrets.kv.v2.delete_secret_versions`` (``POST
    /v1/<mount>/delete/<path>``), which returns a 204 with no body —
    hvac yields a ``requests.Response``, so the handler does not unwrap
    a JSON payload and instead echoes the requested versions for an
    actionable agent-facing result.
    """
    mount: str = str(params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path: str = str(params["path"]).strip()
    versions: list[int] = list(params["versions"])

    async with _auth_vault.vault_client_for_operator(target) as client:
        await asyncio.to_thread(
            client.secrets.kv.v2.delete_secret_versions,
            path=path,
            versions=versions,
            mount_point=mount,
        )
        return {"deleted_versions": versions}


#: Per-op registration specs for the KV-v2 group. One dict per op
#: carrying only the fields that vary between ops; the invariant
#: coordinates (``product`` / ``version`` / ``impl_id`` / ``group_key``)
#: and the caller-supplied ``embedding_service`` are applied uniformly
#: by :func:`register_vault_typed_operations`. Keeping this as data
#: rather than five near-identical call blocks keeps the registrar
#: short and makes the sys/auth groups (sibling Tasks) a one-row
#: append rather than another copy-pasted block.
_KV_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.kv.read",
        "handler": vault_kv_read,
        "summary": "Read a single KV v2 secret from HashiCorp Vault.",
        "description": (
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
        "parameter_schema": VAULT_KV_READ_PARAMETER_SCHEMA,
        "response_schema": _VAULT_KV_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "secret-read"],
        "safety_level": "safe",
        "llm_instructions": _VAULT_KV_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.kv.list",
        "handler": vault_kv_list,
        "summary": "List key names at a KV v2 folder path.",
        "description": (
            "Lists the key names directly beneath a KV v2 folder path "
            "via the operator's OIDC-forwarded JWT. Read-only — never "
            "returns secret values (Vault's list endpoint exposes only "
            "names). Classified credential_read per decision #3: the G6 "
            "broadcast publisher emits aggregate-only for this op-id "
            "because key names can leak structure. Failures land as a "
            "connector_error OperationResult with "
            "extras.exception_class naming the failure class."
        ),
        "parameter_schema": VAULT_KV_LIST_PARAMETER_SCHEMA,
        "response_schema": _VAULT_KV_LIST_RESPONSE_SCHEMA,
        "tags": ["read-only", "credential-read"],
        "safety_level": "safe",
        "llm_instructions": _VAULT_KV_LIST_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.kv.put",
        "handler": vault_kv_put,
        "summary": "Write a new version of a KV v2 secret.",
        "description": (
            "Writes a new version of the secret at the supplied KV v2 "
            "path via the operator's OIDC-forwarded JWT. Mutating — KV "
            "v2 keeps version history; the write replaces the latest "
            "version wholesale (no merge). Optional Check-And-Set guard "
            "('cas'). safety_level=caution; the production-path "
            "approval gate is G7/G10 policy territory. Failures land as "
            "a connector_error OperationResult with "
            "extras.exception_class naming the failure class."
        ),
        "parameter_schema": VAULT_KV_PUT_PARAMETER_SCHEMA,
        "response_schema": _VAULT_KV_PUT_RESPONSE_SCHEMA,
        "tags": ["write", "secret-write"],
        "safety_level": "caution",
        "llm_instructions": _VAULT_KV_PUT_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.kv.versions",
        "handler": vault_kv_versions,
        "summary": "Read the version metadata for a KV v2 secret.",
        "description": (
            "Reads the version history metadata for a KV v2 secret via "
            "the operator's OIDC-forwarded JWT — current version plus "
            "per-version created/deletion/destroyed timestamps. "
            "Read-only metadata browse; never returns secret values, so "
            "NOT classified credential_read. Failures land as a "
            "connector_error OperationResult with "
            "extras.exception_class naming the failure class."
        ),
        "parameter_schema": VAULT_KV_VERSIONS_PARAMETER_SCHEMA,
        "response_schema": _VAULT_KV_VERSIONS_RESPONSE_SCHEMA,
        "tags": ["read-only", "metadata"],
        "safety_level": "safe",
        "llm_instructions": _VAULT_KV_VERSIONS_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.kv.delete",
        "handler": vault_kv_delete,
        "summary": "Soft-delete specific versions of a KV v2 secret.",
        "description": (
            "Soft-deletes the named versions of a KV v2 secret via the "
            "operator's OIDC-forwarded JWT. Reversible — Vault marks "
            "the versions deleted and stops returning them from reads "
            "while retaining the underlying data (undeletable until "
            "destroyed). safety_level=dangerous; the production-path "
            "approval gate is G7/G10 policy territory. Failures land as "
            "a connector_error OperationResult with "
            "extras.exception_class naming the failure class."
        ),
        "parameter_schema": VAULT_KV_DELETE_PARAMETER_SCHEMA,
        "response_schema": _VAULT_KV_DELETE_RESPONSE_SCHEMA,
        "tags": ["write", "destructive", "reversible"],
        "safety_level": "dangerous",
        "llm_instructions": _VAULT_KV_DELETE_LLM_INSTRUCTIONS,
    },
)


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

    Scope: G3.3-T1 registers the full KV-v2 group — ``vault.kv.read``,
    ``vault.kv.list``, ``vault.kv.put``, ``vault.kv.versions``,
    ``vault.kv.delete``. The identity-read group (Task #547,
    ``vault.auth.userpass.list/read`` / ``vault.auth.approle.list/read``)
    is registered from its own module via the
    :func:`~meho_backplane.connectors.vault.ops_auth.register_vault_auth_operations`
    call at the end of this function; the ``sys`` read group (Task
    #546) ships its own lifespan registrar (see ``__init__.py``).
    Keeping each group's registrations in its own module keeps the
    surfaces independently reviewable; no further substrate changes
    are needed.

    ``requires_approval`` for the mutating ops (``kv.put`` /
    ``kv.delete``) is registered ``False`` (the dev default). The
    shipped G0.6 substrate has no per-path approval predicate —
    ``requires_approval`` is a static boolean on the descriptor and the
    production-path gate is G7/G10 policy territory (see the
    :class:`~meho_backplane.db.models.EndpointDescriptor` docstring:
    ``caution``/``dangerous`` ops "flow through G7 / G10 policy logic
    once those Goals land"). ``safety_level`` (``caution`` for
    ``kv.put``, ``dangerous`` for ``kv.delete``) is the load-bearing
    signal the future policy gate keys on.
    """
    for spec in _KV_OP_SPECS:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            group_key="kv",
            # G0.9-T4a #731 placeholder; T4b #732 replaces with a
            # curated blurb for the ``kv`` group.
            when_to_use="TODO: curate (T4b #732)",
            requires_approval=False,
            embedding_service=embedding_service,
            **spec,
        )
    # Identity-read group (Task #547) -- registered from its own
    # module so the auth surface stays independently reviewable while
    # the package keeps a single lifespan-driven registrar entry.
    await register_vault_auth_operations(embedding_service=embedding_service)
