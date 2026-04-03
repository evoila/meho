# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

NAME_OF_DISTRIBUTED_SWITCH = "Name of distributed switch"
NAME_OF_ESXI_HOST = "Name of ESXi host"
NAME_OF_THE_VM = "Name of the VM"

# NETWORKING OPERATIONS

NETWORKING_OPERATIONS = [
    OperationDefinition(
        operation_id="list_networks",
        name="List Networks",
        description="Get all networks in vCenter.",
        category="networking",
        parameters=[],
        example="list_networks()",
        response_entity_type="Network",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_vm_nics",
        name="Get VM Network Adapters",
        description="List all network adapters attached to a VM with MAC and network info.",
        category="networking",
        parameters=[
            {"name": "vm_name", "type": "string", "required": True, "description": NAME_OF_THE_VM}
        ],
        example="get_vm_nics(vm_name='web-01')",
        response_entity_type="NetworkAdapter",
        response_identifier_field="key",
        response_display_name_field="label",
    ),
    OperationDefinition(
        operation_id="get_host_networks",
        name="Get Host Networks",
        description="List all networks available on a specific ESXi host.",
        category="networking",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            }
        ],
        example="get_host_networks(host_name='esxi-01.example.com')",
        response_entity_type="Network",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_distributed_switches",
        name="List Distributed Switches",
        description="List all vSphere Distributed Switches in vCenter.",
        category="networking",
        parameters=[],
        example="list_distributed_switches()",
        response_entity_type="DistributedVirtualSwitch",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_distributed_switch",
        name="Get Distributed Switch Details",
        description="Get detailed configuration of a distributed switch.",
        category="networking",
        parameters=[
            {
                "name": "dvs_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_DISTRIBUTED_SWITCH,
            }
        ],
        example="get_distributed_switch(dvs_name='DSwitch-Production')",
        response_entity_type="DistributedVirtualSwitch",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_port_groups",
        name="List Port Groups",
        description="List all port groups (standard and distributed).",
        category="networking",
        parameters=[],
        example="list_port_groups()",
        response_entity_type="PortGroup",
        response_identifier_field="key",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_port_group",
        name="Get Port Group Details",
        description="Get detailed configuration of a port group including VLAN.",
        category="networking",
        parameters=[
            {
                "name": "portgroup_name",
                "type": "string",
                "required": True,
                "description": "Name of port group",
            }
        ],
        example="get_port_group(portgroup_name='VM-Network')",
        response_entity_type="PortGroup",
        response_identifier_field="key",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="create_dvs_portgroup",
        name="Create DVS Port Group",
        description="Create a new distributed port group on a DVS. pyvmomi: CreateDVPortgroup_Task(spec)",
        category="networking",
        parameters=[
            {
                "name": "dvs_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_DISTRIBUTED_SWITCH,
            },
            {
                "name": "portgroup_name",
                "type": "string",
                "required": True,
                "description": "Name for new port group",
            },
            {
                "name": "vlan_id",
                "type": "integer",
                "required": False,
                "description": "VLAN ID (0 for none)",
            },
            {
                "name": "num_ports",
                "type": "integer",
                "required": False,
                "description": "Number of ports (default: 8)",
            },
        ],
        example="create_dvs_portgroup(dvs_name='DSwitch-Prod', portgroup_name='VLAN-100', vlan_id=100)",
    ),
    OperationDefinition(
        operation_id="destroy_dvs_portgroup",
        name="Delete DVS Port Group",
        description="Delete a distributed port group. Must have no connected VMs. pyvmomi: Destroy_Task()",
        category="networking",
        parameters=[
            {
                "name": "portgroup_name",
                "type": "string",
                "required": True,
                "description": "Name of port group to delete",
            },
        ],
        example="destroy_dvs_portgroup(portgroup_name='old-portgroup')",
    ),
    OperationDefinition(
        operation_id="query_used_vlans",
        name="Query Used VLANs",
        description="Get list of VLAN IDs in use on a DVS. pyvmomi: QueryUsedVlanIdInDvs()",
        category="networking",
        parameters=[
            {
                "name": "dvs_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_DISTRIBUTED_SWITCH,
            },
        ],
        example="query_used_vlans(dvs_name='DSwitch-Prod')",
        response_entity_type="VlanId",
        response_identifier_field="vlan_id",
        response_display_name_field="vlan_id",
    ),
    OperationDefinition(
        operation_id="refresh_dvs_port_state",
        name="Refresh DVS Port State",
        description="Refresh the state of ports on a DVS. pyvmomi: RefreshDVPortState(portKeys)",
        category="networking",
        parameters=[
            {
                "name": "dvs_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_DISTRIBUTED_SWITCH,
            },
        ],
        example="refresh_dvs_port_state(dvs_name='DSwitch-Prod')",
    ),
    OperationDefinition(
        operation_id="add_network_adapter",
        name="Add Network Adapter",
        description="Add a new network adapter to a VM.",
        category="networking",
        parameters=[
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VM,
            },
            {
                "name": "network_name",
                "type": "string",
                "required": True,
                "description": "Name of network to connect to",
            },
            {
                "name": "adapter_type",
                "type": "string",
                "required": False,
                "description": "Adapter type: vmxnet3, e1000e, e1000 (default: vmxnet3)",
            },
        ],
        example="add_network_adapter(vm_name='web-01', network_name='VM Network', adapter_type='vmxnet3')",
    ),
    OperationDefinition(
        operation_id="remove_network_adapter",
        name="Remove Network Adapter",
        description="Remove a network adapter from a VM.",
        category="networking",
        parameters=[
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VM,
            },
            {
                "name": "adapter_label",
                "type": "string",
                "required": True,
                "description": "Label of adapter to remove (e.g., 'Network adapter 2')",
            },
        ],
        example="remove_network_adapter(vm_name='web-01', adapter_label='Network adapter 2')",
    ),
    OperationDefinition(
        operation_id="change_network",
        name="Change VM Network",
        description="Change the network a VM adapter is connected to.",
        category="networking",
        parameters=[
            {
                "name": "vm_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_THE_VM,
            },
            {
                "name": "adapter_label",
                "type": "string",
                "required": True,
                "description": "Label of adapter (e.g., 'Network adapter 1')",
            },
            {
                "name": "network_name",
                "type": "string",
                "required": True,
                "description": "Name of new network",
            },
        ],
        example="change_network(vm_name='web-01', adapter_label='Network adapter 1', network_name='Production-VLAN')",
    ),
    OperationDefinition(
        operation_id="get_host_firewall_rules",
        name="Get Host Firewall Rules",
        description="Get firewall rules configured on ESXi host.",
        category="networking",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
        ],
        example="get_host_firewall_rules(host_name='esxi-01.example.com')",
        response_entity_type="FirewallRule",
        response_identifier_field="key",
        response_display_name_field="label",
    ),
    OperationDefinition(
        operation_id="enable_firewall_ruleset",
        name="Enable Firewall Ruleset",
        description="Enable a firewall ruleset on ESXi host.",
        category="networking",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
            {
                "name": "ruleset_key",
                "type": "string",
                "required": True,
                "description": "Ruleset key (e.g., 'sshServer', 'nfsClient')",
            },
        ],
        example="enable_firewall_ruleset(host_name='esxi-01.example.com', ruleset_key='sshServer')",
    ),
    OperationDefinition(
        operation_id="disable_firewall_ruleset",
        name="Disable Firewall Ruleset",
        description="Disable a firewall ruleset on ESXi host.",
        category="networking",
        parameters=[
            {
                "name": "host_name",
                "type": "string",
                "required": True,
                "description": NAME_OF_ESXI_HOST,
            },
            {
                "name": "ruleset_key",
                "type": "string",
                "required": True,
                "description": "Ruleset key (e.g., 'sshServer', 'nfsClient')",
            },
        ],
        example="disable_firewall_ruleset(host_name='esxi-01.example.com', ruleset_key='sshServer')",
    ),
]
