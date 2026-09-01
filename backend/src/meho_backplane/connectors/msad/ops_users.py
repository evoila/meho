# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad user ops — ``list`` / ``get`` / ``search`` (safe) + guarded writes.

``create`` / ``set`` / ``enable`` / ``disable`` are ``caution`` (recoverable);
``delete`` is ``dangerous`` + ``requires_approval`` (the rke2 approval-parked
mold). Driven by the ``ActiveDirectory`` module cmdlets (``Get-ADUser`` /
``New-ADUser`` / ``Set-ADUser`` / ``*-ADAccount`` / ``Remove-ADUser``) over the
shared PowerShell-over-SSH transport.

**No plaintext password on the wire (deliberate).** The shared transport's
safety contract forbids credential material in the ``-EncodedCommand`` script
(it lands on the remote argv). So ``create`` omits ``-AccountPassword`` — per the
cmdlet's documented behaviour an account created without a password is **disabled
until a password is set** — and ``set`` never touches it. Password provisioning /
reset is a deferred Vault-brokered follow-up (rke2 ``token.rotate`` mold); see
``docs/codebase/connectors-msad.md``.

**Injection safety.** Identities / attribute values are interpolated only inside
single-quoted literals via ``ps_single_quote``. The ``search`` query is bound
through a **script-block** ``-Filter`` (``{Name -like $q}``), the documented
injection-safe form: the AD filter engine binds the session variable's value as a
data operand rather than re-parsing an interpolated string. Cmdlet reference:
https://learn.microsoft.com/en-us/powershell/module/activedirectory/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.msad.ops import (
    SSH_TRANSPORT_NOTE,
    MsadOp,
    ad_list_read,
    validate_limit,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "USER_OPS",
    "msad_user_create",
    "msad_user_delete",
    "msad_user_disable",
    "msad_user_enable",
    "msad_user_get",
    "msad_user_list",
    "msad_user_search",
    "msad_user_set",
]

_USER_PROPS: str = "DisplayName,Description,EmailAddress"  # non-default props to fetch
#: The bounded projection every user read returns — a stable, JSON-safe subset.
_USER_SELECT: str = (
    "SamAccountName, Name, DisplayName, Enabled, UserPrincipalName, "
    "DistinguishedName, GivenName, Surname, Description, EmailAddress"
)


async def msad_user_list(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.list`` — ``Get-ADUser -Filter *`` (list, capped)."""
    limit = validate_limit(params.get("limit"))
    return await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADUser -Filter * -ResultSetSize {limit} -Properties {_USER_PROPS} "
            f"| Select-Object {_USER_SELECT}"
        ),
        operator=operator,
    )


async def msad_user_get(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.get`` — ``Get-ADUser -Identity`` (one account)."""
    identity = ps_single_quote(params["identity"])
    result = await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADUser -Identity {identity} -Properties {_USER_PROPS} "
            f"| Select-Object {_USER_SELECT}"
        ),
        operator=operator,
    )
    return {"identity": params["identity"], **result}


