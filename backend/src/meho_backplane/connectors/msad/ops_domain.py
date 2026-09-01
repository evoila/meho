# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""msad domain ops — ``info`` / ``forest`` / ``controllers`` / ``replication`` (all safe).

Read-only domain / forest topology facts via ``Get-ADDomain`` /
``Get-ADForest`` (FSMO role holders + functional level), ``Get-ADDomainController``
(the DC inventory), and ``Get-ADReplicationPartnerMetadata`` (the inbound
replication summary), routed through the shared PowerShell-over-SSH transport
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`. **No operator input is
interpolated into any of these scripts** (they are constants — the domain /
forest / replication targets are derived on the DC itself), so the domain-read
group has no injection surface.

References
----------

* ``Get-ADDomain`` / ``Get-ADForest`` (FSMO roles, functional level):
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/get-addomain
* ``Get-ADDomainController``:
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/get-addomaincontroller
* ``Get-ADReplicationPartnerMetadata`` (``-Target`` / ``-Scope Domain``):
  https://learn.microsoft.com/en-us/powershell/module/activedirectory/get-adreplicationpartnermetadata
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.msad.ops import (
    SSH_TRANSPORT_NOTE,
    MsadOp,
    ad_list_read,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "DOMAIN_OPS",
    "msad_domain_controllers",
    "msad_domain_forest",
    "msad_domain_info",
    "msad_domain_replication",
]


#: Explicit hashtable projection (not ``Select-Object``) so the FSMO role
#: holders and the domain SID render as flat scalars rather than nested AD
#: objects ``ConvertTo-Json`` would either truncate or blow up.
_DOMAIN_INFO_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$d = Get-ADDomain; "
    "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
    "DNSRoot = $d.DNSRoot; "
    "NetBIOSName = $d.NetBIOSName; "
    "DomainMode = $d.DomainMode.ToString(); "
    "Forest = $d.Forest; "
    "DistinguishedName = $d.DistinguishedName; "
    "PDCEmulator = $d.PDCEmulator; "
    "RIDMaster = $d.RIDMaster; "
    "InfrastructureMaster = $d.InfrastructureMaster; "
    "DomainSID = $d.DomainSID.Value; "
    "DomainControllers = @($d.ReplicaDirectoryServers) }"
)

_FOREST_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$f = Get-ADForest; "
    "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
    "Name = $f.Name; "
    "ForestMode = $f.ForestMode.ToString(); "
    "RootDomain = $f.RootDomain; "
    "SchemaMaster = $f.SchemaMaster; "
    "DomainNamingMaster = $f.DomainNamingMaster; "
    "Domains = @($f.Domains); "
    "GlobalCatalogs = @($f.GlobalCatalogs); "
    "Sites = @($f.Sites) }"
)

_DC_SELECT: str = (
    "Name, HostName, Site, IPv4Address, IsGlobalCatalog, IsReadOnly, "
    "OperatingSystem, OperationMasterRoles"
)

_REPL_SELECT: str = (
    "Server, Partner, Partition, LastReplicationSuccess, "
    "LastReplicationAttempt, LastReplicationResult"
)


async def msad_domain_info(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.info`` — ``Get-ADDomain`` (FSMO + mode). Read-only."""
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _DOMAIN_INFO_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def msad_domain_forest(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.forest`` — ``Get-ADForest`` (FSMO + mode). Read-only."""
    del params
    payload = await pwsh_run(connector, target, _FOREST_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def msad_domain_controllers(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.controllers`` — ``Get-ADDomainController -Filter *`` (list)."""
    del params
    return await ad_list_read(
        connector,
        target,
        pipeline=f"Get-ADDomainController -Filter * | Select-Object {_DC_SELECT}",
        operator=operator,
    )


async def msad_domain_replication(
    connector: MsadConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``msad.domain.replication`` — inbound replication summary (list).

    Reads ``Get-ADReplicationPartnerMetadata -Target <domain> -Scope Domain``.
    The ``-Target`` is derived on the DC (``(Get-ADDomain).DNSRoot``), so the
    script is a constant — no injection surface. A single-DC lab domain has no
    replication partners; the ``{rows, total}`` envelope renders that as
    ``{rows: [], total: 0}`` rather than a false-empty failure.
    """
    del params
    return await ad_list_read(
        connector,
        target,
        prelude="$dom = (Get-ADDomain).DNSRoot; ",
        pipeline=(
            "Get-ADReplicationPartnerMetadata -Target $dom -Scope Domain "
            f"| Select-Object {_REPL_SELECT}"
        ),
        operator=operator,
    )


_EMPTY_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
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


DOMAIN_OPS: tuple[MsadOp, ...] = (
    MsadOp(
        op_id="msad.domain.info",
        handler_attr="msad_domain_info",
        summary="Read the AD domain projection (FSMO holders, mode, forest) via Get-ADDomain.",
        description=(
            "Runs ``Get-ADDomain`` and returns the domain DNS root, NetBIOS "
            "name, functional mode, forest, distinguished name, the three "
            "domain-level FSMO role holders (PDC emulator, RID master, "
            "infrastructure master), the domain SID, and the DC list. "
            "Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "DNSRoot": {"type": ["string", "null"]},
                "NetBIOSName": {"type": ["string", "null"]},
                "DomainMode": {"type": ["string", "null"]},
                "Forest": {"type": ["string", "null"]},
                "PDCEmulator": {"type": ["string", "null"]},
                "RIDMaster": {"type": ["string", "null"]},
                "InfrastructureMaster": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="domain",
        tags=("read-only", "domain", "fsmo"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for domain topology — DNS root, functional level, and "
                "the domain-level FSMO role holders (PDC emulator / RID / "
                "infrastructure master). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict: DNSRoot, NetBIOSName, DomainMode, Forest, "
                "PDCEmulator, RIDMaster, InfrastructureMaster, DomainSID, "
                "DomainControllers."
            ),
        },
    ),
    MsadOp(
        op_id="msad.domain.forest",
        handler_attr="msad_domain_forest",
        summary="Read the AD forest projection (schema/naming FSMO, mode) via Get-ADForest.",
        description=(
            "Runs ``Get-ADForest`` and returns the forest name, functional "
            "mode, root domain, the two forest-level FSMO role holders (schema "
            "master, domain-naming master), and the domain / global-catalog / "
            "site lists. Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "Name": {"type": ["string", "null"]},
                "ForestMode": {"type": ["string", "null"]},
                "RootDomain": {"type": ["string", "null"]},
                "SchemaMaster": {"type": ["string", "null"]},
                "DomainNamingMaster": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="domain",
        tags=("read-only", "forest", "fsmo"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for forest topology — functional level and the "
                "forest-level FSMO role holders (schema master / domain-naming "
                "master). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict: Name, ForestMode, RootDomain, SchemaMaster, "
                "DomainNamingMaster, Domains, GlobalCatalogs, Sites."
            ),
        },
    ),
    MsadOp(
        op_id="msad.domain.controllers",
        handler_attr="msad_domain_controllers",
        summary="List the domain controllers via Get-ADDomainController (name/site/roles).",
        description=(
            "Runs ``Get-ADDomainController -Filter *`` and returns one row per "
            "DC (Name, HostName, Site, IPv4Address, IsGlobalCatalog, "
            "IsReadOnly, OperatingSystem, OperationMasterRoles). Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="domain",
        tags=("read-only", "domain", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the domain controllers and which FSMO roles "
                "each holds. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Name, HostName, Site, IPv4Address, "
                "IsGlobalCatalog, OperationMasterRoles}], 'total': <int>}."
            ),
        },
    ),
    MsadOp(
        op_id="msad.domain.replication",
        handler_attr="msad_domain_replication",
        summary="Summarise inbound replication via Get-ADReplicationPartnerMetadata.",
        description=(
            "Runs ``Get-ADReplicationPartnerMetadata -Target <domain> -Scope "
            "Domain`` and returns one row per inbound replication partner "
            "(Server, Partner, Partition, LastReplicationSuccess, "
            "LastReplicationAttempt, LastReplicationResult). A single-DC domain "
            "returns an empty list. Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="domain",
        tags=("read-only", "domain", "replication"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to check AD replication health — the last replication "
                "success / attempt / result per partner. A non-zero "
                "LastReplicationResult flags a failing pairing. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Server, Partner, Partition, "
                "LastReplicationSuccess, LastReplicationResult}], 'total': "
                "<int>}. Empty on a single-DC domain."
            ),
        },
    ),
)
