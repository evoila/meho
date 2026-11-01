# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows, approval previews and grant sets of the six host security changes.

``_register.py`` collects :data:`COMPOSITES`, and ``_governed_subops.py`` reads
:data:`HOST_SECURITY_GOVERNED_SUBOPS`. All six changes are ``dangerous`` with
``requires_approval=True`` and sit in the ``vmware-host-security`` group with
the two reads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._host_access_schemas import (
    COREDUMP_SET,
    SNMP_SET,
)
from meho_backplane.connectors.vmware_rest.composites._host_access_writes import (
    OP_ID_COREDUMP_SET,
    OP_ID_LOCKDOWN_EXCEPTIONS_SET,
    OP_ID_LOCKDOWN_SET,
    OP_ID_SNMP_SET,
    VIM_SUB_OPS_COREDUMP_SET,
    VIM_SUB_OPS_LOCKDOWN_EXCEPTIONS_SET,
    VIM_SUB_OPS_LOCKDOWN_SET,
    VIM_SUB_OPS_SNMP_SET,
    host_coredump_set_composite,
    host_lockdown_exceptions_set_composite,
    host_lockdown_set_composite,
    host_snmp_set_composite,
    read_coredump,
    read_exceptions,
    read_lockdown,
    read_snmp,
    snmp_plan,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_flow import (
    Reader,
    preview_current,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_schemas import (
    ADVANCED_SETTINGS_SET,
    FIREWALL_RULESET_SET,
    LOCKDOWN_EXCEPTIONS_SET,
    LOCKDOWN_SET,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_writes import (
    OP_ID_ADVANCED_SETTINGS_SET,
    OP_ID_FIREWALL_RULESET_SET,
    VIM_SUB_OPS_ADVANCED_SETTINGS_SET,
    VIM_SUB_OPS_FIREWALL_RULESET_SET,
    hidden_keys,
    host_advanced_settings_set_composite,
    host_firewall_ruleset_set_composite,
    read_ruleset,
    read_settings,
    requested_settings,
    settings_view,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.host_security import HOST_SECURITY_GROUP_KEY
from meho_backplane.operations._preview import PreviewBuilder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["COMPOSITES", "HOST_SECURITY_GOVERNED_SUBOPS"]

#: composite op id -> the vim calls it may make.
HOST_SECURITY_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_ADVANCED_SETTINGS_SET: VIM_SUB_OPS_ADVANCED_SETTINGS_SET,
    OP_ID_FIREWALL_RULESET_SET: VIM_SUB_OPS_FIREWALL_RULESET_SET,
    OP_ID_LOCKDOWN_SET: VIM_SUB_OPS_LOCKDOWN_SET,
    OP_ID_LOCKDOWN_EXCEPTIONS_SET: VIM_SUB_OPS_LOCKDOWN_EXCEPTIONS_SET,
    OP_ID_SNMP_SET: VIM_SUB_OPS_SNMP_SET,
    OP_ID_COREDUMP_SET: VIM_SUB_OPS_COREDUMP_SET,
}


# ---------------------------------------------------------------------------
# Previews: the approver sees the host, its current setting and what is asked
# ---------------------------------------------------------------------------


def _advanced_settings_reader(params: dict[str, Any]) -> Reader:
    """Reads the current values; a secret setting shows ``<hidden>``."""
    keys = list(requested_settings(params.get("settings")))
    hidden = hidden_keys(params.get("settings"))

    async def read(
        connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
    ) -> Any:
        values = await read_settings(connector, operator, target, manager, keys)
        return settings_view(values, hidden)

    return read


def _ruleset_reader(params: dict[str, Any]) -> Reader:
    async def read(
        connector: VmwareRestConnector, operator: Operator, target: Any, manager: str
    ) -> Any:
        return await read_ruleset(connector, operator, target, manager, str(params["ruleset"]))

    return read


def _builder(
    manager: str,
    reader_for: Callable[[dict[str, Any]], Reader],
    requested: Callable[[dict[str, Any]], dict[str, Any]],
) -> PreviewBuilder:
    """A preview that reads the current setting with ``reader_for(params)``.

    *requested* turns the params into the asked change (never a secret).
    """

    async def build(ctx: PreviewContext) -> dict[str, Any] | None:
        return await preview_current(ctx, manager, reader_for(ctx.params), requested(ctx.params))

    return build


def _fixed(reader: Reader) -> Callable[[dict[str, Any]], Reader]:
    return lambda _params: reader


def _fields(*names: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda params: {name: params[name] for name in names if name in params}


_advanced_settings_preview = _builder(
    "advancedOption",
    _advanced_settings_reader,
    lambda p: {"settings": requested_settings(p.get("settings"))},
)
_firewall_ruleset_preview = _builder(
    "firewallSystem", _ruleset_reader, _fields("ruleset", "enabled", "allowed")
)
_lockdown_preview = _builder("hostAccessManager", _fixed(read_lockdown), _fields("mode"))
_lockdown_exceptions_preview = _builder(
    "hostAccessManager", _fixed(read_exceptions), _fields("users")
)
_snmp_preview = _builder("snmpSystem", _fixed(read_snmp), snmp_plan)
_coredump_preview = _builder(
    "diagnosticSystem", _fixed(read_coredump), _fields("disk_name", "partition")
)


# ---------------------------------------------------------------------------
# Registration rows
# ---------------------------------------------------------------------------


def _row(
    op_id: str,
    handler: Any,
    parts: dict[str, Any],
    summary: str,
    description: str,
    tags: list[str],
    preview: PreviewBuilder,
) -> CompositeSpec:
    return CompositeSpec(
        op_id=op_id,
        handler=handler,
        summary=summary,
        description=description,
        **parts,
        group_key=HOST_SECURITY_GROUP_KEY,
        tags=["composite", "write", "host", *tags, "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        preview=preview,
    )


COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    _row(
        OP_ID_ADVANCED_SETTINGS_SET,
        host_advanced_settings_set_composite,
        ADVANCED_SETTINGS_SET,
        "Set one or more advanced settings of one ESXi host.",
        "Sets advanced settings of one host. Checks each key, its type and range before "
        "sending; sends only the values that differ and reads them back. A setting that "
        "holds a secret takes its value from a Vault path and never shows it. Waits for "
        "approval.",
        ["advanced-settings"],
        _advanced_settings_preview,
    ),
    _row(
        OP_ID_FIREWALL_RULESET_SET,
        host_firewall_ruleset_set_composite,
        FIREWALL_RULESET_SET,
        "Turn one ESXi firewall rule set on or off, or set its allowed IP addresses.",
        "Turns one firewall rule set of a host on or off and sets who may connect to it. A "
        "required rule set is never turned off. Reads the rule set first and back after. "
        "Waits for approval.",
        ["firewall"],
        _firewall_ruleset_preview,
    ),
    _row(
        OP_ID_LOCKDOWN_SET,
        host_lockdown_set_composite,
        LOCKDOWN_SET,
        "Set the lockdown mode of one ESXi host: disabled, normal or strict.",
        "Sets the lockdown mode of one host. In lockdown only vCenter and the exception users "
        "reach the host; strict also stops the host console. Reads the mode back. Waits for "
        "approval.",
        ["lockdown", "security"],
        _lockdown_preview,
    ),
    _row(
        OP_ID_LOCKDOWN_EXCEPTIONS_SET,
        host_lockdown_exceptions_set_composite,
        LOCKDOWN_EXCEPTIONS_SET,
        "Set the users who keep access to one ESXi host in lockdown mode.",
        "Replaces the lockdown exception users of one host: the users who keep access when "
        "lockdown mode is on. Reads the list first and back after. Waits for approval.",
        ["lockdown", "security"],
        _lockdown_exceptions_preview,
    ),
    _row(
        OP_ID_SNMP_SET,
        host_snmp_set_composite,
        SNMP_SET,
        "Set the SNMP agent of one ESXi host: on or off, port, community and trap targets.",
        "Sets the SNMP agent of one host. The community comes from a Vault path and is never "
        "shown; fields left out keep their value. Reads the agent back. Waits for approval.",
        ["snmp", "monitoring"],
        _snmp_preview,
    ),
    _row(
        OP_ID_COREDUMP_SET,
        host_coredump_set_composite,
        COREDUMP_SET,
        "Choose the core dump partition of one ESXi host from its free partitions.",
        "Makes one free diagnostic partition the active core dump partition of a host. A "
        "partition that is not free is refused and the free ones are listed. Waits for "
        "approval.",
        ["coredump", "diagnostics"],
        _coredump_preview,
    ),
)