async def msad_user_search(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.search`` — name / SAM ``-like`` match (list, capped).

    The query is wrapped in ``*…*``, assigned to ``$q`` as a single-quoted literal,
    and bound through a script-block ``-Filter`` (injection-safe — see the module
    docstring).
    """
    limit = validate_limit(params.get("limit"))
    q_literal = ps_single_quote(f"*{params['query']}*")
    return await ad_list_read(
        connector,
        target,
        prelude=f"$q = {q_literal}; ",
        pipeline=(
            "Get-ADUser -Filter {(Name -like $q) -or (SamAccountName -like $q)} "
            f"-ResultSetSize {limit} -Properties {_USER_PROPS} "
            f"| Select-Object {_USER_SELECT}"
        ),
        operator=operator,
    )


async def msad_user_create(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.create`` (caution) — ``New-ADUser`` (disabled, no password).

    Creates the account WITHOUT a password, so it is created disabled (see the
    module docstring). Optional UPN / names / OU path / description / email.
    """
    name = params["name"]
    sam = params["sam_account_name"]
    clauses = [
        f"New-ADUser -Name {ps_single_quote(name)}",
        f"-SamAccountName {ps_single_quote(sam)}",
    ]
    _append_str_clause(clauses, params, "user_principal_name", "-UserPrincipalName")
    _append_str_clause(clauses, params, "display_name", "-DisplayName")
    _append_str_clause(clauses, params, "given_name", "-GivenName")
    _append_str_clause(clauses, params, "surname", "-Surname")
    _append_str_clause(clauses, params, "path", "-Path")
    _append_str_clause(clauses, params, "description", "-Description")
    _append_str_clause(clauses, params, "email", "-EmailAddress")
    new_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{new_expr} | Out-Null; "
        f"$u = Get-ADUser -Identity {ps_single_quote(sam)} -Properties {_USER_PROPS} "
        f"| Select-Object {_USER_SELECT}; "
        "ConvertTo-Json -Depth 4 -Compress -InputObject @{ ok = $true; user = $u }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    user = payload.get("user") if isinstance(payload, dict) else None
    return {"identity": sam, "action": "create", "user": user, "op_class": "write"}


async def msad_user_set(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.set`` (caution) — ``Set-ADUser`` attributes (never a password).

    Applies display name / description / email / given / surname. At least one is
    required. Enable / disable are separate ops.
    """
    identity = ps_single_quote(params["identity"])
    set_clauses: list[str] = []
    _append_str_clause(set_clauses, params, "display_name", "-DisplayName")
    _append_str_clause(set_clauses, params, "description", "-Description")
    _append_str_clause(set_clauses, params, "email", "-EmailAddress")
    _append_str_clause(set_clauses, params, "given_name", "-GivenName")
    _append_str_clause(set_clauses, params, "surname", "-Surname")
    if not set_clauses:
        raise ValueError(
            "user.set requires at least one attribute to change "
            "(display_name / description / email / given_name / surname)"
        )
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Set-ADUser -Identity {identity} {' '.join(set_clauses)}; "
        f"$u = Get-ADUser -Identity {identity} -Properties {_USER_PROPS} "
        f"| Select-Object {_USER_SELECT}; "
        "ConvertTo-Json -Depth 4 -Compress -InputObject @{ ok = $true; user = $u }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    user = payload.get("user") if isinstance(payload, dict) else None
    return {"identity": params["identity"], "action": "set", "user": user, "op_class": "write"}


async def msad_user_enable(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.enable`` (caution) — ``Enable-ADAccount``.

    Enabling requires a password to already be set on the account (see the
    module docstring); on a passwordless account this surfaces as a real
    ``PwshRunError``.
    """
    return await _account_action(
        connector, target, params["identity"], "Enable-ADAccount", "enable", operator
    )


async def msad_user_disable(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.disable`` (caution) — ``Disable-ADAccount``."""
    return await _account_action(
        connector, target, params["identity"], "Disable-ADAccount", "disable", operator
    )


async def msad_user_delete(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.user.delete`` (dangerous, resume path only) — ``Remove-ADUser``.

    Runs ``Remove-ADUser -Identity <id> -Confirm:$false``. Irreversible — the
    op is ``requires_approval`` so a dispatch parks for a human decision before
    the account is removed. ``-Confirm:$false`` suppresses the cmdlet's own
    interactive prompt (it would hang non-interactively); the approval gate is
    MEHO's, not AD's.
    """
    return await _account_action(
        connector,
        target,
        params["identity"],
        "Remove-ADUser",
        "delete",
        operator,
        confirm_false=True,
    )


def _append_str_clause(clauses: list[str], params: dict[str, Any], key: str, flag: str) -> None:
    """Append ``<flag> '<escaped>'`` to *clauses* when *key* is a non-None string."""
    value = params.get(key)
    if value is not None:
        clauses.append(f"{flag} {ps_single_quote(value)}")


async def _account_action(
    connector: MsadConnector,
    target: Any,
    identity: str,
    cmdlet: str,
    action: str,
    operator: Operator | None,
    *,
    confirm_false: bool = False,
) -> dict[str, Any]:
    """Run a single-cmdlet ``-Identity`` write and return the write envelope."""
    quoted = ps_single_quote(identity)
    confirm = " -Confirm:$false" if confirm_false else ""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{cmdlet} -Identity {quoted}{confirm}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"identity": identity, "action": action, "op_class": "write"}


_IDENTITY_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The user identity — sAMAccountName, distinguished name, GUID, or SID "
        "(the ``-Identity`` operand, e.g. ``svc-sql``)."
    ),
}

