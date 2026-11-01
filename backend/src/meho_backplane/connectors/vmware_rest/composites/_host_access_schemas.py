# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the host local-user and Active Directory changes.

``vmware.composite.host.local_user.{create,delete,password.set}`` and
``vmware.composite.host.ad.{join,leave}``. The handlers are in
:mod:`._host_local_users` and :mod:`._host_ad`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._vcenter_deploy_schemas import (
    ISSUE_SCHEMA,
    secret_ref_fields,
)
from meho_backplane.connectors.vmware_rest.host_access import (
    AD_STATE_PROPERTIES,
    HOST_PARAMETER,
)

__all__ = [
    "AD_JOIN_LLM_INSTRUCTIONS",
    "AD_JOIN_PARAMETER_SCHEMA",
    "AD_JOIN_RESPONSE_SCHEMA",
    "AD_LEAVE_LLM_INSTRUCTIONS",
    "AD_LEAVE_PARAMETER_SCHEMA",
    "AD_LEAVE_RESPONSE_SCHEMA",
    "LOCAL_USER_CREATE_LLM_INSTRUCTIONS",
    "LOCAL_USER_CREATE_PARAMETER_SCHEMA",
    "LOCAL_USER_CREATE_RESPONSE_SCHEMA",
    "LOCAL_USER_DELETE_LLM_INSTRUCTIONS",
    "LOCAL_USER_DELETE_PARAMETER_SCHEMA",
    "LOCAL_USER_DELETE_RESPONSE_SCHEMA",
    "LOCAL_USER_PASSWORD_SET_LLM_INSTRUCTIONS",
    "LOCAL_USER_PASSWORD_SET_PARAMETER_SCHEMA",
    "LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA",
]

_NULL_STR: Final = ["string", "null"]

#: An ESXi user or account name: no spaces, slashes, backslashes or colons.
_ACCOUNT_NAME_PATTERN: Final = r"^[^\s/\\:]+$(?!\n)"

_USER_NAME: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "pattern": _ACCOUNT_NAME_PATTERN,
    "description": "Login name of the local user on the ESXi host.",
}

_VAULT_EXAMPLE: Final = "<vault-path-of-the-password>"

_FAULT_PROPERTIES: dict[str, Any] = {
    "error_type": {
        "type": _NULL_STR,
        "description": "The vendor fault type on failed, such as AlreadyExists or InvalidLogin.",
    },
    "error_message": {
        "type": _NULL_STR,
        "description": "The vendor fault text on failed; never holds a password.",
    },
    "guidance": {"type": _NULL_STR, "description": "What to do next, in plain words."},
    "issues": {
        "type": "array",
        "description": "An error entry when the change failed or is not confirmed.",
        "items": ISSUE_SCHEMA,
    },
}

_USER_ROW: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "The user as MEHO read it last; null when the host has no such user.",
    "properties": {
        "user": {"type": "string", "description": "Login name."},
        "full_name": {"type": _NULL_STR, "description": "Full name or description."},
        "uid": {"type": ["integer", "null"], "description": "POSIX user id."},
        "shell_access": {
            "type": ["boolean", "null"],
            "description": "Whether the user may use the ESXi shell.",
        },
    },
}


def _user_response(statuses: list[str], text: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses, "description": text},
            "user_name": {"type": "string", "description": "The user the call named."},
            "user": _USER_ROW,
            "target_product": {"type": _NULL_STR, "description": "On unsupported_host_target."},
            **_FAULT_PROPERTIES,
        },
        "required": ["status", "user_name"],
    }


_TARGET_REFUSALS: Final = ["needs_esxi_target", "unsupported_host_target"]

_ESXI_PRECONDITION: Final = (
    "The target must be the ESXi host itself (list_targets), not a vCenter: local users "
    "live on the host. "
)

# ===========================================================================
# host.local_user.create
# ===========================================================================

LOCAL_USER_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_name": _USER_NAME,
        "description": {
            "type": "string",
            "maxLength": 255,
            "description": "Full name or a short note about the user (optional).",
        },
        **secret_ref_fields("password", "the password of the new user"),
    },
    "required": ["user_name", "password_secret_ref"],
    "additionalProperties": False,
    "examples": [
        {
            "user_name": "<user-name>",
            "description": "<what-the-account-is-for>",
            "password_secret_ref": _VAULT_EXAMPLE,
        }
    ],
}

