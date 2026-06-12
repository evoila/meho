# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema + ``llm_instructions`` for the Vault *identity* ops (G3.15-T4).

Sibling of :mod:`meho_backplane.connectors.vault.ops_token_schemas`;
split out so each group's schema data stays under the file-size budget.
Pure data only -- JSON Schema 2020-12 ``parameter_schema`` /
``response_schema`` documents and the structured ``llm_instructions``
payload the meta-tools (G0.6-T8) inline verbatim when an LLM is choosing
an op.

Mirrors the schema/``llm_instructions`` shape of the sibling auth
modules (``ops_auth_schemas`` / ``ops_auth_write_schemas``): ``minLength=1``
/ ``pattern="\\S"`` / ``additionalProperties=False`` input discipline and
a when-to-use + parameter-hint + output-shape ``llm_instructions`` block
per op. The identity entity/group/alias objects carry no secret material;
policy bindings and group membership are the privilege signal (the
load-bearing reason the writes require approval), not secrets.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_IDENTITY_ENTITY_ALIAS_WRITE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_ENTITY_ALIAS_WRITE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_ENTITY_ALIAS_WRITE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_ENTITY_READ_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_ENTITY_READ_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_ENTITY_READ_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_ENTITY_WRITE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_ENTITY_WRITE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_ENTITY_WRITE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_GROUP_DELETE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_GROUP_DELETE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_GROUP_DELETE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_GROUP_READ_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_GROUP_READ_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_GROUP_READ_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_GROUP_WRITE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_GROUP_WRITE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_GROUP_WRITE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_LIST_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_LIST_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_LIST_RESPONSE_SCHEMA",
]


_POLICIES_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "description": (
        "Vault policy names bound to this identity object. This is the "
        "privilege assignment -- the load-bearing reason every write op "
        "in this group requires approval. Omit to leave unchanged on an "
        "update (Vault treats the field as optional)."
    ),
}

_METADATA_PROPERTY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Arbitrary string key/value metadata stamped on the object. Not "
        "secret material; echoed back verbatim by reads."
    ),
}


# --- identity.entity.write -------------------------------------------------

VAULT_IDENTITY_ENTITY_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Entity name. Forwarded verbatim to hvac's name= argument.",
        },
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Existing entity id to update. Omit to create a new entity "
                "(Vault mints the id and returns it)."
            ),
        },
        "policies": _POLICIES_PROPERTY,
        "metadata": _METADATA_PROPERTY,
        "disabled": {
            "type": "boolean",
            "description": "Whether the entity is disabled (its aliases cannot authenticate).",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

VAULT_IDENTITY_ENTITY_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The entity created or updated."},
        "entity_id": {
            "type": ["string", "null"],
            "description": (
                "The entity id. Present on a create (Vault mints and returns "
                "it) and echoed on an update; null when Vault returns no body "
                "(a pure update)."
            ),
        },
        "written": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["name", "written"],
    "description": "Confirms the create/update; carries the entity id on a create.",
}

VAULT_IDENTITY_ENTITY_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a new identity entity or update an existing one's name, "
        "policies, metadata, or disabled state. DANGEROUS, requires "
        "approval: binding policies is a privilege assignment. An entity "
        "is the canonical identity a human/service maps onto across auth "
        "backends; aliases (vault.identity.entity_alias.write) link a "
        "specific auth-method login to it."
    ),
    "parameter_hints": {
        "name": "Required. The entity name.",
        "entity_id": "Optional. Supply to update a specific entity; omit to create.",
        "policies": "Optional. Policy names bound to the entity (privilege assignment).",
        "metadata": "Optional. String key/value metadata.",
        "disabled": "Optional. Set true to disable the entity (its aliases cannot log in).",
    },
    "output_shape": (
        "On success: {'name': ..., 'entity_id': ..., 'written': true}. "
        "entity_id is the minted id on a create. On failure: a "
        "connector_error OperationResult with extras.exception_class."
    ),
}