_LIMIT_PROP: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "Max rows to return (maps to ``-ResultSetSize``). Default 500.",
}

_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}

_ACCOUNT_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "action": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["identity", "action", "op_class"],
    "additionalProperties": True,
}

# create/set additionally echo the resulting object under ``user``.
_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    **_ACCOUNT_ACTION_RESPONSE_SCHEMA,
    "properties": {
        **_ACCOUNT_ACTION_RESPONSE_SCHEMA["properties"],
        "user": {"type": ["object", "null"]},
    },
}


def _account_action_op(
    op_id: str,
    handler_attr: str,
    verb: str,
    cmdlet: str,
    *,
    safety_level: str,
    requires_approval: bool,
) -> MsadOp:
    """Build one identity-only user write op (enable / disable / delete)."""
    note = (
        "Irreversible; approval-gated — parks for a human decision first."
        if requires_approval
        else "Recoverable."
    )
    confirm = " -Confirm:$false" if requires_approval else ""
    return MsadOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"{verb.capitalize()} an AD user account via ``{cmdlet}``.",
        description=(
            f"Runs ``{cmdlet} -Identity <id>``{confirm}. {note} safety_level={safety_level}."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_ACCOUNT_ACTION_RESPONSE_SCHEMA,
        group_key="users",
        tags=("write", "user", verb),
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
        llm_instructions={
            "when_to_use": f"{verb.capitalize()} an AD user account. {note} " + SSH_TRANSPORT_NOTE,
            "parameter_hints": {"identity": "Required. sAMAccountName / DN / GUID / SID."},
            "output_shape": f"{{'identity', 'action': '{verb}', 'op_class': 'write'}}.",
        },
    )


