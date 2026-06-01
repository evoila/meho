# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault ``sys`` ACL-policy ops — typed handlers, schemas, LLM hints.

Op-ids (G3.15-T2 #1410), all under the ``sys`` operation group and
registered from :func:`meho_backplane.connectors.vault.ops_sys.register_vault_sys_typed_operations`:

* ``vault.sys.policy.read`` — read one policy body (``GET /v1/sys/policy/<name>``).
* ``vault.sys.policy.list`` — list configured policy names (``GET /v1/sys/policy``).
* ``vault.sys.policy.write`` — create/replace a policy
  (``PUT /v1/sys/policy/<name>``).
* ``vault.sys.policy.delete`` — delete a policy (``DELETE /v1/sys/policy/<name>``).

``read`` / ``list`` are ``safety_level='safe'`` / ``requires_approval=False``.
``write`` / ``delete`` are ``safety_level='dangerous'`` /
``requires_approval=True`` — a bad HCL body can lock everyone out or
silently widen access, the highest-blast-radius Vault mutation, so they
belong behind the approval gate (the issue's "dumb substrate, smart
agent" rule: this layer is a thin pass-through; verb-layer linting of the
policy body is out of scope — Vault itself rejects malformed HCL).

Broadcast ``op_class`` (via
:func:`~meho_backplane.broadcast.events.classify_op`): ``policy.list`` →
``read`` (``.list`` suffix); ``policy.read`` → ``other`` (its only param
is the policy name; ``.read`` is deliberately not a read-suffix, matching
the ``vault.auth.*.read`` precedent); ``policy.write`` / ``policy.delete``
→ ``write`` (the ``.write`` / ``.delete`` suffixes redact the HCL body
rather than broadcasting it in full).

This module is split out of ``ops_sys.py`` purely to keep each file under
the repository's 600-line size budget; the ``register_typed_operation``
calls stay in ``ops_sys.py`` so the whole ``sys`` group registers from
one place. The handlers follow the exact shape of the authenticated sys
read ops: ``(operator, target, params)``, the operator JWT forwarded via
:func:`~meho_backplane.auth.vault.vault_client_for_operator`, and the
synchronous hvac call offloaded with :func:`asyncio.to_thread` because
hvac is ``requests``-based and FastAPI does not auto-offload blocking I/O
inside an ``async def``. Handlers **raise** on failure; the dispatcher's
``connector_error`` branch records the exception class in
``extras["exception_class"]`` (no raw traceback to the agent).
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator

__all__ = [
    "VAULT_SYS_POLICY_DELETE_LLM_INSTRUCTIONS",
    "VAULT_SYS_POLICY_DELETE_PARAMETER_SCHEMA",
    "VAULT_SYS_POLICY_DELETE_RESPONSE_SCHEMA",
    "VAULT_SYS_POLICY_LIST_LLM_INSTRUCTIONS",
    "VAULT_SYS_POLICY_LIST_PARAMETER_SCHEMA",
    "VAULT_SYS_POLICY_LIST_RESPONSE_SCHEMA",
    "VAULT_SYS_POLICY_READ_LLM_INSTRUCTIONS",
    "VAULT_SYS_POLICY_READ_PARAMETER_SCHEMA",
    "VAULT_SYS_POLICY_READ_RESPONSE_SCHEMA",
    "VAULT_SYS_POLICY_WRITE_LLM_INSTRUCTIONS",
    "VAULT_SYS_POLICY_WRITE_PARAMETER_SCHEMA",
    "VAULT_SYS_POLICY_WRITE_RESPONSE_SCHEMA",
    "vault_sys_policy_delete",
    "vault_sys_policy_list",
    "vault_sys_policy_read",
    "vault_sys_policy_write",
]


#: Shared ``name`` schema fragment for the three name-addressed policy
#: ops. ``pattern="^(?=.*\\S)[^/]+$"`` mirrors the KV-v2 ``mount``
#: fragment in :mod:`meho_backplane.connectors.vault.ops`: the
#: ``(?=.*\S)`` lookahead makes an all-whitespace value a validation-time
#: ``invalid_params`` failure rather than a value that ``.strip()``s to
#: ``""`` and degrades to a runtime ``connector_error``; ``[^/]+``
#: rejects a slash-bearing name (Vault policy names are flat handles).
_POLICY_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "pattern": r"^(?=.*\S)[^/]+$",
    "description": (
        "The ACL policy name (a flat handle, e.g. 'meho-mcp'). Vault "
        "lower-cases policy names server-side."
    ),
}


