# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""NSX Manager operation definitions for VMware connector.

All operations are read-only (D-26) and use the NSX Policy API v1 (D-25)
with Management API fallback for transport node details.
"""

from meho_app.modules.connectors.base import OperationDefinition

NSX_OPERATIONS = [
    # ------------------------------------------------------------------
    # Segments
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_segments",
        name="List NSX Segments",
        description=(
            "List NSX logical segments to trace VM-to-segment connectivity. "
            "Shows segment type (ROUTED/CONNECTED), VLAN IDs, subnets, and transport zone."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_segments()",
        response_entity_type="NsxSegment",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_nsx_segment",
        name="Get NSX Segment Details",
        description=(
            "Get detailed NSX segment info including segment ports. "
            "Use to investigate which VMs are connected to a specific network segment."
        ),
        category="networking",
        parameters=[
            {
                "name": "segment_id",
                "type": "string",
                "required": True,
                "description": "NSX segment ID",
            }
        ],
        example="get_nsx_segment(segment_id='web-segment-01')",
        response_entity_type="NsxSegment",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Firewall Policies & Rules
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_firewall_policies",
        name="List NSX Firewall Policies",
        description=(
            "List distributed firewall security policies and their rules to check "
            "if traffic is being blocked. Shows source/destination groups, actions, "
            "and services for each rule."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_firewall_policies()",
        response_entity_type="NsxFirewallPolicy",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_nsx_firewall_rule",
        name="Get NSX Firewall Rule Details",
        description=(
            "Get details of a specific distributed firewall rule including direction, "
            "IP protocol, profiles, and scope."
        ),
        category="networking",
        parameters=[
            {
                "name": "policy_id",
                "type": "string",
                "required": True,
                "description": "Security policy ID containing the rule",
            },
            {
                "name": "rule_id",
                "type": "string",
                "required": True,
                "description": "Firewall rule ID",
            },
        ],
        example="get_nsx_firewall_rule(policy_id='default-layer3-section', rule_id='rule-1')",
        response_entity_type="NsxFirewallRule",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Security Groups
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_security_groups",
        name="List NSX Security Groups",
        description=(
            "List NSX security groups with membership criteria. "
            "Groups define the source/destination in firewall rules -- "
            "check which VMs belong to which security groups."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_security_groups()",
        response_entity_type="NsxSecurityGroup",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Gateways
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_tier0_gateways",
        name="List NSX Tier-0 Gateways",
        description=(
            "List NSX Tier-0 gateways for north-south routing analysis. "
            "Tier-0 connects the NSX overlay to the physical network."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_tier0_gateways()",
        response_entity_type="NsxGateway",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="list_nsx_tier1_gateways",
        name="List NSX Tier-1 Gateways",
        description=(
            "List NSX Tier-1 gateways for east-west routing and micro-segmentation. "
            "Shows parent Tier-0, route advertisement types, and failover mode."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_tier1_gateways()",
        response_entity_type="NsxGateway",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Load Balancers
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_load_balancers",
        name="List NSX Load Balancers",
        description=(
            "List NSX load balancer services for traffic distribution analysis. "
            "Shows enabled state, size, and connectivity path."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_load_balancers()",
        response_entity_type="NsxLoadBalancer",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Transport Zones
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_transport_zones",
        name="List NSX Transport Zones",
        description=(
            "List NSX transport zones to understand overlay vs VLAN network topology. "
            "Shows transport type (OVERLAY/VLAN) and host switch binding."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_transport_zones()",
        response_entity_type="NsxTransportZone",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Transport Nodes
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="list_nsx_transport_nodes",
        name="List NSX Transport Nodes",
        description=(
            "List NSX transport nodes to verify host preparation and tunnel status. "
            "Uses Management API for full details."
        ),
        category="networking",
        parameters=[],
        example="list_nsx_transport_nodes()",
        response_entity_type="NsxTransportNode",
        response_identifier_field="node_id",
        response_display_name_field="display_name",
    ),
    OperationDefinition(
        operation_id="get_nsx_transport_node",
        name="Get NSX Transport Node Details",
        description=(
            "Get detailed transport node info including host switch spec and IP addresses. "
            "Uses Management API for full host preparation details."
        ),
        category="networking",
        parameters=[
            {
                "name": "node_id",
                "type": "string",
                "required": True,
                "description": "Transport node ID",
            }
        ],
        example="get_nsx_transport_node(node_id='tn-001')",
        response_entity_type="NsxTransportNode",
        response_identifier_field="node_id",
        response_display_name_field="display_name",
    ),
    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    OperationDefinition(
        operation_id="search_nsx",
        name="Search NSX Objects",
        description=(
            "Search NSX objects by query string for cross-system entity resolution. "
            "Optionally filter by resource_type (e.g. Segment, Group, FirewallRule)."
        ),
        category="networking",
        parameters=[
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "Search query string",
            },
            {
                "name": "resource_type",
                "type": "string",
                "required": False,
                "description": "NSX resource type filter (e.g. Segment, Group, FirewallRule)",
            },
        ],
        example="search_nsx(query='web-segment', resource_type='Segment')",
        response_entity_type="NsxSearchResult",
        response_identifier_field="id",
        response_display_name_field="display_name",
    ),
]
