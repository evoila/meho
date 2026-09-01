# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv local-user ops — ``list`` (safe) + ``create`` / ``set`` (caution) + ``delete``.

Local accounts are driven by the ``Microsoft.PowerShell.LocalAccounts``
cmdlets (``Get-LocalUser`` / ``New-LocalUser`` / ``Set-LocalUser`` /
``Enable-LocalUser`` / ``Disable-LocalUser`` / ``Remove-LocalUser``) over the
shared PowerShell-over-SSH transport. ``delete`` is ``dangerous`` +
``requires_approval`` (the rke2 mold — irreversible account removal parks for
a human decision); ``create`` / ``set`` are ``caution``.

No plaintext password on the wire (deliberate)
----------------------------------------------

The shared pwsh transport's safety contract forbids credential material in
the ``-EncodedCommand`` script — the encoded payload lands on the remote
process argv and the script body is visible to a privileged remote observer
(see :mod:`~meho_backplane.connectors._shared.pwsh`). ``New-LocalUser`` /
``Set-LocalUser`` take the password as a ``SecureString``, but constructing
one from an operator-supplied plaintext would require embedding that
plaintext in the script — a direct contract violation. So ``create`` uses the
``-NoPassword`` parameter set and ``set`` never touches the password.
Provisioning a password is a deferred follow-up (the rke2
``token.rotate`` mint-and-stash-in-Vault mold, so no secret ever enters the
script). Until then, set the password out of band or via a domain flow (msad,
#3262).

PowerShell injection safety
---------------------------

Operator-supplied strings (name / description / full name) are interpolated
only inside single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; boolean
toggles render as ``$true`` / ``$false`` literals after validation.

References
----------

* ``New-LocalUser`` / ``Set-LocalUser`` / ``Remove-LocalUser`` (PowerShell 5.1):
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.localaccounts/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "LOCALUSER_OPS",
    "winsrv_localuser_create",
    "winsrv_localuser_delete",
    "winsrv_localuser_list",
    "winsrv_localuser_set",
]

_LOCALUSER_SELECT: str = "Name, Enabled, Description, FullName, PasswordRequired, LastLogon"


async def winsrv_localuser_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.localuser.list`` — every local user (list-shaped).

    Runs ``Get-LocalUser`` and returns ``{rows, total}`` (each row carries
    ``Name`` / ``Enabled`` / ``Description`` / ``FullName`` /
    ``PasswordRequired`` / ``LastLogon``). Read-only.
    """
    del params  # declared empty in schema; intentionally ignored
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$u = @(Get-LocalUser | Select-Object {_LOCALUSER_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $u; total = $u.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def winsrv_localuser_create(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.localuser.create`` (caution) — ``New-LocalUser -NoPassword``.

    Creates a local account with no password (see the module docstring — a
    plaintext password can't ride the pwsh transport). Optional ``full_name``
    / ``description`` and ``disabled`` / ``account_never_expires`` /
    ``user_may_not_change_password`` toggles.
    """
    name: str = params["name"]
    clauses = [f"New-LocalUser -Name {ps_single_quote(name)} -NoPassword"]
    if params.get("full_name"):
        clauses.append(f"-FullName {ps_single_quote(params['full_name'])}")
    if params.get("description"):
        clauses.append(f"-Description {ps_single_quote(params['description'])}")
    if params.get("disabled") is True:
        clauses.append("-Disabled")
    if params.get("account_never_expires") is True:
        clauses.append("-AccountNeverExpires")
    if params.get("user_may_not_change_password") is True:
        clauses.append("-UserMayNotChangePassword")
    new_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{new_expr} | Out-Null; "
        f"$u = Get-LocalUser -Name {ps_single_quote(name)} "
        f"| Select-Object {_LOCALUSER_SELECT}; "
        "ConvertTo-Json -Depth 3 -Compress -InputObject @{ ok = $true; user = $u }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    user = payload.get("user") if isinstance(payload, dict) else None
    return {"name": name, "action": "create", "user": user, "op_class": "write"}


