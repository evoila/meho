# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows and approval previews of the host NTP, DNS and syslog changes.

``_register.py`` collects :data:`COMPOSITES`, and ``_governed_subops.py`` reads
:data:`HOST_CONFIG_GOVERNED_SUBOPS`. All three changes are ``dangerous`` with
``requires_approval=True``. The handlers are in :mod:`._host_config_writes`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._host_config_write_schemas import (
    DNS_SET_LLM_INSTRUCTIONS,
    DNS_SET_PARAMETER_SCHEMA,
    DNS_SET_RESPONSE_SCHEMA,
    NTP_SET_LLM_INSTRUCTIONS,
    NTP_SET_PARAMETER_SCHEMA,
    NTP_SET_RESPONSE_SCHEMA,
    SYSLOG_SET_LLM_INSTRUCTIONS,
    SYSLOG_SET_PARAMETER_SCHEMA,
    SYSLOG_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._host_config_writes import (
    OP_ID_DNS_SET,
    OP_ID_NTP_SET,
    OP_ID_SYSLOG_SET,
    VIM_SUB_OPS_HOST_DNS_SET,
    VIM_SUB_OPS_HOST_NTP_SET,
    VIM_SUB_OPS_HOST_SYSLOG_SET,
    _ntp_reader,
    host_dns_set_composite,
    host_ntp_set_composite,
    host_syslog_set_composite,
    locate_host_managers,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.host_config import (
    SYSLOG_LOG_HOST_KEY,
    dns_state,
    query_option,
    read_property,
    syslog_state,
)
from meho_backplane.operations._preview import (
    PreviewBuilder,
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["COMPOSITES", "HOST_CONFIG_GOVERNED_SUBOPS"]

#: composite op id -> the vim calls it may make.
HOST_CONFIG_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_NTP_SET: VIM_SUB_OPS_HOST_NTP_SET,
    OP_ID_DNS_SET: VIM_SUB_OPS_HOST_DNS_SET,
    OP_ID_SYSLOG_SET: VIM_SUB_OPS_HOST_SYSLOG_SET,
}


# ---------------------------------------------------------------------------
# Previews: the approver sees the host, what it has now and what is asked
# ---------------------------------------------------------------------------


async def _preview_current(
    ctx: PreviewContext,
    needed: tuple[str, ...],
    read: Callable[[Any, Any, dict[str, str]], Awaitable[Any]],
) -> dict[str, Any] | None:
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    asked = {k: v for k, v in ctx.params.items() if k != "host"}
    try:
        host_moid, managers, refusal = await locate_host_managers(
            connector, ctx.operator, ctx.target, ctx.params.get("host"), needed
        )
        if refusal is not None:
            return {
                "summary": f"MEHO could not find the host ({refusal['status']}).",
                "requested": asked,
            }
        current = await read(connector, host_moid, managers)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the host, so it cannot show its settings.",
            "requested": asked,
            "read_error": preview_failure_reason(exc),
        }
    return {
        "summary": f"Change the settings of host {host_moid}.",
        "host": host_moid,
        "current": current,
        "requested": asked,
    }


def _preview_builder(
    manager: str, mo_type: str, prop: str, parse: Callable[[Any], dict[str, Any]]
) -> PreviewBuilder:
    async def build(ctx: PreviewContext) -> dict[str, Any] | None:
        async def read(connector: Any, host_moid: Any, managers: dict[str, str]) -> Any:
            if mo_type == "OptionManager":
                value = await query_option(
                    connector, ctx.operator, ctx.target, managers[manager], prop
                )
                return parse(value)
            value = await read_property(
                connector, ctx.operator, ctx.target, mo_type, managers[manager], prop
            )
            return parse(value)

        return await _preview_current(ctx, (manager,), read)

    return build


async def _ntp_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The NTP preview also shows the ntpd start policy and whether ntpd runs."""

    async def read(connector: Any, _host_moid: Any, managers: dict[str, str]) -> Any:
        return await _ntp_reader(connector, ctx.operator, ctx.target, managers)()

    return await _preview_current(ctx, ("dateTimeSystem", "serviceSystem"), read)


_dns_preview = _preview_builder("networkSystem", "HostNetworkSystem", "dnsConfig", dns_state)
_syslog_preview = _preview_builder(
    "advancedOption", "OptionManager", SYSLOG_LOG_HOST_KEY, syslog_state
)


# ---------------------------------------------------------------------------
# Registration rows
# ---------------------------------------------------------------------------

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_NTP_SET,
        handler=host_ntp_set_composite,
        summary="Set the NTP servers of one ESXi host and make sure ntpd runs.",
        description=(
            "Sets the NTP server list of one host, sets ntpd to start with the host, and "
            "restarts ntpd (or starts it when it is stopped). Reads the settings first and "
            "sends nothing when they already match; reads them back after. Waits for approval."
        ),
        parameter_schema=NTP_SET_PARAMETER_SCHEMA,
        response_schema=NTP_SET_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "ntp", "time", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=NTP_SET_LLM_INSTRUCTIONS,
        preview=_ntp_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_DNS_SET,
        handler=host_dns_set_composite,
        summary="Set the DNS servers, search domains, host name or domain of one ESXi host.",
        description=(
            "Sets the DNS servers, search domains, host name and domain name of one host; "
            "fields left out keep their value. Refused with DHCP DNS. Reads the settings "
            "first and back after; sends nothing when they match. Waits for approval."
        ),
        parameter_schema=DNS_SET_PARAMETER_SCHEMA,
        response_schema=DNS_SET_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "dns", "hostname", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=DNS_SET_LLM_INSTRUCTIONS,
        preview=_dns_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_SYSLOG_SET,
        handler=host_syslog_set_composite,
        summary="Set the remote syslog targets of one ESXi host (Syslog.global.logHost).",
        description=(
            "Sets where one host sends its logs: the Syslog.global.logHost advanced option. "
            "The list replaces the current one; an empty list removes all. Reads the value "
            "first and back after; sends nothing when it matches. Waits for approval."
        ),
        parameter_schema=SYSLOG_SET_PARAMETER_SCHEMA,
        response_schema=SYSLOG_SET_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "syslog", "logging", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SYSLOG_SET_LLM_INSTRUCTIONS,
        preview=_syslog_preview,
    ),
)
