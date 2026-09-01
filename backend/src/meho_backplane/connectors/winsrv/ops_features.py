# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv feature ops — ``list`` (safe) + ``install`` / ``remove`` (caution).

The Windows Server role/feature surface is driven by the ``ServerManager``
module (``Get-WindowsFeature`` / ``Install-WindowsFeature`` /
``Uninstall-WindowsFeature``) over the shared PowerShell-over-SSH transport.
This is the c1sql1 ``Install-WindowsFeature`` path
(evoila-bosnia/claude-rdc-hetzner-dc#2789) — the Failover-Clustering role
install that today rides ungoverned ``govc guest.run``.

``install`` / ``remove`` are ``caution`` (recoverable — a removed feature can
be reinstalled). They deliberately **never** pass ``-Restart``: a reboot is a
``dangerous`` + ``requires_approval`` action, so the handlers return
``restart_needed`` and leave the reboot to the approval-gated
``winsrv.power.reboot`` op. Batch feature changes, then take one governed
reboot.

PowerShell injection safety
---------------------------

The operator-supplied feature ``name`` is interpolated only inside a
single-quoted PowerShell literal via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`. The boolean
toggles are rendered as PowerShell ``$true`` / ``$false`` literals after
Python-side validation, never as interpolated operator text.

References
----------

* ``Get-WindowsFeature`` / ``Install-WindowsFeature`` /
  ``Uninstall-WindowsFeature`` (ServerManager, Windows Server 2022):
  https://learn.microsoft.com/en-us/powershell/module/servermanager/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "FEATURE_OPS",
    "winsrv_feature_install",
    "winsrv_feature_list",
    "winsrv_feature_remove",
]

_FEATURE_SELECT: str = "Name, DisplayName, InstallState, Installed"


def _ps_bool(value: Any) -> str:
    """Render *value* as a PowerShell ``$true`` / ``$false`` literal.

    ``value`` is a JSON-schema-validated boolean (or absent → treated as
    ``False``). Rendering to a fixed literal — never the interpolated
    operator text — keeps the toggle off the injection surface entirely.
    """
    return "$true" if value is True else "$false"


async def winsrv_feature_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.feature.list`` — every role/feature (list-shaped).

    Runs ``Get-WindowsFeature`` and returns ``{rows, total}`` (each row
    carries ``Name`` / ``DisplayName`` / ``InstallState`` / ``Installed``).
    Read-only. Requires the ServerManager module (present on Windows Server).
    """
    del params  # declared empty in schema; intentionally ignored
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$f = @(Get-WindowsFeature | Select-Object {_FEATURE_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $f; total = $f.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def winsrv_feature_install(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.feature.install`` (caution) — ``Install-WindowsFeature``.

    Installs the named feature (with optional ``-IncludeManagementTools`` /
    ``-IncludeAllSubFeature``) under ``$ErrorActionPreference = 'Stop'``.
    Never passes ``-Restart`` — returns ``restart_needed`` and leaves the
    reboot to the approval-gated ``winsrv.power.reboot``. Returns the
    cmdlet's ``Success`` / ``ExitCode`` / ``RestartNeeded`` plus the changed
    feature names.
    """
    name: str = params["name"]
    mgmt_tools = _ps_bool(params.get("include_management_tools"))
    sub_features = _ps_bool(params.get("include_all_sub_feature"))
    quoted = ps_single_quote(name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$r = Install-WindowsFeature -Name {quoted} "
        f"-IncludeManagementTools:{mgmt_tools} -IncludeAllSubFeature:{sub_features}; "
        "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
        "ok = [bool]$r.Success; success = [bool]$r.Success; "
        "exit_code = $r.ExitCode.ToString(); "
        "restart_needed = ($r.RestartNeeded.ToString() -ne 'No'); "
        "features_changed = @($r.FeatureResult | ForEach-Object { $_.Name }) }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return _feature_write_result(name, "install", payload)


async def winsrv_feature_remove(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.feature.remove`` (caution) — ``Uninstall-WindowsFeature``.

    Removes the named feature under ``$ErrorActionPreference = 'Stop'``.
    Never passes ``-Restart`` — returns ``restart_needed`` and leaves the
    reboot to the approval-gated ``winsrv.power.reboot``.
    """
    name: str = params["name"]
    mgmt_tools = _ps_bool(params.get("include_management_tools"))
    quoted = ps_single_quote(name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$r = Uninstall-WindowsFeature -Name {quoted} "
        f"-IncludeManagementTools:{mgmt_tools}; "
        "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
        "ok = [bool]$r.Success; success = [bool]$r.Success; "
        "exit_code = $r.ExitCode.ToString(); "
        "restart_needed = ($r.RestartNeeded.ToString() -ne 'No'); "
        "features_changed = @($r.FeatureResult | ForEach-Object { $_.Name }) }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return _feature_write_result(name, "remove", payload)


def _feature_write_result(name: str, action: str, payload: Any) -> dict[str, Any]:
    """Shape the install/remove cmdlet payload into the write envelope."""
    data = payload if isinstance(payload, dict) else {}
    changed = data.get("features_changed")
    return {
        "name": name,
        "action": action,
        "success": bool(data.get("success")),
        "exit_code": data.get("exit_code"),
        "restart_needed": bool(data.get("restart_needed")),
        "features_changed": changed if isinstance(changed, list) else [],
        "op_class": "write",
    }


_FEATURE_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The Windows feature/role short name (the ``Name`` column, e.g. "
        "``Failover-Clustering`` or ``Web-Server``)."
    ),
}

_FEATURE_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "action": {"type": "string"},
        "success": {"type": "boolean"},
        "exit_code": {"type": ["string", "null"]},
        "restart_needed": {"type": "boolean"},
        "features_changed": {"type": "array", "items": {"type": "string"}},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["name", "action", "success", "op_class"],
    "additionalProperties": True,
}


FEATURE_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.feature.list",
        handler_attr="winsrv_feature_list",
        summary="List Windows roles/features via ``Get-WindowsFeature`` (name/install-state).",
        description=(
            "Runs ``Get-WindowsFeature`` and returns one row per role/feature "
            "(Name, DisplayName, InstallState, Installed). Read-only. "
            "Requires the ServerManager module (present on Windows Server, "
            "absent on client SKUs)."
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
        group_key="features",
        tags=("read-only", "feature", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate Windows roles/features and their install "
                "state (e.g. is ``Failover-Clustering`` installed?) before an "
                "install/remove. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{Name, DisplayName, InstallState, Installed}], 'total': <int>}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.feature.install",
        handler_attr="winsrv_feature_install",
        summary="Install a Windows role/feature via ``Install-WindowsFeature`` (caution).",
        description=(
            "Runs ``Install-WindowsFeature -Name <name>`` with optional "
            "``-IncludeManagementTools`` / ``-IncludeAllSubFeature``. Never "
            "auto-restarts — returns ``restart_needed`` and leaves the reboot "
            "to the approval-gated ``winsrv.power.reboot`` (batch feature "
            "changes, then one governed reboot). safety_level=caution. This "
            "is the c1sql1 Failover-Clustering install path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _FEATURE_NAME_PROP,
                "include_management_tools": {
                    "type": "boolean",
                    "description": (
                        "Also install the feature's management tools "
                        "(``-IncludeManagementTools``). Default false — the "
                        "cmdlet default."
                    ),
                },
                "include_all_sub_feature": {
                    "type": "boolean",
                    "description": (
                        "Also install all sub-features (``-IncludeAllSubFeature``). Default false."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_FEATURE_WRITE_RESPONSE_SCHEMA,
        group_key="features",
        tags=("write", "feature", "install"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Install a Windows role/feature (e.g. ``Failover-Clustering`` "
                "with ``include_management_tools=true`` on a SQL FCI node). "
                "Recoverable; safety_level=caution. Does NOT restart — check "
                "``restart_needed`` and use ``winsrv.power.reboot`` (approval-"
                "gated) if set. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The feature short name.",
                "include_management_tools": "Optional bool; default false.",
                "include_all_sub_feature": "Optional bool; default false.",
            },
            "output_shape": (
                "{'name', 'action': 'install', 'success', 'exit_code', "
                "'restart_needed', 'features_changed': [names], 'op_class': 'write'}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.feature.remove",
        handler_attr="winsrv_feature_remove",
        summary="Remove a Windows role/feature via ``Uninstall-WindowsFeature`` (caution).",
        description=(
            "Runs ``Uninstall-WindowsFeature -Name <name>`` (optionally with "
            "``-IncludeManagementTools`` to also remove the tools). Never "
            "auto-restarts — returns ``restart_needed``. safety_level=caution "
            "(recoverable — the feature can be reinstalled)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _FEATURE_NAME_PROP,
                "include_management_tools": {
                    "type": "boolean",
                    "description": (
                        "Also remove the feature's management tools "
                        "(``-IncludeManagementTools``). Default false."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_FEATURE_WRITE_RESPONSE_SCHEMA,
        group_key="features",
        tags=("write", "feature", "remove"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Remove a Windows role/feature. Recoverable; "
                "safety_level=caution. Does NOT restart — check "
                "``restart_needed``. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The feature short name.",
                "include_management_tools": "Optional bool; default false.",
            },
            "output_shape": (
                "{'name', 'action': 'remove', 'success', 'exit_code', "
                "'restart_needed', 'features_changed': [names], 'op_class': 'write'}."
            ),
        },
    ),
)