VAULT_SYS_POLICY_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": _POLICY_NAME_PROPERTY},
    "required": ["name"],
    "additionalProperties": False,
}


#: ``vault.sys.policy.list`` takes no parameter — it returns every
#: configured policy name. A stray key is a validation error.
VAULT_SYS_POLICY_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


VAULT_SYS_POLICY_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _POLICY_NAME_PROPERTY,
        "policy": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The policy body as HCL or JSON. REPLACES any existing "
                "policy of the same name in full (not a merge). Vault "
                "itself rejects a malformed body; this op does not "
                "pre-validate beyond requiring a non-empty string."
            ),
        },
    },
    "required": ["name", "policy"],
    "additionalProperties": False,
}


VAULT_SYS_POLICY_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": _POLICY_NAME_PROPERTY},
    "required": ["name"],
    "additionalProperties": False,
}


VAULT_SYS_POLICY_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The policy name echoed back by Vault.",
        },
        "rules": {
            "type": ["string", "null"],
            "description": (
                "The policy body (HCL or JSON) Vault has stored for this "
                "name, or null when Vault returned no body."
            ),
        },
    },
    "required": ["name", "rules"],
}


VAULT_SYS_POLICY_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The configured ACL policy names (always includes the "
                "built-in 'default' and 'root' policies)."
            ),
        },
    },
    "required": ["policies"],
}


VAULT_SYS_POLICY_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The policy name that was created or replaced.",
        },
        "written": {
            "type": "boolean",
            "description": "Always True on success (Vault returns HTTP 204).",
        },
    },
    "required": ["name", "written"],
}


VAULT_SYS_POLICY_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The policy name that was deleted.",
        },
        "deleted": {
            "type": "boolean",
            "description": (
                "Always True on success (Vault returns HTTP 204; deleting "
                "a non-existent policy is a no-op success on Vault's side)."
            ),
        },
    },
    "required": ["name", "deleted"],
}


VAULT_SYS_POLICY_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read the ACL policy body (HCL/JSON rules) for a named Vault "
        "policy. Use to inspect what a policy grants before editing it, "
        "or to answer 'what does the meho-mcp policy allow?'. Read-only; "
        "returns policy rules, not secret values."
    ),
    "parameter_hints": {
        "name": "The policy name, e.g. 'meho-mcp' or 'default'.",
    },
    "output_shape": (
        "{'name': <str>, 'rules': <str|null>}. On a missing policy or "
        "transport failure: a connector_error OperationResult with "
        "extras.exception_class."
    ),
}


VAULT_SYS_POLICY_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List the names of every configured ACL policy on the Vault "
        "target. Use to discover which policies exist before reading or "
        "editing one. Read-only; returns names only, no policy bodies."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'policies': [<name>, ...]} including the built-in 'default' / "
        "'root'. On failure: connector_error with extras.exception_class."
    ),
}


VAULT_SYS_POLICY_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a new ACL policy or replace an existing one in full. "
        "DANGEROUS and approval-gated: a bad HCL body can lock operators "
        "out or silently widen access. Always read the current body "
        "(vault.sys.policy.read) first and diff your change. Never use "
        "for the built-in 'root' policy."
    ),
    "parameter_hints": {
        "name": "The policy name to create or replace.",
        "policy": (
            "The full policy body as HCL or JSON. This REPLACES the "
            "existing body — it is not a merge."
        ),
    },
    "output_shape": (
        "{'name': <str>, 'written': true} on success (HTTP 204). On a "
        "malformed body or transport failure: connector_error with "
        "extras.exception_class."
    ),
}