USER_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.user.list",
        handler_attr="msad_user_list",
        summary="List AD users via ``Get-ADUser -Filter *`` (capped by limit).",
        description=(
            "Runs ``Get-ADUser -Filter *`` (capped at ``limit`` rows, default 500) "
            "and returns one row per user (SamAccountName, Name, DisplayName, "
            "Enabled, UPN, DN, GivenName, Surname, Description, EmailAddress). "
            "Read-only; no password material is ever read."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_PROP},
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="users",
        tags=("read-only", "user", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate AD user accounts (bounded by ``limit``). "
                "Read-only; no password material. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"limit": "Optional; max rows (default 500)."},
            "output_shape": "{'rows': [{SamAccountName, Name, Enabled, ...}], 'total': <int>}.",
        },
    ),
    MsadOp(
        op_id="msad.user.get",
        handler_attr="msad_user_get",
        summary="Read one AD user by identity via ``Get-ADUser -Identity``.",
        description=(
            "Runs ``Get-ADUser -Identity <id>`` and returns the single matching "
            "user projection as a ``{rows, total}`` envelope plus the echoed "
            "``identity``. A missing identity is a terminating error. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "identity": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["identity", "rows", "total"],
            "additionalProperties": True,
        },
        group_key="users",
        tags=("read-only", "user", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one AD user's attributes and enabled state by "
                "sAMAccountName / DN / GUID / SID. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. sAMAccountName / DN / GUID / SID."},
            "output_shape": "{'identity', 'rows': [<user>], 'total': <int>}.",
        },
    ),
    MsadOp(
        op_id="msad.user.search",
        handler_attr="msad_user_search",
        summary="Search AD users by name / SAM substring (``-like``, capped).",
        description=(
            "Runs ``Get-ADUser`` with a script-block ``-Filter`` matching the "
            "query as a ``*…*`` substring against Name or SamAccountName "
            "(capped at ``limit`` rows, default 500). The query is bound as a "
            "data operand (injection-safe). Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "Substring matched against Name / SamAccountName.",
                },
                "limit": _LIMIT_PROP,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="users",
        tags=("read-only", "user", "search"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to find AD users whose name or SAM account name contains "
                "a substring (e.g. ``sql``). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "query": "Required. Substring to match (wrapped in wildcards).",
                "limit": "Optional; max rows (default 500).",
            },
            "output_shape": "{'rows': [{SamAccountName, Name, ...}], 'total': <int>}.",
        },
    ),
    MsadOp(
        op_id="msad.user.create",
        handler_attr="msad_user_create",
        summary="Create an AD user via ``New-ADUser`` (disabled, no password) (caution).",
        description=(
            "Runs ``New-ADUser -Name <name> -SamAccountName <sam>`` with optional "
            "UPN / display name / given / surname / OU path / description / email. "
            "Created WITHOUT a password, so it is **disabled** until a password is "
            "set out of band (see the connector doc). safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The account name / CN (``-Name``).",
                },
                "sam_account_name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The sAMAccountName (``-SamAccountName``).",
                },
                "user_principal_name": {"type": "string", "description": "Optional UPN."},
                "display_name": {"type": "string", "description": "Optional display name."},
                "given_name": {"type": "string", "description": "Optional given name."},
                "surname": {"type": "string", "description": "Optional surname."},
                "path": {"type": "string", "description": "Optional OU distinguished name."},
                "description": {"type": "string", "description": "Optional description."},
                "email": {"type": "string", "description": "Optional email address."},
            },
            "required": ["name", "sam_account_name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="users",
        tags=("write", "user", "create"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Create an AD user account (created disabled + passwordless — set "
                "the password out of band, then enable). Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The account name / CN.",
                "sam_account_name": "Required. The sAMAccountName.",
                "path": "Optional. OU distinguished name to create the user in.",
            },
            "output_shape": "{'identity': <sam>, 'action': 'create', 'user': <created>, ...}.",
        },
    ),
    MsadOp(
        op_id="msad.user.set",
        handler_attr="msad_user_set",
        summary="Update an AD user's attributes via ``Set-ADUser`` (never a password) (caution).",
        description=(
            "Applies display name / description / email / given name / surname via "
            "``Set-ADUser``. Never touches the password. At least one attribute is "
            "required. Enable / disable are separate ops. safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "identity": _IDENTITY_PROP,
                "display_name": {"type": "string", "description": "New display name."},
                "description": {"type": "string", "description": "New description."},
                "email": {"type": "string", "description": "New email address."},
                "given_name": {"type": "string", "description": "New given name."},
                "surname": {"type": "string", "description": "New surname."},
            },
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="users",
        tags=("write", "user", "set"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Update an AD user's display name / description / email / given / "
                "surname. Never touches the password. Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. sAMAccountName / DN / GUID / SID."},
            "output_shape": "{'identity', 'action': 'set', 'user': <updated>, ...}.",
        },
    ),
    _account_action_op(
        "msad.user.enable",
        "msad_user_enable",
        "enable",
        "Enable-ADAccount",
        safety_level="caution",
        requires_approval=False,
    ),
    _account_action_op(
        "msad.user.disable",
        "msad_user_disable",
        "disable",
        "Disable-ADAccount",
        safety_level="caution",
        requires_approval=False,
    ),
    _account_action_op(
        "msad.user.delete",
        "msad_user_delete",
        "delete",
        "Remove-ADUser",
        safety_level="dangerous",
        requires_approval=True,
    ),
)
