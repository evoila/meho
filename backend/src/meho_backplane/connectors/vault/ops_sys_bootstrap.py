# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault ``sys`` bootstrap ops — auth-method + secret-mount enable/tune.

Op-ids (G3.15-T5 #1413), all under the ``sys`` operation group and
registered from :func:`meho_backplane.connectors.vault.ops_sys.register_vault_sys_typed_operations`:

* ``vault.sys.auth.enable`` — enable an auth method
  (``POST /v1/sys/auth/<path>``).
* ``vault.sys.auth.tune`` — tune an enabled auth method's mount config
  (``POST /v1/sys/auth/<path>/tune``).
* ``vault.sys.mounts.enable`` — enable a secret backend
  (``POST /v1/sys/mounts/<path>``).
* ``vault.sys.mounts.tune`` — tune an enabled mount's config
  (``POST /v1/sys/mounts/<path>/tune``).

These are the bootstrap operations the ops team previously ran via the
break-glass shell wrapper — the last slice of the ``/vault-admin`` write
surface. All four are ``requires_approval=True``. The two ``.enable``
ops are ``safety_level='dangerous'`` (enabling an auth method or secret
engine widens the cluster's attack/credential surface); the two
``.tune`` ops are ``safety_level='caution'`` (they reconfigure an
already-enabled mount — lease TTLs, description, listing visibility —
without standing up a new credential path).

Idempotency. ``enable`` is treated as idempotent-tolerant: re-enabling
an already-enabled method/engine at the same path is reported as
``created=False`` success rather than a ``connector_error``. Vault
returns HTTP 400 ``"path is already in use"`` on a duplicate enable; the
handler unwraps that one signal into a success payload (matching the
connector's existing error-unwrapping posture — the dispatcher's
``connector_error`` branch still owns every *other* failure). A genuine
configuration mismatch (different type at the same path) is a distinct
Vault error and propagates. ``tune`` is naturally idempotent on Vault's
side (re-applying the same config is a no-op 204) and needs no special
handling.

Broadcast ``op_class`` (via
:func:`~meho_backplane.broadcast.events.classify_op`): all four classify
``other`` — ``.enable`` / ``.tune`` are deliberately **not** added to
the classifier's write-suffix tuple. Adding ``.enable`` there would
reclassify the unrelated ``meho.connector.enable`` MCP admin tool (whose
broadcast op_class is derived from ``classify_op`` on the tool name) from
``other`` to ``write``, an out-of-scope behaviour change. None of these
ops carry secret material in their params (auth/mount type, path, lease
TTLs, descriptions — configuration only), so the full-detail ``other``
broadcast leaks nothing; classifying them ``other`` is both the cleaner
and the more scoped choice. See ``docs/codebase/connectors-vault.md``.

Handler shape mirrors the sibling policy ops in
:mod:`meho_backplane.connectors.vault.ops_sys_policy`:
``(operator, target, params)``, the operator JWT forwarded via
:func:`~meho_backplane.auth.vault.vault_client_for_operator`, and the
synchronous hvac call offloaded with :func:`asyncio.to_thread` because
hvac is ``requests``-based and FastAPI does not auto-offload blocking I/O
inside an ``async def``. Handlers **raise** on failure; the dispatcher's
``connector_error`` branch records the exception class in
``extras["exception_class"]`` (no raw traceback to the agent).

This module is split out of ``ops_sys.py`` purely to keep each file under
the repository's 600-line size budget; the ``register_typed_operation``
calls stay in ``ops_sys.py`` so the whole ``sys`` group registers from
one place.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator

__all__ = [
    "VAULT_SYS_AUTH_ENABLE_LLM_INSTRUCTIONS",
    "VAULT_SYS_AUTH_ENABLE_PARAMETER_SCHEMA",
    "VAULT_SYS_AUTH_ENABLE_RESPONSE_SCHEMA",
    "VAULT_SYS_AUTH_TUNE_LLM_INSTRUCTIONS",
    "VAULT_SYS_AUTH_TUNE_PARAMETER_SCHEMA",
    "VAULT_SYS_AUTH_TUNE_RESPONSE_SCHEMA",
    "VAULT_SYS_MOUNTS_ENABLE_LLM_INSTRUCTIONS",
    "VAULT_SYS_MOUNTS_ENABLE_PARAMETER_SCHEMA",
    "VAULT_SYS_MOUNTS_ENABLE_RESPONSE_SCHEMA",
    "VAULT_SYS_MOUNTS_TUNE_LLM_INSTRUCTIONS",
    "VAULT_SYS_MOUNTS_TUNE_PARAMETER_SCHEMA",
    "VAULT_SYS_MOUNTS_TUNE_RESPONSE_SCHEMA",
    "vault_sys_auth_enable",
    "vault_sys_auth_tune",
    "vault_sys_mounts_enable",
    "vault_sys_mounts_tune",
]


#: Shared ``path`` schema fragment for the four bootstrap ops. The
#: ``(?=.*\S)`` lookahead makes an all-whitespace value a validation-time
#: ``invalid_params`` failure (mirrors the policy-name fragment in
#: :mod:`~meho_backplane.connectors.vault.ops_sys_policy`); a leading or
#: trailing slash is rejected so the path is the bare mount point Vault
#: expects (hvac appends the trailing slash itself). The path is where
#: the method/engine is mounted, defaulting server-side to the type name
#: when omitted — but it is required here so an agent must be explicit
#: about *where* a dangerous mount lands.
_MOUNT_PATH_PROPERTY: dict[str, Any] = {
    "type": "string",
    "pattern": r"^(?=.*\S)[^/]+$",
    "description": (
        "The mount path (a flat handle, no slashes, e.g. 'userpass' or "
        "'kv-prod'). This is where the method/engine is mounted; it need "
        "not match the type."
    ),
}


#: Shared tune-config fragment. Vault's tune endpoint accepts a fixed set
#: of mount-config knobs; only these are exposed (``additionalProperties``
#: stays False on the parent object so a typo'd knob is a clear
#: validation error). TTLs accept the Vault duration spellings ("768h",
#: "30m") or an integer of seconds; hvac forwards them verbatim.
_TUNE_PROPERTIES: dict[str, Any] = {
    "default_lease_ttl": {
        "type": ["string", "integer"],
        "description": (
            "Default lease TTL for the mount (Vault duration string like "
            "'768h' or an integer of seconds)."
        ),
    },
    "max_lease_ttl": {
        "type": ["string", "integer"],
        "description": ("Maximum lease TTL for the mount (duration string or seconds)."),
    },
    "description": {
        "type": "string",
        "description": "Human-readable mount description.",
    },
    "listing_visibility": {
        "type": "string",
        "enum": ["unauth", "hidden"],
        "description": (
            "Whether the mount appears in the unauthenticated UI listing "
            "('unauth') or is hidden ('hidden')."
        ),
    },
}


VAULT_SYS_AUTH_ENABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method_type": {
            "type": "string",
            "pattern": r"^(?=.*\S)\S+$",
            "description": (
                "The auth method type to enable, e.g. 'userpass', 'approle', 'jwt', 'kubernetes'."
            ),
        },
        "path": _MOUNT_PATH_PROPERTY,
        "description": {
            "type": "string",
            "description": "Optional human-readable description for the mount.",
        },
    },
    "required": ["method_type", "path"],
    "additionalProperties": False,
}