VAULT_SYS_POLICY_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete an ACL policy by name. DANGEROUS and approval-gated: "
        "removing a policy immediately revokes the access it granted to "
        "every token/entity bound to it. Confirm no live identity depends "
        "on it first. Never delete the built-in 'default' / 'root' "
        "policies."
    ),
    "parameter_hints": {
        "name": "The policy name to delete.",
    },
    "output_shape": (
        "{'name': <str>, 'deleted': true} on success (HTTP 204; deleting "
        "a non-existent policy is a no-op success). On failure: "
        "connector_error with extras.exception_class."
    ),
}


def _extract_rules(response: Any) -> str | None:
    """Pull the policy body out of an hvac ``read_policy`` response.

    Vault's legacy ``GET /v1/sys/policy/<name>`` endpoint returns the
    rules both at the envelope top level (``{"name", "rules"}``) and,
    on newer servers, nested under ``data`` (``{"data": {"name",
    "rules"|"policy"}}``). hvac forwards the JSON verbatim. Probe the
    nested ``data`` object first (the modern shape), then the top
    level, accepting either the ``rules`` or ``policy`` key spelling.
    """
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, dict):
        nested = data.get("rules", data.get("policy"))
        if nested is not None:
            return str(nested)
    top = response.get("rules", response.get("policy"))
    return None if top is None else str(top)


def _extract_policy_names(response: Any) -> list[str]:
    """Pull the policy-name list out of an hvac ``list_policies`` response.

    Like :func:`_extract_rules`, ``GET /v1/sys/policy`` returns the
    ``policies`` array both top-level and under ``data`` depending on
    the server version. Prefer the nested ``data.policies`` (modern
    shape), fall back to the top-level key.
    """
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("policies"), list):
        return [str(name) for name in data["policies"]]
    policies = response.get("policies")
    if isinstance(policies, list):
        return [str(name) for name in policies]
    return []


async def vault_sys_policy_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one ACL policy body (``GET /v1/sys/policy/<name>``).

    Op-id: ``vault.sys.policy.read``. Safe / read-only. Forwards the
    operator JWT and offloads the blocking ``sys.read_policy`` call.

    Returns ``{"name": <name>, "rules": <body|None>}``.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the policy read.
    """
    name: str = str(params["name"]).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.sys.read_policy, name=name)
        return {"name": name, "rules": _extract_rules(response)}


async def vault_sys_policy_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List configured ACL policy names (``GET /v1/sys/policy``).

    Op-id: ``vault.sys.policy.list``. Safe / read-only.

    Returns ``{"policies": [<name>, ...]}``.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the policy list.
    """
    _ = params  # schema-validated empty object; no inputs to extract.
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.sys.list_policies)
        return {"policies": _extract_policy_names(response)}


async def vault_sys_policy_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or replace an ACL policy (``PUT /v1/sys/policy/<name>``).

    Op-id: ``vault.sys.policy.write``. DANGEROUS / approval-gated.
    Delegates to hvac's ``sys.create_or_update_policy(name, policy)``;
    the ``policy`` body is HCL or JSON and replaces any existing policy
    of the same name in full. Vault returns HTTP 204 on success (no
    body), so the handler synthesizes ``{"name", "written": True}`` —
    a reaching-here-means-success contract (hvac raises on a non-2xx).

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the policy write (e.g. a malformed
        HCL body Vault rejects with a 400).
    """
    name: str = str(params["name"]).strip()
    policy: str = params["policy"]
    async with _auth_vault.vault_client_for_operator(operator) as client:
        await asyncio.to_thread(
            client.sys.create_or_update_policy,
            name=name,
            policy=policy,
        )
        return {"name": name, "written": True}


async def vault_sys_policy_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete an ACL policy (``DELETE /v1/sys/policy/<name>``).

    Op-id: ``vault.sys.policy.delete``. DANGEROUS / approval-gated.
    Delegates to hvac's ``sys.delete_policy(name)``. Vault returns HTTP
    204 (deleting a non-existent policy is a no-op success), so the
    handler synthesizes ``{"name", "deleted": True}``.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    Exception
        Any error hvac raises from the policy delete.
    """
    name: str = str(params["name"]).strip()
    async with _auth_vault.vault_client_for_operator(operator) as client:
        await asyncio.to_thread(client.sys.delete_policy, name=name)
        return {"name": name, "deleted": True}