# --- identity.entity_alias.write -------------------------------------------

VAULT_IDENTITY_ENTITY_ALIAS_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Alias name -- the login identifier on the source auth method "
                "(e.g. the username, the OIDC subject)."
            ),
        },
        "canonical_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Entity id this alias maps onto (the entity's canonical id).",
        },
        "mount_accessor": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Accessor of the auth-method mount the alias belongs to "
                "(from sys.auth.list / 'vault auth list -detailed')."
            ),
        },
        "alias_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Existing alias id to update. Omit to create a new alias.",
        },
    },
    "required": ["name", "canonical_id", "mount_accessor"],
    "additionalProperties": False,
}

VAULT_IDENTITY_ENTITY_ALIAS_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The alias name."},
        "canonical_id": {"type": "string", "description": "The entity id the alias maps onto."},
        "alias_id": {
            "type": ["string", "null"],
            "description": "The alias id (minted on a create); null when Vault returns no body.",
        },
        "written": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["name", "canonical_id", "written"],
    "description": "Confirms the alias create/update; carries the alias id on a create.",
}

VAULT_IDENTITY_ENTITY_ALIAS_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Link a specific auth-method login (a username, an OIDC subject) "
        "to an identity entity by creating or updating an alias. "
        "DANGEROUS, requires approval: an alias lets that login inherit "
        "the entity's policies. Needs the mount accessor of the source "
        "auth method (read it from the 'sys' group's auth-method listing)."
    ),
    "parameter_hints": {
        "name": "Required. The login identifier on the source auth method.",
        "canonical_id": "Required. The entity id this alias maps onto.",
        "mount_accessor": "Required. The auth-method mount accessor (from sys.auth.list).",
        "alias_id": "Optional. Supply to update a specific alias; omit to create.",
    },
    "output_shape": (
        "On success: {'name': ..., 'canonical_id': ..., 'alias_id': ..., "
        "'written': true}. On failure: a connector_error OperationResult."
    ),
}


# --- identity.group.write --------------------------------------------------

VAULT_IDENTITY_GROUP_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Group name. Forwarded verbatim to hvac's name= argument.",
        },
        "group_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Existing group id to update. Omit to create a new group.",
        },
        "group_type": {
            "type": "string",
            "enum": ["internal", "external"],
            "default": "internal",
            "description": (
                "Group type. 'internal' groups list explicit member entities/groups; "
                "'external' groups are populated by an auth method's group claims."
            ),
        },
        "policies": _POLICIES_PROPERTY,
        "metadata": _METADATA_PROPERTY,
        "member_entity_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Entity ids that are members of this internal group -- the "
                "membership-as-privilege plumbing. Ignored for external groups."
            ),
        },
        "member_group_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Child group ids nested under this internal group.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

VAULT_IDENTITY_GROUP_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The group created or updated."},
        "group_id": {
            "type": ["string", "null"],
            "description": "The group id (minted on a create); null when Vault returns no body.",
        },
        "written": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["name", "written"],
    "description": "Confirms the group create/update; carries the group id on a create.",
}

VAULT_IDENTITY_GROUP_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create or update an identity group, including its policies and "
        "member entities/groups. DANGEROUS, requires approval: group "
        "membership is privilege plumbing -- adding an entity to a "
        "policy-bearing group grants it that policy. Use group_type "
        "'internal' for explicit membership, 'external' for "
        "auth-method-claim-driven membership."
    ),
    "parameter_hints": {
        "name": "Required. The group name.",
        "group_id": "Optional. Supply to update a specific group; omit to create.",
        "group_type": "Optional. 'internal' (default) or 'external'.",
        "policies": "Optional. Policy names bound to the group (privilege assignment).",
        "metadata": "Optional. String key/value metadata.",
        "member_entity_ids": "Optional. Member entity ids (internal groups only).",
        "member_group_ids": "Optional. Child group ids (internal groups only).",
    },
    "output_shape": (
        "On success: {'name': ..., 'group_id': ..., 'written': true}. On "
        "failure: a connector_error OperationResult."
    ),
}