VAULT_SYS_MOUNTS_ENABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "backend_type": {
            "type": "string",
            "pattern": r"^(?=.*\S)\S+$",
            "description": (
                "The secret engine type to enable, e.g. 'kv', 'transit', 'pki', 'database'."
            ),
        },
        "path": _MOUNT_PATH_PROPERTY,
        "description": {
            "type": "string",
            "description": "Optional human-readable description for the mount.",
        },
    },
    "required": ["backend_type", "path"],
    "additionalProperties": False,
}


VAULT_SYS_AUTH_TUNE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": _MOUNT_PATH_PROPERTY, **_TUNE_PROPERTIES},
    "required": ["path"],
    "additionalProperties": False,
}


VAULT_SYS_MOUNTS_TUNE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": _MOUNT_PATH_PROPERTY, **_TUNE_PROPERTIES},
    "required": ["path"],
    "additionalProperties": False,
}


VAULT_SYS_AUTH_ENABLE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The mount path the auth method is enabled at.",
        },
        "method_type": {
            "type": "string",
            "description": "The enabled auth method type.",
        },
        "created": {
            "type": "boolean",
            "description": (
                "True when this call enabled the method; False when it was "
                "already enabled at this path (idempotent no-op success)."
            ),
        },
    },
    "required": ["path", "method_type", "created"],
}


VAULT_SYS_MOUNTS_ENABLE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The mount path the secret engine is enabled at.",
        },
        "backend_type": {
            "type": "string",
            "description": "The enabled secret engine type.",
        },
        "created": {
            "type": "boolean",
            "description": (
                "True when this call enabled the engine; False when it was "
                "already enabled at this path (idempotent no-op success)."
            ),
        },
    },
    "required": ["path", "backend_type", "created"],
}


VAULT_SYS_AUTH_TUNE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The auth-method mount path that was tuned.",
        },
        "tuned": {
            "type": "boolean",
            "description": "Always True on success (Vault returns HTTP 204).",
        },
    },
    "required": ["path", "tuned"],
}


