# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""DNS resolution tool - Resolve hostnames to IP addresses and DNS records.

Uses aiodns for async DNS queries supporting A, AAAA, CNAME, MX, SRV, TXT,
NS, and SOA record types. Emits IPAddress and ExternalURL topology entities
for future cross-connector correlation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import aiodns
from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = logging.getLogger(__name__)


class DnsResolveInput(BaseModel):
    """Input for dns_resolve tool."""

    hostname: str = Field(description="Hostname to resolve (e.g., 'example.com')")
    record_types: list[str] = Field(
        default=["A", "AAAA"],
        description="DNS record types to query (A, AAAA, CNAME, MX, SRV, TXT, NS, SOA)",
    )


class DnsResolveOutput(BaseModel):
    """Output from dns_resolve tool."""

    hostname: str = Field(description="The queried hostname")
    records: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="Records grouped by type, e.g. {'A': [{'host': '1.2.3.4'}]}",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-record-type error messages",
    )
    success: bool = Field(default=False, description="True if at least one record type resolved")


@dataclass
class DnsResolveTool(BaseTool[DnsResolveInput, DnsResolveOutput]):
    """Resolve DNS records for a hostname.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "dns_resolve"
    TOOL_DESCRIPTION: ClassVar[str] = """Resolve DNS records for a hostname.
Returns A, AAAA, CNAME, MX, SRV, TXT, NS, SOA records.
Use to find IP addresses, mail servers, service endpoints, or verify DNS configuration."""
    InputSchema: ClassVar[type[BaseModel]] = DnsResolveInput
    OutputSchema: ClassVar[type[BaseModel]] = DnsResolveOutput

    async def execute(
        self,
        tool_input: DnsResolveInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> DnsResolveOutput:
        """Execute DNS resolution for the given hostname."""
        await emitter.tool_start(self.TOOL_NAME)

        resolver = aiodns.DNSResolver()
        records: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []

        for rtype in tool_input.record_types:
            rtype_upper = rtype.upper()
            try:
                result = await resolver.query(tool_input.hostname, rtype_upper)
                parsed = _parse_dns_result(rtype_upper, result)
                if parsed:
                    records[rtype_upper] = parsed
            except aiodns.error.DNSError as e:
                errors.append(f"{rtype_upper}: {e.args[1] if len(e.args) > 1 else str(e)}")
            except Exception as e:
                errors.append(f"{rtype_upper}: {e}")

        success = len(records) > 0

        # Fire-and-forget topology emission
        if success:
            try:
                await _emit_topology(tool_input.hostname, records, deps)
            except Exception:
                logger.debug(
                    "Topology emission failed for dns_resolve(%s), continuing",
                    tool_input.hostname,
                    exc_info=True,
                )

        await emitter.tool_complete(self.TOOL_NAME, success=success)
        return DnsResolveOutput(
            hostname=tool_input.hostname,
            records=records,
            errors=errors,
            success=success,
        )


def _parse_dns_result(rtype: str, result: Any) -> list[dict[str, Any]]:
    """Parse aiodns query result into a list of dicts."""
    parsed: list[dict[str, Any]] = []

    if rtype in ("A", "AAAA"):
        for r in result:
            parsed.append({"host": r.host})
    elif rtype == "CNAME":
        # aiodns returns a single result for CNAME
        parsed.append({"cname": result.cname})
    elif rtype == "MX":
        for r in result:
            parsed.append({"host": r.host, "priority": r.priority})
    elif rtype == "SRV":
        for r in result:
            parsed.append({
                "host": r.host,
                "port": r.port,
                "priority": r.priority,
                "weight": r.weight,
            })
    elif rtype == "TXT":
        for r in result:
            parsed.append({"text": r.text})
    elif rtype == "NS":
        for r in result:
            parsed.append({"host": r.host})
    elif rtype == "SOA":
        parsed.append({
            "nsname": result.nsname,
            "hostmaster": result.hostmaster,
            "serial": result.serial,
            "refresh": result.refresh,
            "retry": result.retry,
            "expires": result.expires,
            "minttl": result.minttl,
        })

    return parsed


async def _emit_topology(
    hostname: str,
    records: dict[str, list[dict[str, Any]]],
    deps: Any,
) -> None:
    """Emit topology entities for resolved DNS records (fire-and-forget)."""
    meho_deps = getattr(deps, "meho_deps", None)
    if not meho_deps:
        return

    session = getattr(meho_deps, "db_session", None)
    if not session:
        return

    tenant_id = getattr(meho_deps, "tenant_id", "default")

    from meho_app.modules.topology.schemas import (
        StoreDiscoveryInput,
        TopologyEntityCreate,
        TopologyRelationshipCreate,
    )
    from meho_app.modules.topology.service import TopologyService

    now_iso = datetime.now(UTC).isoformat()
    entities: list[TopologyEntityCreate] = []
    relationships: list[TopologyRelationshipCreate] = []

    # Emit ExternalURL entity for the hostname itself
    hostname_canonical = hostname.lower()
    entities.append(
        TopologyEntityCreate(
            name=hostname,
            entity_type="ExternalURL",
            connector_type="network_diagnostics",
            canonical_id=hostname_canonical,
            description=f"DNS-resolved hostname: {hostname}",
            raw_attributes={"url": hostname, "hostname": hostname, "last_resolved": now_iso},
        )
    )

    # Emit IPAddress entities for A/AAAA records
    for rtype in ("A", "AAAA"):
        for rec in records.get(rtype, []):
            ip = rec.get("host", "")
            if not ip:
                continue
            entities.append(
                TopologyEntityCreate(
                    name=ip,
                    entity_type="IPAddress",
                    connector_type="network_diagnostics",
                    canonical_id=ip,
                    description=f"IP address {ip} resolved from {hostname} ({rtype} record)",
                    raw_attributes={
                        "ip": ip,
                        "hostname": hostname,
                        "record_type": rtype,
                        "last_resolved": now_iso,
                    },
                )
            )
            # RESOLVES_TO relationship: hostname -> IP
            relationships.append(
                TopologyRelationshipCreate(
                    from_entity_name=hostname,
                    to_entity_name=ip,
                    relationship_type="resolves_to",
                )
            )

    if entities:
        service = TopologyService(session)
        await service.store_discovery(
            input=StoreDiscoveryInput(
                connector_type="network_diagnostics",
                entities=entities,
                relationships=relationships,
            ),
            tenant_id=tenant_id,
        )