# --- identity.group.delete -------------------------------------------------

VAULT_IDENTITY_GROUP_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Group name to delete. Forwarded verbatim to hvac's name= argument.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

VAULT_IDENTITY_GROUP_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The group deleted."},
        "deleted": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["name", "deleted"],
    "description": "Confirms the group deletion. Idempotent: deleting a missing group succeeds.",
}

VAULT_IDENTITY_GROUP_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete an identity group by name, removing its policy bindings "
        "from every member. DANGEROUS, requires approval: an irreversible "
        "privilege change. Idempotent -- deleting a non-existent group is "
        "a no-op success. This op deletes ONE named group; there is no "
        "bulk-delete verb by design."
    ),
    "parameter_hints": {
        "name": "Required. The group name to delete.",
    },
    "output_shape": (
        "On success: {'name': ..., 'deleted': true}. On failure: a connector_error OperationResult."
    ),
}


# --- identity.entity.read --------------------------------------------------

VAULT_IDENTITY_ENTITY_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "The entity id to read (from a create response or a list).",
        },
    },
    "required": ["entity_id"],
    "additionalProperties": False,
}

VAULT_IDENTITY_ENTITY_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The entity's config dict (name, policies, metadata, aliases, "
        "disabled), unwrapped from Vault's {'data': {...}} envelope."
    ),
    "additionalProperties": True,
}

VAULT_IDENTITY_ENTITY_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read one identity entity's config by id -- its name, bound "
        "policies, metadata, aliases, and disabled state. Read-only; "
        "registered safe (no approval) even though the lookup is an HTTP "
        "POST, so a create-if-absent flow does not stall on approval."
    ),
    "parameter_hints": {
        "entity_id": "Required. The entity id (from a create response or a list).",
    },
    "output_shape": (
        "On success: the entity's config dict. On failure: a "
        "connector_error OperationResult (InvalidPath when the id does "
        "not exist)."
    ),
}


# --- identity.group.read ---------------------------------------------------

VAULT_IDENTITY_GROUP_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "The group name to read.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

VAULT_IDENTITY_GROUP_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The group's config dict (name, type, policies, metadata, member "
        "entity/group ids), unwrapped from Vault's {'data': {...}} envelope."
    ),
    "additionalProperties": True,
}

VAULT_IDENTITY_GROUP_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read one identity group's config by name -- its type, bound "
        "policies, metadata, and member entity/group ids. Read-only; "
        "registered safe (no approval) so a create-if-absent flow does "
        "not stall on approval."
    ),
    "parameter_hints": {
        "name": "Required. The group name.",
    },
    "output_shape": (
        "On success: the group's config dict. On failure: a "
        "connector_error OperationResult (InvalidPath when the group does "
        "not exist)."
    ),
}


# --- identity.list ---------------------------------------------------------

VAULT_IDENTITY_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["entities", "groups"],
            "default": "groups",
            "description": (
                "Which identity collection to enumerate by id: 'entities' "
                "lists entity ids, 'groups' lists group ids."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

VAULT_IDENTITY_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": "The collection enumerated ('entities' / 'groups').",
        },
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The ids in the collection. Empty list when the collection is empty.",
        },
    },
    "required": ["kind", "keys"],
    "description": "The list of entity or group ids. Always carries a 'keys' list.",
}

VAULT_IDENTITY_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Enumerate identity entity ids or group ids. Read-only; "
        "registered safe (no approval). Pick the collection with 'kind' "
        "('groups' by default). Pair with vault.identity.entity.read / "
        "vault.identity.group.read to inspect one object's config."
    ),
    "parameter_hints": {
        "kind": "Optional. 'groups' (default) or 'entities'.",
    },
    "output_shape": (
        "On success: {'kind': ..., 'keys': [...]} -- the ids in the "
        "collection. An empty collection yields {'keys': []}."
    ),
}