VAULT_SYS_MOUNTS_TUNE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The secret-engine mount path that was tuned.",
        },
        "tuned": {
            "type": "boolean",
            "description": "Always True on success (Vault returns HTTP 204).",
        },
    },
    "required": ["path", "tuned"],
}


VAULT_SYS_AUTH_ENABLE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Enable a new auth method on the Vault target (userpass, approle, "
        "jwt, kubernetes, ...). DANGEROUS and approval-gated: enabling an "
        "auth method opens a new login path into the cluster. Idempotent — "
        "re-enabling the same type at the same path returns created=False. "
        "List the existing methods (vault.sys.auth.list) first to avoid a "
        "path collision with a different type."
    ),
    "parameter_hints": {
        "method_type": "The auth method type, e.g. 'userpass' or 'approle'.",
        "path": "Where to mount it (defaults conceptually to the type name).",
        "description": "Optional human-readable mount description.",
    },
    "output_shape": (
        "{'path': <str>, 'method_type': <str>, 'created': <bool>}. "
        "created=False means it was already enabled at this path. On a "
        "type-mismatch collision or transport failure: connector_error "
        "with extras.exception_class."
    ),
}


VAULT_SYS_AUTH_TUNE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Tune an already-enabled auth method's mount config (lease TTLs, "
        "description, listing visibility). CAUTION and approval-gated: "
        "reconfigures an existing mount but does not stand up a new "
        "credential path. The method must already be enabled."
    ),
    "parameter_hints": {
        "path": "The auth-method mount path to tune, e.g. 'userpass'.",
        "default_lease_ttl": "Default lease TTL ('768h' or seconds).",
        "max_lease_ttl": "Maximum lease TTL ('768h' or seconds).",
        "description": "New human-readable description.",
        "listing_visibility": "'unauth' or 'hidden'.",
    },
    "output_shape": (
        "{'path': <str>, 'tuned': true} on success (HTTP 204). On a "
        "not-enabled path or transport failure: connector_error with "
        "extras.exception_class."
    ),
}


VAULT_SYS_MOUNTS_ENABLE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Enable a new secret engine on the Vault target (kv, transit, pki, "
        "database, ...). DANGEROUS and approval-gated: stands up a new "
        "secret-management surface. Idempotent — re-enabling the same type "
        "at the same path returns created=False. List the existing mounts "
        "(vault.sys.mounts.list) first to avoid a path collision."
    ),
    "parameter_hints": {
        "backend_type": "The secret engine type, e.g. 'kv' or 'transit'.",
        "path": "Where to mount it (defaults conceptually to the type name).",
        "description": "Optional human-readable mount description.",
    },
    "output_shape": (
        "{'path': <str>, 'backend_type': <str>, 'created': <bool>}. "
        "created=False means it was already enabled at this path. On a "
        "type-mismatch collision or transport failure: connector_error "
        "with extras.exception_class."
    ),
}


VAULT_SYS_MOUNTS_TUNE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Tune an already-enabled secret engine's mount config (lease TTLs, "
        "description, listing visibility). CAUTION and approval-gated: "
        "reconfigures an existing mount. The engine must already be enabled."
    ),
    "parameter_hints": {
        "path": "The secret-engine mount path to tune, e.g. 'secret'.",
        "default_lease_ttl": "Default lease TTL ('768h' or seconds).",
        "max_lease_ttl": "Maximum lease TTL ('768h' or seconds).",
        "description": "New human-readable description.",
        "listing_visibility": "'unauth' or 'hidden'.",
    },
    "output_shape": (
        "{'path': <str>, 'tuned': true} on success (HTTP 204). On a "
        "not-enabled path or transport failure: connector_error with "
        "extras.exception_class."
    ),
}


#: The substring Vault embeds in the HTTP 400 it returns when an
#: ``enable`` targets a path that is already mounted. Matched
#: case-insensitively so the idempotency unwrap survives a Vault-side
#: capitalisation tweak. A path-in-use 400 is the *only* error the
#: enable handlers swallow into a ``created=False`` success — every other
#: ``InvalidRequest`` (e.g. an unknown method type, a malformed config)
#: re-raises for the dispatcher's ``connector_error`` branch.
_ALREADY_IN_USE_MARKER: str = "already in use"


def _is_path_already_in_use(exc: hvac.exceptions.InvalidRequest) -> bool:
    """Return True when *exc* is Vault's "path already in use" 400.

    Vault rejects a duplicate ``enable`` with HTTP 400 and a message
    containing "path is already in use". hvac raises that as
    :class:`hvac.exceptions.InvalidRequest` carrying the server message.
    The check is substring + case-insensitive so it tolerates message
    drift without matching unrelated 400s (a bad config, an unknown
    type), which keep propagating to the dispatcher.
    """
    return _ALREADY_IN_USE_MARKER in str(exc).lower()