LOCAL_USER_CREATE_RESPONSE_SCHEMA = _user_response(
    ["created", "already_exists", "not_verified", "failed", *_TARGET_REFUSALS],
    "created: the user exists now. already_exists: nothing was sent. failed: the host "
    "refused (see error_type). not_verified: no error, but the read-back has no user.",
)

LOCAL_USER_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to add a local user to one ESXi host, for example a service or break-glass "
        "account. The password comes from Vault."
    ),
    "preconditions": (
        _ESXI_PRECONDITION + "Store the password in Vault first and pass its path in "
        "password_secret_ref; never pass the password itself. The password must meet the "
        "host's password rules. The call waits for a second person's approval."
    ),
    "parameter_hints": {
        "user_name": "The new login name, e.g. 'svc-backup'.",
        "password_secret_ref": "Vault path of the secret; the key is 'password' by default.",
    },
    "output_shape": (
        "{status, user_name, user: {user, full_name, uid, shell_access}, error_type, "
        "error_message, guidance}. status created means the read-back found the user."
    ),
    "next_step": (
        "The new user has no access role on the host yet. Check it with "
        "vmware.host.local_user.list. On failed, fix the cause error_type names (for example "
        "a password the host refuses) and call again."
    ),
}

# ===========================================================================
# host.local_user.password.set
# ===========================================================================

LOCAL_USER_PASSWORD_SET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_name": _USER_NAME,
        **secret_ref_fields("password", "the new password of the user"),
    },
    "required": ["user_name", "password_secret_ref"],
    "additionalProperties": False,
    "examples": [{"user_name": "<user-name>", "password_secret_ref": _VAULT_EXAMPLE}],
}

LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA = _user_response(
    ["password_set", "not_found", "failed", *_TARGET_REFUSALS],
    "password_set: the host took the new password. not_found: no such user, nothing was "
    "sent. failed: the host refused (see error_type).",
)

LOCAL_USER_PASSWORD_SET_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to change the password of a local user on one ESXi host, for example in a "
        "password rotation. The new password comes from Vault."
    ),
    "preconditions": (
        _ESXI_PRECONDITION + "Store the new password in Vault first and pass its path in "
        "password_secret_ref. If MEHO itself logs in to this host with this user, update the "
        "target's own Vault secret too, or MEHO cannot log in any more. The call waits for a "
        "second person's approval."
    ),
    "parameter_hints": {
        "user_name": "The login name, e.g. 'root'.",
        "password_secret_ref": "Vault path of the secret; the key is 'password' by default.",
    },
    "output_shape": (
        "{status, user_name, user: {user, full_name, uid, shell_access}, error_type, "
        "error_message, guidance}. MEHO cannot read a password back; password_set means the "
        "host took it without an error and the user is still there."
    ),
    "next_step": (
        "On not_found, check the name with vmware.host.local_user.list. On failed, fix the "
        "cause error_type names (for example a password the host refuses) and call again."
    ),
}

# ===========================================================================
# host.local_user.delete
# ===========================================================================

LOCAL_USER_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"user_name": _USER_NAME},
    "required": ["user_name"],
    "additionalProperties": False,
    "examples": [{"user_name": "<user-name>"}],
}

LOCAL_USER_DELETE_RESPONSE_SCHEMA = _user_response(
    ["deleted", "unchanged", "protected", "still_present", "failed", *_TARGET_REFUSALS],
    "deleted: the user is gone. unchanged: there was no such user. protected: MEHO never "
    "deletes root. failed: the host refused (see error_type).",
)

LOCAL_USER_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to remove a local user from one ESXi host, for example an old service account "
        "found in an audit."
    ),
    "preconditions": (
        _ESXI_PRECONDITION + "This is a destructive change: call preview_operation with the "
        "same params first and pass its preview_hash. A person must run it, and a second "
        "person approves. MEHO refuses to delete root. Do not delete the user MEHO logs in "
        "with on this host."
    ),
    "parameter_hints": {"user_name": "The login name, e.g. 'svc-old'."},
    "output_shape": (
        "{status, user_name, user, error_type, error_message, guidance}. user is the row "
        "read before the delete. status deleted means the read-back no longer finds the user."
    ),
    "next_step": "Check the result with vmware.host.local_user.list.",
}

# ===========================================================================
# host.ad.join / host.ad.leave
# ===========================================================================

