# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""NSX Manager operations for VMware connector (Policy API v1).

Provides read-only network visibility operations via the NSX Policy API
(/policy/api/v1/) with Management API fallback for transport node details.
All methods reference ``self._nsx_client`` -- a :class:`VMwareRESTClient`
instance whose lifecycle is managed by :class:`VMwareConnector` (Plan 04).
"""

from __future__ import annotations

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

_NSX_NOT_CONFIGURED = (
    "NSX Manager not configured. "
    "Add nsx_host, nsx_username, nsx_password to this connector's credentials."
)


class NsxHandlerMixin:
    """NSX Manager network visibility operations (read-only).

    Covers segments, firewall policies/rules, security groups,
    Tier-0/Tier-1 gateways, load balancers, transport zones/nodes,
    and search.  All list operations use paginated_get for environments
    with >1000 entities.
    """

    # _nsx_client will be set by VMwareConnector.__init__ (Plan 04).
    # Declared here for IDE type-checking only.
    _nsx_client: Any

    def _nsx_available(self) -> bool:
        """Return True if the NSX REST client is connected."""
        return bool(self._nsx_client and self._nsx_client.is_connected)

    # ------------------------------------------------------------------
    # 1. Segments
    # ------------------------------------------------------------------

    async def _list_nsx_segments(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX logical segments to trace VM-to-segment connectivity."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_segments = await self._nsx_client.paginated_get("/policy/api/v1/infra/segments")
            return [
                {
                    "id": seg.get("id"),
                    "display_name": seg.get("display_name"),
                    "path": seg.get("path"),
                    "type": seg.get("type", "UNKNOWN"),
                    "transport_zone_path": seg.get("transport_zone_path"),
                    "vlan_ids": seg.get("vlan_ids", []),
                    "subnets": [
                        {
                            "gateway_address": s.get("gateway_address"),
                            "network": s.get("network"),
                        }
                        for s in seg.get("subnets", [])
                    ],
                    "admin_state": seg.get("admin_state"),
                }
                for seg in raw_segments
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX segments: {e}")
            raise RuntimeError(f"NSX list_segments failed: {e}") from e

    async def _get_nsx_segment(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed NSX segment info including ports."""
        if not self._nsx_available():
            return {"error": _NSX_NOT_CONFIGURED}

        segment_id = params.get("segment_id")
        if not segment_id:
            raise ValueError("segment_id is required")

        try:
            segment = await self._nsx_client.get(f"/policy/api/v1/infra/segments/{segment_id}")
            # Also fetch segment ports
            ports_raw = await self._nsx_client.paginated_get(
                f"/policy/api/v1/infra/segments/{segment_id}/ports"
            )
            ports = [
                {
                    "id": p.get("id"),
                    "display_name": p.get("display_name"),
                    "path": p.get("path"),
                    "attachment": p.get("attachment"),
                }
                for p in ports_raw
            ]
            return {
                "id": segment.get("id"),
                "display_name": segment.get("display_name"),
                "path": segment.get("path"),
                "type": segment.get("type", "UNKNOWN"),
                "transport_zone_path": segment.get("transport_zone_path"),
                "vlan_ids": segment.get("vlan_ids", []),
                "subnets": segment.get("subnets", []),
                "admin_state": segment.get("admin_state"),
                "segment_ports": ports,
            }
        except Exception as e:
            logger.error(f"Failed to get NSX segment {segment_id}: {e}")
            raise RuntimeError(f"NSX get_segment failed for {segment_id}: {e}") from e

    # ------------------------------------------------------------------
    # 2. Firewall Policies & Rules
    # ------------------------------------------------------------------

    async def _list_nsx_firewall_policies(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List distributed firewall policies and their rules to check if traffic is being blocked."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_policies = await self._nsx_client.paginated_get(
                "/policy/api/v1/infra/domains/default/security-policies"
            )
            results = []
            for policy in raw_policies:
                policy_id = policy.get("id", "")
                # Fetch rules for each policy
                raw_rules = await self._nsx_client.paginated_get(
                    f"/policy/api/v1/infra/domains/default/security-policies/{policy_id}/rules"
                )
                rules = [
                    {
                        "id": r.get("id"),
                        "display_name": r.get("display_name"),
                        "action": r.get("action"),
                        "source_groups": r.get("source_groups", []),
                        "destination_groups": r.get("destination_groups", []),
                        "services": r.get("services", []),
                        "logged": r.get("logged", False),
                        "disabled": r.get("disabled", False),
                    }
                    for r in raw_rules
                ]
                results.append(
                    {
                        "id": policy_id,
                        "display_name": policy.get("display_name"),
                        "category": policy.get("category"),
                        "scope": policy.get("scope", []),
                        "rules": rules,
                    }
                )
            return results
        except Exception as e:
            logger.error(f"Failed to list NSX firewall policies: {e}")
            raise RuntimeError(f"NSX list_firewall_policies failed: {e}") from e

    async def _get_nsx_firewall_rule(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get details of a specific distributed firewall rule."""
        if not self._nsx_available():
            return {"error": _NSX_NOT_CONFIGURED}

        policy_id = params.get("policy_id")
        rule_id = params.get("rule_id")
        if not policy_id or not rule_id:
            raise ValueError("policy_id and rule_id are required")

        try:
            rule = await self._nsx_client.get(
                f"/policy/api/v1/infra/domains/default/security-policies/{policy_id}/rules/{rule_id}"
            )
            return {
                "id": rule.get("id"),
                "display_name": rule.get("display_name"),
                "action": rule.get("action"),
                "direction": rule.get("direction"),
                "ip_protocol": rule.get("ip_protocol"),
                "source_groups": rule.get("source_groups", []),
                "destination_groups": rule.get("destination_groups", []),
                "services": rule.get("services", []),
                "profiles": rule.get("profiles", []),
                "scope": rule.get("scope", []),
                "logged": rule.get("logged", False),
                "disabled": rule.get("disabled", False),
                "sequence_number": rule.get("sequence_number"),
                "tag": rule.get("tag"),
            }
        except Exception as e:
            logger.error(f"Failed to get NSX firewall rule {policy_id}/{rule_id}: {e}")
            raise RuntimeError(
                f"NSX get_firewall_rule failed for {policy_id}/{rule_id}: {e}"
            ) from e

    # ------------------------------------------------------------------
    # 3. Security Groups
    # ------------------------------------------------------------------

    async def _list_nsx_security_groups(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX security groups with membership criteria for firewall policy analysis."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_groups = await self._nsx_client.paginated_get(
                "/policy/api/v1/infra/domains/default/groups"
            )
            return [
                {
                    "id": g.get("id"),
                    "display_name": g.get("display_name"),
                    "expression": g.get("expression", []),
                    "path": g.get("path"),
                }
                for g in raw_groups
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX security groups: {e}")
            raise RuntimeError(f"NSX list_security_groups failed: {e}") from e

    # ------------------------------------------------------------------
    # 4. Gateways (Tier-0 and Tier-1)
    # ------------------------------------------------------------------

    async def _list_nsx_tier0_gateways(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX Tier-0 gateways for north-south routing analysis."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_gateways = await self._nsx_client.paginated_get("/policy/api/v1/infra/tier-0s")
            return [
                {
                    "id": gw.get("id"),
                    "display_name": gw.get("display_name"),
                    "ha_mode": gw.get("ha_mode"),
                    "failover_mode": gw.get("failover_mode"),
                    "transit_subnets": gw.get("transit_subnets", []),
                }
                for gw in raw_gateways
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX Tier-0 gateways: {e}")
            raise RuntimeError(f"NSX list_tier0_gateways failed: {e}") from e

    async def _list_nsx_tier1_gateways(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX Tier-1 gateways for east-west routing and micro-segmentation analysis."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_gateways = await self._nsx_client.paginated_get("/policy/api/v1/infra/tier-1s")
            return [
                {
                    "id": gw.get("id"),
                    "display_name": gw.get("display_name"),
                    "tier0_path": gw.get("tier0_path"),
                    "route_advertisement_types": gw.get("route_advertisement_types", []),
                    "failover_mode": gw.get("failover_mode"),
                }
                for gw in raw_gateways
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX Tier-1 gateways: {e}")
            raise RuntimeError(f"NSX list_tier1_gateways failed: {e}") from e

    # ------------------------------------------------------------------
    # 5. Load Balancers
    # ------------------------------------------------------------------

    async def _list_nsx_load_balancers(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX load balancer services for traffic distribution analysis."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_lbs = await self._nsx_client.paginated_get("/policy/api/v1/infra/lb-services")
            return [
                {
                    "id": lb.get("id"),
                    "display_name": lb.get("display_name"),
                    "enabled": lb.get("enabled"),
                    "size": lb.get("size"),
                    "connectivity_path": lb.get("connectivity_path"),
                    "error_log_level": lb.get("error_log_level"),
                }
                for lb in raw_lbs
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX load balancers: {e}")
            raise RuntimeError(f"NSX list_load_balancers failed: {e}") from e

    # ------------------------------------------------------------------
    # 6. Transport Zones
    # ------------------------------------------------------------------

    async def _list_nsx_transport_zones(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX transport zones to understand overlay/VLAN network topology."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            raw_tzs = await self._nsx_client.paginated_get(
                "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"
            )
            return [
                {
                    "id": tz.get("id"),
                    "display_name": tz.get("display_name"),
                    "transport_type": tz.get("transport_type"),
                    "host_switch_name": tz.get("host_switch_name"),
                }
                for tz in raw_tzs
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX transport zones: {e}")
            raise RuntimeError(f"NSX list_transport_zones failed: {e}") from e

    # ------------------------------------------------------------------
    # 7. Transport Nodes (Management API fallback per D-25)
    # ------------------------------------------------------------------

    async def _list_nsx_transport_nodes(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        """List NSX transport nodes to verify host preparation and tunnel status."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        try:
            # Transport node details not fully available via Policy API (D-25),
            # use Management API fallback: GET /api/v1/transport-nodes
            raw_nodes = await self._nsx_client.paginated_get("/api/v1/transport-nodes")
            return [
                {
                    "node_id": n.get("node_id"),
                    "display_name": n.get("display_name"),
                    "node_deployment_info": n.get("node_deployment_info"),
                    "maintenance_mode": n.get("maintenance_mode"),
                    "resource_type": n.get("resource_type"),
                }
                for n in raw_nodes
            ]
        except Exception as e:
            logger.error(f"Failed to list NSX transport nodes: {e}")
            raise RuntimeError(f"NSX list_transport_nodes failed: {e}") from e

    async def _get_nsx_transport_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed transport node info including host switch spec and IP addresses."""
        if not self._nsx_available():
            return {"error": _NSX_NOT_CONFIGURED}

        node_id = params.get("node_id")
        if not node_id:
            raise ValueError("node_id is required")

        try:
            node = await self._nsx_client.get(f"/api/v1/transport-nodes/{node_id}")
            return {
                "node_id": node.get("node_id"),
                "display_name": node.get("display_name"),
                "node_deployment_info": node.get("node_deployment_info"),
                "host_switch_spec": node.get("host_switch_spec"),
                "maintenance_mode": node.get("maintenance_mode"),
                "resource_type": node.get("resource_type"),
                "ip_addresses": node.get("ip_addresses", []),
            }
        except Exception as e:
            logger.error(f"Failed to get NSX transport node {node_id}: {e}")
            raise RuntimeError(f"NSX get_transport_node failed for {node_id}: {e}") from e

    # ------------------------------------------------------------------
    # 8. Search
    # ------------------------------------------------------------------

    async def _search_nsx(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Search NSX objects by query string for cross-system entity resolution."""
        if not self._nsx_available():
            return [{"error": _NSX_NOT_CONFIGURED}]

        query = params.get("query")
        if not query:
            raise ValueError("query is required")

        resource_type = params.get("resource_type")
        search_query = f"resource_type:{resource_type} AND {query}" if resource_type else query

        try:
            data = await self._nsx_client.get(
                "/policy/api/v1/search/query",
                params={"query": search_query},
            )
            results: list[dict[str, Any]] = data.get("results", [])
            return results
        except Exception as e:
            logger.error(f"NSX search failed for query '{query}': {e}")
            raise RuntimeError(f"NSX search failed for '{query}': {e}") from e
