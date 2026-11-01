# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the four typed operations for the vCenter appliance's local accounts.

A local account is an operating-system account of the vCenter appliance
itself, for example ``root``. The handlers are in
:mod:`.appliance_local_accounts`; the password handling is in
:mod:`.appliance_local_account_password`.

* ``vmware.appliance.local_account.list`` and ``.get`` are ``safe`` reads.
* ``vmware.appliance.local_account.password.set`` and ``.policy.set`` are
  ``dangerous`` changes with ``requires_approval=True``: a person's call waits
  until a second person approves it, and an agent needs a grant first.

Imported at the bottom of :mod:`.typed_ops` (after :class:`VmwareTypedOp` is
defined), like the other typed-op modules.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    DEFAULT_PASSWORD_KEY,
    PASSWORD_SET_OP_ID,
)
from meho_backplane.connectors.vmware_rest.appliance_local_accounts import (
    ACCOUNT_FIELDS,
    DAYS_MAX,
    DAYS_MIN,
    GET_OP_ID,
    LIST_OP_ID,
    POLICY_FIELDS,
    POLICY_SET_OP_ID,
    USERNAME_MAX_LENGTH,
    USERNAME_PATTERN,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

__all__ = [
    "APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY",
    "APPLIANCE_LOCAL_ACCOUNTS_OPS",
    "APPLIANCE_LOCAL_ACCOUNTS_WHEN_TO_USE",
    "VMWARE_APPLIANCE_LOCAL_ACCOUNT_GET_OP",
    "VMWARE_APPLIANCE_LOCAL_ACCOUNT_LIST_OP",
    "VMWARE_APPLIANCE_LOCAL_ACCOUNT_PASSWORD_SET_OP",
    "VMWARE_APPLIANCE_LOCAL_ACCOUNT_POLICY_SET_OP",
]

APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY: Final = "vmware-appliance-local-accounts"

#: Group text: says which reads and changes the group holds.
APPLIANCE_LOCAL_ACCOUNTS_WHEN_TO_USE: Final = (
    "This group holds the local accounts of the vCenter appliance: its own "
    "operating-system accounts, such as root, the login for the appliance console, SSH "
    "and the management page on port 5480. Reads: vmware.appliance.local_account.list "
    "and vmware.appliance.local_account.get (roles, password expiry, day limits). "
    "Changes: vmware.appliance.local_account.password.set (a new password from a Vault "
    "path) and vmware.appliance.local_account.policy.set (when the password expires and "
    "when the account locks)."
)

_TIME_NOTE: Final = "ISO 8601 date and time"
#: The SSO group every operation of this group needs; reads and changes alike.
_SERVICE_ACCOUNT_NOTE: Final = (
    "The vCenter service account of the target needs the appliance super administrator "
    "level for this operation: membership in the SSO group "
    "SystemConfiguration.BashShellAdministrators. After a group change, MEHO's next login "
    "to vCenter (by default within 20 minutes) carries the new group."
)

_ACCOUNT_PROPERTIES: Final[dict[str, Any]] = {
    "username": {"type": "string", "description": "The local user name, for example root."},
    "roles": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Appliance roles of the account, for example superAdmin.",
    },
    "enabled": {"type": ["boolean", "null"], "description": "True when the account can log in."},
    "has_password": {
        "type": ["boolean", "null"],
        "description": "True when the account has a password. The password is never returned.",
    },
    "last_password_change": {
        "type": ["string", "null"],
        "description": f"When the password last changed ({_TIME_NOTE}); null: never.",
    },
    "password_expires_at": {
        "type": ["string", "null"],
        "description": f"When the password expires ({_TIME_NOTE}); null: it never expires.",
    },
    "password_expired": {
        "type": ["boolean", "null"],
        "description": (
            "True when password_expires_at is now or past; false when it is later or the "
            "password never expires; null when the date cannot be read."
        ),
    },
    "days_until_password_expires": {
        "type": ["integer", "null"],
        "description": "Whole days until the password expires; negative after it expired.",
    },
    "max_days_between_password_change": {
        "type": ["integer", "null"],
        "description": "Days a password stays valid; null: no limit.",
    },
    "min_days_between_password_change": {
        "type": ["integer", "null"],
        "description": "Days before the password may change again.",
    },
    "warn_days_before_password_expiration": {
        "type": ["integer", "null"],
        "description": "Days of warning before the password expires.",
    },
    "inactive_at": {
        "type": ["string", "null"],
        "description": f"When the account locks after the expiry ({_TIME_NOTE}); null: never.",
    },
}

_ACCOUNT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One local account. No field holds a password.",
    "properties": _ACCOUNT_PROPERTIES,
    "required": list(ACCOUNT_FIELDS),
}

_USERNAME_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": USERNAME_MAX_LENGTH,
    "pattern": USERNAME_PATTERN,
    "description": (
        "The local user name, for example root: letters, digits, '.', '@' and '-', "
        "starting and ending with a letter or a digit."
    ),
}


def _days_property(description: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": DAYS_MIN,
        "maximum": DAYS_MAX,
        "description": f"{description} From {DAYS_MIN} to {DAYS_MAX}.",
    }