async def winsrv_localuser_set(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.localuser.set`` (caution) — ``Set-LocalUser`` + enable/disable.

    Applies the supplied account attributes (``full_name`` / ``description``
    / ``password_never_expires``) via ``Set-LocalUser`` and, when ``enabled``
    is supplied, toggles the account with ``Enable-LocalUser`` /
    ``Disable-LocalUser``. Never touches the password. At least one settable
    attribute is required.
    """
    name: str = params["name"]
    quoted = ps_single_quote(name)
    set_clauses: list[str] = []
    if params.get("full_name") is not None:
        set_clauses.append(f"-FullName {ps_single_quote(params['full_name'])}")
    if params.get("description") is not None:
        set_clauses.append(f"-Description {ps_single_quote(params['description'])}")
    if params.get("password_never_expires") is not None:
        flag = "$true" if params["password_never_expires"] else "$false"
        set_clauses.append(f"-PasswordNeverExpires {flag}")

    enabled = params.get("enabled")
    if not set_clauses and enabled is None:
        raise ValueError(
            "localuser.set requires at least one attribute to change "
            "(full_name / description / password_never_expires / enabled)"
        )

    statements = ["$ErrorActionPreference = 'Stop'"]
    if set_clauses:
        statements.append(f"Set-LocalUser -Name {quoted} {' '.join(set_clauses)}")
    if enabled is True:
        statements.append(f"Enable-LocalUser -Name {quoted}")
    elif enabled is False:
        statements.append(f"Disable-LocalUser -Name {quoted}")
    statements.append(f"$u = Get-LocalUser -Name {quoted} | Select-Object {_LOCALUSER_SELECT}")
    statements.append("ConvertTo-Json -Depth 3 -Compress -InputObject @{ ok = $true; user = $u }")
    script = "; ".join(statements)
    payload = await pwsh_run(connector, target, script, operator=operator)
    user = payload.get("user") if isinstance(payload, dict) else None
    return {"name": name, "action": "set", "user": user, "op_class": "write"}


async def winsrv_localuser_delete(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.localuser.delete`` (dangerous, resume path only).

    Runs ``Remove-LocalUser -Name <name>`` under ``$ErrorActionPreference =
    'Stop'``. Irreversible — the op is ``requires_approval`` so a dispatch
    parks for a human decision before the account is removed.
    """
    name: str = params["name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Remove-LocalUser -Name {ps_single_quote(name)}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"name": name, "action": "delete", "op_class": "write"}


_USER_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The local user account name (the ``Name`` column, e.g. ``svc-sql``).",
}

_USER_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "action": {"type": "string"},
        "user": {"type": ["object", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["name", "action", "op_class"],
    "additionalProperties": True,
}


LOCALUSER_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.localuser.list",
        handler_attr="winsrv_localuser_list",
        summary="List local users via ``Get-LocalUser`` (name/enabled/description).",
        description=(
            "Runs ``Get-LocalUser`` and returns one row per local account "
            "(Name, Enabled, Description, FullName, PasswordRequired, "
            "LastLogon). Read-only. No password material is ever read or "
            "returned."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["rows", "total"],
            "additionalProperties": True,
        },
        group_key="localusers",
        tags=("read-only", "localuser", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate local user accounts and their enabled "
                "state. Read-only; no password material. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Name, Enabled, Description, FullName, ...}], 'total': <int>}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.localuser.create",
        handler_attr="winsrv_localuser_create",
        summary="Create a local user via ``New-LocalUser -NoPassword`` (caution).",
        description=(
            "Runs ``New-LocalUser -Name <name> -NoPassword`` with optional "
            "``full_name`` / ``description`` and disabled / never-expires / "
            "may-not-change-password toggles. The account is created WITHOUT "
            "a password — a plaintext password cannot ride the pwsh transport "
            "(see the connector doc); provision it out of band or via the "
            "domain (msad). safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _USER_NAME_PROP,
                "full_name": {"type": "string", "description": "Optional display/full name."},
                "description": {"type": "string", "description": "Optional account comment."},
                "disabled": {
                    "type": "boolean",
                    "description": "Create the account disabled (``-Disabled``). Default false.",
                },
                "account_never_expires": {
                    "type": "boolean",
                    "description": "``-AccountNeverExpires``. Default false.",
                },
                "user_may_not_change_password": {
                    "type": "boolean",
                    "description": "``-UserMayNotChangePassword``. Default false.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_USER_WRITE_RESPONSE_SCHEMA,
        group_key="localusers",
        tags=("write", "localuser", "create"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Create a local user account (no password — set it out of "
                "band). Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The local account name.",
                "full_name": "Optional display name.",
                "description": "Optional comment.",
            },
            "output_shape": (
                "{'name', 'action': 'create', 'user': <created account>, 'op_class': 'write'}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.localuser.set",
        handler_attr="winsrv_localuser_set",
        summary="Update a local user's attributes / enabled state (caution).",
        description=(
            "Applies ``full_name`` / ``description`` / "
            "``password_never_expires`` via ``Set-LocalUser`` and toggles the "
            "account via ``Enable-LocalUser`` / ``Disable-LocalUser`` when "
            "``enabled`` is supplied. Never touches the password. At least "
            "one attribute is required. safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _USER_NAME_PROP,
                "full_name": {"type": "string", "description": "New display/full name."},
                "description": {"type": "string", "description": "New account comment."},
                "enabled": {
                    "type": "boolean",
                    "description": "Enable (true) or disable (false) the account.",
                },
                "password_never_expires": {
                    "type": "boolean",
                    "description": "Set ``-PasswordNeverExpires``.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_USER_WRITE_RESPONSE_SCHEMA,
        group_key="localusers",
        tags=("write", "localuser", "set"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Update a local account's display name, description, "
                "password-never-expires flag, or enabled state. Never touches "
                "the password. Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The local account name.",
                "enabled": "Optional bool; enable/disable the account.",
            },
            "output_shape": (
                "{'name', 'action': 'set', 'user': <updated account>, 'op_class': 'write'}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.localuser.delete",
        handler_attr="winsrv_localuser_delete",
        summary="Delete a local user via ``Remove-LocalUser`` (dangerous, approval-gated).",
        description=(
            "Runs ``Remove-LocalUser -Name <name>``. Irreversible — the "
            "account and its SID are gone. safety_level=dangerous, "
            "requires_approval=True: a dispatch parks for a human to approve "
            "before the account is removed."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _USER_NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["name", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="localusers",
        tags=("write", "localuser", "delete"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Delete a local user account. Irreversible; approval-gated — "
                "parks for a human decision first. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The local account name to remove."},
            "output_shape": "{'name', 'action': 'delete', 'op_class': 'write'}.",
        },
    ),
)
