# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad computer ops — ``list`` / ``get`` (safe) + ``join-prestage`` / ``unjoin`` / ``delete``.

``join-prestage`` (create a computer account so a machine can join the domain)
and ``unjoin`` (disable a computer account so it can no longer authenticate,
while the object is retained) are ``caution`` / recoverable; ``delete`` is
``dangerous`` + ``requires_approval``. Computers are driven by the
``ActiveDirectory`` module cmdlets (``Get-ADComputer`` / ``New-ADComputer`` /
``Disable-ADAccount`` / ``Remove-ADComputer``) over the shared
PowerShell-over-SSH transport.

Why ``unjoin`` disables rather than removes
-------------------------------------------

The recoverable inverse of a live domain join, from the directory side, is
``Disable-ADAccount`` — the computer can no longer authenticate to the domain,
but its account object is retained and re-enabling it restores the trust. A full
object removal is the separate ``dangerous`` ``msad.computer.delete`` op. This
mirrors the winsrv tier doctrine (recoverable = ``caution``, destructive =
``dangerous`` + approval).

PowerShell injection safety
---------------------------

The computer name / identity and the OU path are interpolated only inside
single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`. ``delete``
carries ``-Confirm:$false`` (the approval gate is MEHO's, not AD's).

References
----------

* ``Get-ADComputer`` / ``New-ADComputer`` / ``Remove-ADComputer`` /
  ``Disable-ADAccount``:
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
    "COMPUTER_OPS",
    "msad_computer_delete",
    "msad_computer_get",
    "msad_computer_join_prestage",
    "msad_computer_list",
    "msad_computer_unjoin",
]

_COMPUTER_SELECT: str = (
    "Name, SamAccountName, DNSHostName, Enabled, DistinguishedName, "
    "OperatingSystem, IPv4Address, LastLogonDate"
)
_COMPUTER_PROPS: str = "OperatingSystem,IPv4Address,LastLogonDate"


async def msad_computer_list(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.computer.list`` — ``Get-ADComputer -Filter *`` (list, capped)."""
    limit = validate_limit(params.get("limit"))
    return await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADComputer -Filter * -ResultSetSize {limit} -Properties {_COMPUTER_PROPS} "
            f"| Select-Object {_COMPUTER_SELECT}"
        ),
        operator=operator,
    )


async def msad_computer_get(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.computer.get`` — ``Get-ADComputer -Identity`` (one account)."""
    identity = ps_single_quote(params["identity"])
    result = await ad_list_read(
        connector,
        target,
        pipeline=(
            f"Get-ADComputer -Identity {identity} -Properties {_COMPUTER_PROPS} "
            f"| Select-Object {_COMPUTER_SELECT}"
        ),
        operator=operator,
    )
    return {"identity": params["identity"], **result}


async def msad_computer_join_prestage(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.computer.join-prestage`` (caution) — ``New-ADComputer``.

    Prestages a computer account (optionally in an OU, optionally with an
    explicit sAMAccountName / DNS host name) so the target machine can bind to
    it during a domain join. Created enabled by default. Recoverable via
    ``unjoin`` / ``delete``.
    """
    name = params["name"]
    sam = params.get("sam_account_name")
    # Read the account back by its sAMAccountName when the operator set one that
    # differs from the CN -- ``Get-ADComputer -Identity <name>`` resolves by SAM,
    # so a divergent SAM would not round-trip. Mirrors ``user.create``.
    readback = sam if sam is not None else name
    clauses = [f"New-ADComputer -Name {ps_single_quote(name)}"]
    if sam is not None:
        clauses.append(f"-SamAccountName {ps_single_quote(sam)}")
    if params.get("dns_host_name") is not None:
        clauses.append(f"-DNSHostName {ps_single_quote(params['dns_host_name'])}")
    if params.get("path") is not None:
        clauses.append(f"-Path {ps_single_quote(params['path'])}")
    new_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{new_expr} | Out-Null; "
        f"$c = Get-ADComputer -Identity {ps_single_quote(readback)} -Properties {_COMPUTER_PROPS} "
        f"| Select-Object {_COMPUTER_SELECT}; "
        "ConvertTo-Json -Depth 4 -Compress -InputObject @{ ok = $true; computer = $c }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    computer = payload.get("computer") if isinstance(payload, dict) else None
    return {
        "identity": readback,
        "action": "join-prestage",
        "computer": computer,
        "op_class": "write",
    }


async def msad_computer_unjoin(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.computer.unjoin`` (caution) — ``Disable-ADAccount`` (recoverable).

    Disables the computer account so the machine can no longer authenticate to
    the domain while the object is retained (re-enable to restore trust). A full
    object removal is ``msad.computer.delete``.
    """
    identity = params["identity"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Disable-ADAccount -Identity {ps_single_quote(identity)}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"identity": identity, "action": "unjoin", "op_class": "write"}


async def msad_computer_delete(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.computer.delete`` (dangerous, resume path only) — ``Remove-ADComputer``.

    Runs ``Remove-ADComputer -Identity <computer> -Confirm:$false``.
    Irreversible — ``requires_approval`` parks a dispatch for a human first.
    """
    identity = params["identity"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Remove-ADComputer -Identity {ps_single_quote(identity)} -Confirm:$false; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"identity": identity, "action": "delete", "op_class": "write"}


_COMPUTER_IDENTITY_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The computer identity — sAMAccountName (usually ``NAME$``), name, DN, "
        "GUID, or SID (the ``-Identity`` operand, e.g. ``SQLNODE1``)."
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

_IDENTITY_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["identity", "rows", "total"],
    "additionalProperties": True,
}

_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "action": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["identity", "action", "op_class"],
    "additionalProperties": True,
}


COMPUTER_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.computer.list",
        handler_attr="msad_computer_list",
        summary="List AD computer accounts via ``Get-ADComputer -Filter *`` (capped by limit).",
        description=(
            "Runs ``Get-ADComputer -Filter *`` (capped at ``limit`` rows, "
            "default 500) and returns one row per computer (Name, "
            "SamAccountName, DNSHostName, Enabled, DN, OperatingSystem, "
            "IPv4Address, LastLogonDate). Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_PROP},
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="computers",
        tags=("read-only", "computer", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate AD computer accounts (bounded by ``limit``). "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"limit": "Optional; max rows (default 500)."},
            "output_shape": (
                "{'rows': [{Name, DNSHostName, Enabled, OperatingSystem, ...}], 'total': <int>}."
            ),
        },
    ),
    MsadOp(
        op_id="msad.computer.get",
        handler_attr="msad_computer_get",
        summary="Read one AD computer by identity via ``Get-ADComputer -Identity``.",
        description=(
            "Runs ``Get-ADComputer -Identity <id>`` and returns the single "
            "matching computer projection as a ``{rows, total}`` envelope plus "
            "the echoed ``identity``. A missing identity is a terminating "
            "error. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _COMPUTER_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_IDENTITY_LIST_RESPONSE_SCHEMA,
        group_key="computers",
        tags=("read-only", "computer", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one AD computer account's OS / DNS host / enabled "
                "state by identity. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. sAMAccountName / name / DN / GUID / SID."},
            "output_shape": "{'identity', 'rows': [<computer>], 'total': <int>}.",
        },
    ),
    MsadOp(
        op_id="msad.computer.join-prestage",
        handler_attr="msad_computer_join_prestage",
        summary="Prestage an AD computer account via ``New-ADComputer`` (caution).",
        description=(
            "Runs ``New-ADComputer -Name <name>`` with optional sAMAccountName "
            "/ DNS host name / OU path, creating a computer account so a "
            "machine can join the domain against it. Recoverable (via "
            "``unjoin`` / ``delete``). safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The computer account name (``-Name``).",
                },
                "sam_account_name": {
                    "type": "string",
                    "description": "Optional explicit sAMAccountName (usually ``NAME$``).",
                },
                "dns_host_name": {"type": "string", "description": "Optional DNS host name."},
                "path": {"type": "string", "description": "Optional OU distinguished name."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "identity": {"type": "string"},
                "action": {"type": "string"},
                "computer": {"type": ["object", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["identity", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="computers",
        tags=("write", "computer", "join-prestage"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Prestage a computer account so a machine can join the domain "
                "into a chosen OU. Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The computer account name.",
                "path": "Optional. OU distinguished name to create the account in.",
            },
            "output_shape": (
                "{'identity': <name>, 'action': 'join-prestage', 'computer': "
                "<created>, 'op_class': 'write'}."
            ),
        },
    ),
    MsadOp(
        op_id="msad.computer.unjoin",
        handler_attr="msad_computer_unjoin",
        summary="Disable an AD computer account via ``Disable-ADAccount`` (caution, recoverable).",
        description=(
            "Runs ``Disable-ADAccount -Identity <computer>`` — the machine can "
            "no longer authenticate to the domain, but the account object is "
            "retained (re-enable to restore trust). Recoverable; the "
            "destructive object removal is ``msad.computer.delete``. "
            "safety_level=caution."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _COMPUTER_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_ACTION_RESPONSE_SCHEMA,
        group_key="computers",
        tags=("write", "computer", "unjoin"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Disable a computer account so the machine leaves the domain's "
                "trust while the object is retained (recoverable). For "
                "irreversible removal use ``msad.computer.delete``. "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. The computer identity."},
            "output_shape": "{'identity', 'action': 'unjoin', 'op_class': 'write'}.",
        },
    ),
    MsadOp(
        op_id="msad.computer.delete",
        handler_attr="msad_computer_delete",
        summary="Delete an AD computer account via ``Remove-ADComputer`` (dangerous, approval).",
        description=(
            "Runs ``Remove-ADComputer -Identity <computer> -Confirm:$false``. "
            "Irreversible — the account object and its SID are gone. "
            "safety_level=dangerous, requires_approval=True: a dispatch parks "
            "for a human to approve first."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"identity": _COMPUTER_IDENTITY_PROP},
            "required": ["identity"],
            "additionalProperties": False,
        },
        response_schema=_ACTION_RESPONSE_SCHEMA,
        group_key="computers",
        tags=("write", "computer", "delete"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Delete an AD computer account. Irreversible; approval-gated — "
                "parks for a human decision first. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"identity": "Required. The computer identity to remove."},
            "output_shape": "{'identity', 'action': 'delete', 'op_class': 'write'}.",
        },
    ),
)