_ROW_FIELDS_TEXT: Final = (
    "username, roles, enabled, has_password, last_password_change, password_expires_at, "
    "password_expired, days_until_password_expires, max_days_between_password_change, "
    "min_days_between_password_change, warn_days_before_password_expiration, inactive_at"
)

VMWARE_APPLIANCE_LOCAL_ACCOUNT_LIST_OP = VmwareTypedOp(
    op_id=LIST_OP_ID,
    handler_attr="appliance_local_account_list",
    summary="List the vCenter appliance's local accounts, such as root, and their password expiry.",
    description=(
        "Lists the appliance's own operating-system accounts with roles, enabled, last "
        "password change, expiry date, whether it expired, and the day limits. root is the "
        "login for the console, SSH and port 5480. Read-only; never returns a password."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "accounts": {
                "type": "array",
                "description": "One row per local account, sorted by user name.",
                "items": {
                    "type": "object",
                    "properties": {
                        **_ACCOUNT_PROPERTIES,
                        "read_note": {
                            "type": ["string", "null"],
                            "description": "Why this account's read failed; null when it worked.",
                        },
                    },
                    "required": [*ACCOUNT_FIELDS, "read_note"],
                },
            },
        },
        "required": ["accounts"],
    },
    group_key=APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "appliance", "local-account"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the local accounts of a vCenter appliance (its own operating-system "
            "accounts, such as root) and when each password expires. root is the login for "
            "the appliance console, SSH and the management page on port 5480."
        ),
        "output_shape": (
            f"{{accounts: [{{{_ROW_FIELDS_TEXT}, read_note}}]}}. Times are ISO 8601. "
            "password_expires_at null means the password never expires. When one account's "
            "read failed, its fields are null and read_note says why. No password is returned."
        ),
        "next_step": (
            "For one account, use vmware.appliance.local_account.get. To set a new password, "
            "use vmware.appliance.local_account.password.set; to change when it expires, use "
            f"vmware.appliance.local_account.policy.set. {_SERVICE_ACCOUNT_NOTE}"
        ),
    },
)

VMWARE_APPLIANCE_LOCAL_ACCOUNT_GET_OP = VmwareTypedOp(
    op_id=GET_OP_ID,
    handler_attr="appliance_local_account_get",
    summary="Read one vCenter appliance local account, such as root, and its password expiry.",
    description=(
        "Reads one appliance local account: roles, enabled, whether it has a password, the "
        "last change, the expiry date, whether it expired, and the day limits. Read-only; "
        "never returns a password."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"username": _USERNAME_PROPERTY},
        "required": ["username"],
        "additionalProperties": False,
        "examples": [{"username": "root"}],
    },
    response_schema=_ACCOUNT_SCHEMA,
    group_key=APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "appliance", "local-account"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check one local account of the vCenter appliance, for example whether "
            "the root password expired or when it expires."
        ),
        "parameter_hints": {"username": "The local user name, for example 'root'."},
        "output_shape": (
            f"{{{_ROW_FIELDS_TEXT}}}. Times are ISO 8601. password_expires_at null means the "
            "password never expires. No password is returned."
        ),
        "next_step": (
            "If password_expired is true or days_until_password_expires is small, set a new "
            "password with vmware.appliance.local_account.password.set or change the expiry "
            "with vmware.appliance.local_account.policy.set. A missing account answers "
            f"not_found. {_SERVICE_ACCOUNT_NOTE}"
        ),
    },
)

VMWARE_APPLIANCE_LOCAL_ACCOUNT_PASSWORD_SET_OP = VmwareTypedOp(
    op_id=PASSWORD_SET_OP_ID,
    handler_attr="appliance_local_account_password_set",
    summary="Set a new password for one vCenter appliance local account, such as root, from Vault.",
    description=(
        "Sets a new password for one appliance local account. MEHO reads the password from a "
        "Vault path you name; it is never a parameter and never in the result. Waits until a "
        "second person approves it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "username": _USERNAME_PROPERTY,
            "password_secret_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": (
                    "Vault path of the secret that holds the new password. Never put the "
                    "password itself here."
                ),
            },
            "password_secret_mount": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Vault KV version 2 mount of the secret (default 'secret').",
            },
            "password_secret_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    f"Key in the secret that holds the password (default '{DEFAULT_PASSWORD_KEY}')."
                ),
            },
        },
        "required": ["username", "password_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {"username": "root", "password_secret_ref": "<vault-path-of-the-new-password>"}
        ],
    },
    response_schema={
        "type": "object",
        "description": "The account that changed. The password is never returned.",
        "properties": {
            "username": {"type": "string", "description": "The account that changed."},
            "password_set": {"type": "boolean", "description": "True: vCenter took the password."},
            "password_expires_at": {
                "type": ["string", "null"],
                "description": "The new expiry date (ISO 8601); null: never, or the read failed.",
            },
            "read_note": {
                "type": ["string", "null"],
                "description": "Set when the read after the change failed; the change is done.",
            },
        },
        "required": ["username", "password_set", "password_expires_at", "read_note"],
    },
    group_key=APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    tags=("write", "vmware", "vcenter", "appliance", "local-account", "password"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to give one local account of the vCenter appliance a new password, for "
            "example when the root password expired. This changes the login for the appliance "
            "console, SSH and the management page on port 5480."
        ),
        "preconditions": (
            "Store the new password in Vault first, under the key 'password' (or name the key "
            "in password_secret_key), and pass only the path in password_secret_ref. The change "
            "waits until a second person approves it. After the approval, MEHO reads the "
            "password under the approver's login, so the approver needs read access to that "
            f"path. {_SERVICE_ACCOUNT_NOTE} vCenter checks its password rules."
        ),
        "parameter_hints": {
            "username": "The local user name, for example 'root'.",
            "password_secret_ref": "Vault path of the new password; never the password itself.",
            "password_secret_key": "Only when the key in the secret is not 'password'.",
        },
        "output_shape": (
            "{username, password_set, password_expires_at, read_note}. The password is never "
            "returned. read_note is set when the read after the change failed; the change is "
            "done then."
        ),
        "next_step": (
            "Read the account with vmware.appliance.local_account.get and check "
            "last_password_change and password_expires_at. Keep the Vault secret: it now holds "
            "the current password. When vCenter refuses the password, the error gives its "
            "reason, for example a password rule."
        ),
    },
)