def _tune_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """Collect the supplied tune knobs into hvac kwargs.

    The schema already constrains the key set and value shapes; this
    just drops any knob the caller omitted so hvac's own
    ``None``-skipping defaults apply (an absent knob leaves Vault's
    current value untouched rather than resetting it).
    """
    return {key: params[key] for key in _TUNE_PROPERTIES if key in params}


async def vault_sys_auth_enable(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Enable an auth method (``POST /v1/sys/auth/<path>``).

    Op-id: ``vault.sys.auth.enable``. DANGEROUS / approval-gated.
    Delegates to hvac's
    ``sys.enable_auth_method(method_type, path, description)``. Vault
    returns HTTP 204 on success; a duplicate enable returns HTTP 400
    "path is already in use", which the handler unwraps into a
    ``created=False`` idempotent success (see the module docstring).

    Returns ``{"path", "method_type", "created"}``.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    hvac.exceptions.InvalidRequest
        Any 400 *other* than path-already-in-use (unknown type,
        malformed config). Wrapped into ``connector_error``.
    Exception
        Any other error hvac raises from the enable.
    """
    method_type: str = str(params["method_type"]).strip()
    path: str = str(params["path"]).strip()
    description: str | None = params.get("description")
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(
                client.sys.enable_auth_method,
                method_type=method_type,
                path=path,
                description=description,
            )
        except hvac.exceptions.InvalidRequest as exc:
            if not _is_path_already_in_use(exc):
                raise
            return {"path": path, "method_type": method_type, "created": False}
        return {"path": path, "method_type": method_type, "created": True}


async def vault_sys_mounts_enable(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Enable a secret engine (``POST /v1/sys/mounts/<path>``).

    Op-id: ``vault.sys.mounts.enable``. DANGEROUS / approval-gated.
    Delegates to hvac's
    ``sys.enable_secrets_engine(backend_type, path, description)``. Same
    204-success / path-in-use-idempotency contract as
    :func:`vault_sys_auth_enable`.

    Returns ``{"path", "backend_type", "created"}``.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    hvac.exceptions.InvalidRequest
        Any 400 *other* than path-already-in-use. Wrapped into
        ``connector_error``.
    Exception
        Any other error hvac raises from the enable.
    """
    backend_type: str = str(params["backend_type"]).strip()
    path: str = str(params["path"]).strip()
    description: str | None = params.get("description")
    async with _auth_vault.vault_client_for_operator(operator) as client:
        try:
            await asyncio.to_thread(
                client.sys.enable_secrets_engine,
                backend_type=backend_type,
                path=path,
                description=description,
            )
        except hvac.exceptions.InvalidRequest as exc:
            if not _is_path_already_in_use(exc):
                raise
            return {"path": path, "backend_type": backend_type, "created": False}
        return {"path": path, "backend_type": backend_type, "created": True}


async def vault_sys_auth_tune(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Tune an enabled auth method (``POST /v1/sys/auth/<path>/tune``).

    Op-id: ``vault.sys.auth.tune``. CAUTION / approval-gated. Delegates
    to hvac's ``sys.tune_auth_method(path, **knobs)`` with only the
    supplied tune knobs (an omitted knob leaves Vault's current value
    untouched). Vault returns HTTP 204 on success, so the handler
    synthesizes ``{"path", "tuned": True}`` — a reaching-here-means-
    success contract (hvac raises on a non-2xx).

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the tune (e.g. a 400 for a path that
        is not an enabled auth method).
    """
    path: str = str(params["path"]).strip()
    kwargs = _tune_kwargs(params)
    async with _auth_vault.vault_client_for_operator(operator) as client:
        await asyncio.to_thread(client.sys.tune_auth_method, path=path, **kwargs)
        return {"path": path, "tuned": True}


async def vault_sys_mounts_tune(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Tune an enabled secret engine (``POST /v1/sys/mounts/<path>/tune``).

    Op-id: ``vault.sys.mounts.tune``. CAUTION / approval-gated.
    Delegates to hvac's ``sys.tune_mount_configuration(path, **knobs)``.
    Same 204-success contract as :func:`vault_sys_auth_tune`.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the tune.
    """
    path: str = str(params["path"]).strip()
    kwargs = _tune_kwargs(params)
    async with _auth_vault.vault_client_for_operator(operator) as client:
        await asyncio.to_thread(client.sys.tune_mount_configuration, path=path, **kwargs)
        return {"path": path, "tuned": True}