_HOST_REFUSALS: Final = [
    "host_required",
    "host_not_found",
    "ambiguous_host",
    "unsupported_host_target",
    "config_manager_unreadable",
]


def _ad_response(statuses: list[str], text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses, "description": text},
            **AD_STATE_PROPERTIES,
            "task": {
                "type": _NULL_STR,
                "description": "The vendor task id of the change; null when nothing was sent.",
            },
            "candidate_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Host moids on ambiguous_host.",
            },
            "target_product": {"type": _NULL_STR, "description": "On unsupported_host_target."},
            **extra,
            **_FAULT_PROPERTIES,
        },
        "required": ["status"],
    }


AD_JOIN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": HOST_PARAMETER,
        "domain": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "pattern": r"^[^\s/\\]+$(?!\n)",
            "description": "The Active Directory domain to join, such as '<ad-domain>'.",
        },
        "user_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "A domain account that may add computers to the domain.",
        },
        **secret_ref_fields("password", "the password of user_name in the domain"),
    },
    "required": ["domain", "user_name", "password_secret_ref"],
    "additionalProperties": False,
    "examples": [
        {
            "host": "<host-name>",
            "domain": "<ad-domain>",
            "user_name": "<domain-join-account>",
            "password_secret_ref": _VAULT_EXAMPLE,
        }
    ],
}

AD_JOIN_RESPONSE_SCHEMA = _ad_response(
    [
        "joined",
        "unchanged",
        "joined_other_domain",
        "not_supported",
        "not_verified",
        "failed",
        "timeout",
        *_HOST_REFUSALS,
    ],
    "joined: the host is in the domain now. unchanged: it already was, nothing was sent. "
    "joined_other_domain: leave that domain first. failed: the host refused (see "
    "error_type). timeout: MEHO stopped waiting; the join may still run.",
    requested_domain={"type": _NULL_STR, "description": "The domain the call asked for."},
)

AD_JOIN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to join one ESXi host to an Active Directory domain, so domain users can log "
        "in to it. The password of the join account comes from Vault."
    ),
    "preconditions": (
        "The host must reach a domain controller (DNS and time must be right: check with "
        "vmware.host.config.show). It must not be in another domain. Store the password of "
        "the join account in Vault and pass its path in password_secret_ref; never the "
        "password itself. The call waits for a second person's approval."
    ),
    "parameter_hints": {
        "host": "Host name or moid on a vCenter target; not needed on an ESXi target.",
        "domain": "The domain name, e.g. 'corp.example'.",
        "user_name": "The join account, e.g. 'svc-join'.",
    },
    "output_shape": (
        "{status, host, joined, domain, trusted_domains, membership_status, requested_domain, "
        "task, error_type, error_message, guidance}. The host fields are as read after the "
        "join."
    ),
    "next_step": (
        "Check the membership with vmware.host.ad.status. On failed, error_type names the "
        "cause: InvalidLogin (wrong account or password), DomainNotFound, BlockedByFirewall, "
        "InvalidState (already in a domain)."
    ),
}

AD_LEAVE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": HOST_PARAMETER,
        "force": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also delete the permissions that domain users and groups have on this host "
                "and its objects. Without it, the host refuses to leave while such "
                "permissions exist."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{"host": "<host-name>", "force": False}],
}

AD_LEAVE_RESPONSE_SCHEMA = _ad_response(
    ["left", "unchanged", "not_supported", "not_verified", "failed", "timeout", *_HOST_REFUSALS],
    "left: the host is in no domain now. unchanged: it was in none, nothing was sent. "
    "failed: the host refused (see error_type). timeout: MEHO stopped waiting.",
)

AD_LEAVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": "Call to take one ESXi host out of its Active Directory domain.",
    "preconditions": (
        "After the leave, domain users cannot log in to the host. Without force, the host "
        "refuses while domain users or groups have permissions on it; with force, those "
        "permissions are deleted. The call waits for a second person's approval."
    ),
    "parameter_hints": {
        "host": "Host name or moid on a vCenter target; not needed on an ESXi target.",
        "force": "true deletes the domain permissions too. Default false.",
    },
    "output_shape": (
        "{status, host, joined, domain, trusted_domains, membership_status, task, error_type, "
        "error_message, guidance}. The host fields are as read after the leave."
    ),
    "next_step": (
        "Check with vmware.host.ad.status. On failed because domain permissions exist, remove "
        "them or call again with force true."
    ),
}