VMWARE_APPLIANCE_LOCAL_ACCOUNT_POLICY_SET_OP = VmwareTypedOp(
    op_id=POLICY_SET_OP_ID,
    handler_attr="appliance_local_account_policy_set",
    summary=(
        "Change when the password of one vCenter appliance local account, such as root, expires."
    ),
    description=(
        "Changes the expiry rules of one appliance local account: whether the password "
        "expires, the day limits, the warning days, and whether the account locks after "
        "expiry. Only the fields you pass change. Waits until a second person approves it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "username": _USERNAME_PROPERTY,
            "password_expires": {
                "type": "boolean",
                "description": (
                    "False: the password never expires. True: it expires after "
                    "max_days_between_password_change days."
                ),
            },
            "max_days_between_password_change": _days_property(
                "Days a password stays valid before it must change. A large value such as "
                "9999 keeps it valid for years."
            ),
            "min_days_between_password_change": _days_property(
                "Days before the password may change again; 0 allows a change at any time."
            ),
            "warn_days_before_password_expiration": _days_property(
                "Days of warning before the password expires."
            ),
            "inactive_after_password_expiration": {
                "type": "boolean",
                "description": (
                    "True: the account locks some days after the password expired (see "
                    "days_after_password_expiration). False: it does not lock."
                ),
            },
            "days_after_password_expiration": _days_property(
                "Days after the password expired before the account locks."
            ),
        },
        "required": ["username"],
        "anyOf": [{"required": [field]} for field in POLICY_FIELDS],
        "additionalProperties": False,
        "examples": [{"username": "root", "max_days_between_password_change": 9999}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "The account that changed."},
            "requested": {
                "type": "object",
                "description": "The fields sent to vCenter, with their new values.",
            },
            "before": {**_ACCOUNT_SCHEMA, "description": "The account before the change."},
            "after": {
                **_ACCOUNT_SCHEMA,
                "type": ["object", "null"],
                "description": "The account after the change; null when the read failed.",
            },
            "read_note": {
                "type": ["string", "null"],
                "description": "Set when the read after the change failed; the change is done.",
            },
        },
        "required": ["username", "requested", "before", "after", "read_note"],
    },
    group_key=APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    tags=("write", "vmware", "vcenter", "appliance", "local-account", "expiry"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to change when the password of one local account of the vCenter appliance "
            "expires, for example so the root password does not expire again, or whether the "
            "account locks after the expiry."
        ),
        "preconditions": (
            "Read the account first with vmware.appliance.local_account.get. The change waits "
            f"until a second person approves it. {_SERVICE_ACCOUNT_NOTE}"
        ),
        "parameter_hints": {
            "username": "The local user name, for example 'root'.",
            "password_expires": "false to stop the password from expiring.",
            "max_days_between_password_change": "For example 9999 days.",
        },
        "output_shape": (
            "{username, requested: {field: value}, before: <account>, after: <account> or "
            "null, read_note}. before and after have the fields of "
            "vmware.appliance.local_account.get."
        ),
        "next_step": (
            "Compare before and after, above all password_expires_at. This change does not "
            "set a password; for that, use vmware.appliance.local_account.password.set."
        ),
    },
)

#: The four operations, in the order the registrar adds them.
APPLIANCE_LOCAL_ACCOUNTS_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_APPLIANCE_LOCAL_ACCOUNT_LIST_OP,
    VMWARE_APPLIANCE_LOCAL_ACCOUNT_GET_OP,
    VMWARE_APPLIANCE_LOCAL_ACCOUNT_PASSWORD_SET_OP,
    VMWARE_APPLIANCE_LOCAL_ACCOUNT_POLICY_SET_OP,
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = APPLIANCE_LOCAL_ACCOUNTS_OPS
WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY: APPLIANCE_LOCAL_ACCOUNTS_WHEN_TO_USE
}
