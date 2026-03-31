# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Type Definitions (TASK-97)

These help the agent understand what entities exist in VMware
and can be discovered via search_types.
"""

from meho_app.modules.connectors.base import TypeDefinition

VMWARE_TYPES = [
    TypeDefinition(
        type_name="VirtualMachine",
        description="A virtual machine that can be powered on/off, migrated, snapshotted. Core compute resource in vSphere.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "VM display name"},
            {
                "name": "power_state",
                "type": "string",
                "description": "Current power state: poweredOn, poweredOff, suspended",
            },
            {"name": "num_cpu", "type": "integer", "description": "Number of virtual CPUs"},
            {"name": "memory_mb", "type": "integer", "description": "Memory in megabytes"},
            {
                "name": "ip_address",
                "type": "string",
                "description": "Primary IP address from VMware Tools",
            },
            {
                "name": "guest_os",
                "type": "string",
                "description": "Guest operating system identifier",
            },
            {
                "name": "vm_tools_status",
                "type": "string",
                "description": "VMware Tools running status",
            },
        ],
    ),
    TypeDefinition(
        type_name="ClusterComputeResource",
        description="A cluster of ESXi hosts with DRS and HA for workload management and high availability.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "Cluster name"},
            {"name": "num_hosts", "type": "integer", "description": "Number of ESXi hosts"},
            {
                "name": "num_effective_hosts",
                "type": "integer",
                "description": "Hosts not in maintenance mode",
            },
            {
                "name": "total_cpu_mhz",
                "type": "integer",
                "description": "Total CPU capacity in MHz",
            },
            {
                "name": "total_memory_mb",
                "type": "integer",
                "description": "Total memory capacity in MB",
            },
            {
                "name": "drs_enabled",
                "type": "boolean",
                "description": "DRS (Distributed Resource Scheduler) enabled",
            },
            {
                "name": "ha_enabled",
                "type": "boolean",
                "description": "HA (High Availability) enabled",
            },
            {
                "name": "drs_behavior",
                "type": "string",
                "description": "DRS automation level: manual, partiallyAutomated, fullyAutomated",
            },
        ],
    ),
    TypeDefinition(
        type_name="HostSystem",
        description="An ESXi hypervisor host that runs virtual machines.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "Host FQDN or IP"},
            {
                "name": "connection_state",
                "type": "string",
                "description": "Connection state: connected, disconnected, notResponding",
            },
            {
                "name": "power_state",
                "type": "string",
                "description": "Power state: poweredOn, poweredOff, standBy",
            },
            {
                "name": "maintenance_mode",
                "type": "boolean",
                "description": "Whether host is in maintenance mode",
            },
            {"name": "cpu_model", "type": "string", "description": "CPU model string"},
            {"name": "num_cpu_cores", "type": "integer", "description": "Total physical CPU cores"},
            {"name": "memory_mb", "type": "integer", "description": "Total memory in MB"},
            {"name": "vendor", "type": "string", "description": "Hardware vendor"},
            {"name": "model", "type": "string", "description": "Hardware model"},
        ],
    ),
    TypeDefinition(
        type_name="Datastore",
        description="Storage for VM files (VMFS, NFS, vSAN). Contains VM disks, snapshots, ISOs.",
        category="storage",
        properties=[
            {"name": "name", "type": "string", "description": "Datastore name"},
            {
                "name": "type",
                "type": "string",
                "description": "Storage type: VMFS, NFS, vSAN, VVOL",
            },
            {"name": "capacity_gb", "type": "integer", "description": "Total capacity in GB"},
            {"name": "free_space_gb", "type": "integer", "description": "Free space in GB"},
            {
                "name": "accessible",
                "type": "boolean",
                "description": "Whether datastore is accessible",
            },
            {"name": "maintenance_mode", "type": "string", "description": "Maintenance mode state"},
        ],
    ),
    TypeDefinition(
        type_name="Network",
        description="Virtual network for VM connectivity. Can be standard vSwitch or distributed.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Network name"},
            {
                "name": "type",
                "type": "string",
                "description": "Network type: Network, DistributedVirtualPortgroup",
            },
            {"name": "vlan_id", "type": "integer", "description": "VLAN ID if applicable"},
        ],
    ),
    TypeDefinition(
        type_name="Datacenter",
        description="Logical container for hosts, clusters, networks, and datastores.",
        category="inventory",
        properties=[
            {"name": "name", "type": "string", "description": "Datacenter name"},
        ],
    ),
    TypeDefinition(
        type_name="ResourcePool",
        description="Container for allocating CPU and memory resources to VMs.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "Resource pool name"},
            {"name": "cpu_reservation", "type": "integer", "description": "Reserved CPU in MHz"},
            {
                "name": "memory_reservation_mb",
                "type": "integer",
                "description": "Reserved memory in MB",
            },
        ],
    ),
    TypeDefinition(
        type_name="Folder",
        description="Container for organizing VMs and other inventory objects.",
        category="inventory",
        properties=[
            {"name": "name", "type": "string", "description": "Folder name"},
            {"name": "child_count", "type": "integer", "description": "Number of child objects"},
        ],
    ),
    TypeDefinition(
        type_name="DrsRecommendation",
        description="A DRS recommendation for migrating VMs to balance cluster load.",
        category="compute",
        properties=[
            {
                "name": "key",
                "type": "string",
                "description": "Unique recommendation key for applying",
            },
            {
                "name": "rating",
                "type": "integer",
                "description": "Recommendation priority (1-5, 1 is highest)",
            },
            {"name": "reason", "type": "string", "description": "Reason code for recommendation"},
            {"name": "reason_text", "type": "string", "description": "Human-readable reason"},
            {"name": "target", "type": "string", "description": "Target VM for migration"},
        ],
    ),
    TypeDefinition(
        type_name="Task",
        description="An asynchronous operation in vCenter.",
        category="system",
        properties=[
            {"name": "key", "type": "string", "description": "Task ID"},
            {"name": "name", "type": "string", "description": "Task operation name"},
            {
                "name": "state",
                "type": "string",
                "description": "Task state: queued, running, success, error",
            },
            {"name": "progress", "type": "integer", "description": "Progress percentage"},
            {"name": "start_time", "type": "string", "description": "Task start time"},
        ],
    ),
    TypeDefinition(
        type_name="Alarm",
        description="A triggered alarm indicating an issue or threshold breach.",
        category="monitoring",
        properties=[
            {"name": "entity", "type": "string", "description": "Entity the alarm is on"},
            {"name": "alarm_name", "type": "string", "description": "Name of the alarm definition"},
            {"name": "status", "type": "string", "description": "Alarm status: green, yellow, red"},
            {"name": "time", "type": "string", "description": "When alarm was triggered"},
        ],
    ),
    # TASK-99: Additional types
    TypeDefinition(
        type_name="Snapshot",
        description="A point-in-time capture of VM state. Can be reverted to restore VM to that state.",
        category="compute",
        properties=[
            {"name": "name", "type": "string", "description": "Snapshot name"},
            {"name": "description", "type": "string", "description": "Snapshot description"},
            {"name": "created", "type": "string", "description": "Creation timestamp"},
            {
                "name": "state",
                "type": "string",
                "description": "VM power state when snapshot was taken",
            },
            {
                "name": "quiesced",
                "type": "boolean",
                "description": "Whether guest filesystem was quiesced",
            },
            {
                "name": "parent",
                "type": "string",
                "description": "Parent snapshot name (for tree hierarchy)",
            },
        ],
    ),
    TypeDefinition(
        type_name="VirtualDisk",
        description="A virtual disk (VMDK) attached to a VM.",
        category="storage",
        properties=[
            {"name": "label", "type": "string", "description": "Disk label (e.g., 'Hard disk 1')"},
            {"name": "capacity_gb", "type": "integer", "description": "Disk capacity in GB"},
            {
                "name": "datastore",
                "type": "string",
                "description": "Datastore name where disk is stored",
            },
            {"name": "file_name", "type": "string", "description": "Full path to VMDK file"},
            {
                "name": "thin_provisioned",
                "type": "boolean",
                "description": "Whether disk is thin provisioned",
            },
        ],
    ),
    TypeDefinition(
        type_name="VirtualNetworkAdapter",
        description="A virtual network adapter (NIC) attached to a VM.",
        category="networking",
        properties=[
            {
                "name": "label",
                "type": "string",
                "description": "NIC label (e.g., 'Network adapter 1')",
            },
            {"name": "mac_address", "type": "string", "description": "MAC address"},
            {"name": "network", "type": "string", "description": "Connected network name"},
            {"name": "connected", "type": "boolean", "description": "Whether NIC is connected"},
            {
                "name": "type",
                "type": "string",
                "description": "NIC type: VirtualVmxnet3, VirtualE1000, etc.",
            },
        ],
    ),
    TypeDefinition(
        type_name="DistributedVirtualSwitch",
        description="A vSphere Distributed Switch (VDS) for centralized network management across hosts.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Distributed switch name"},
            {"name": "uuid", "type": "string", "description": "Unique identifier"},
            {"name": "num_ports", "type": "integer", "description": "Number of ports"},
            {"name": "num_hosts", "type": "integer", "description": "Number of connected hosts"},
        ],
    ),
    TypeDefinition(
        type_name="DistributedPortGroup",
        description="A port group on a distributed switch with VLAN and security settings.",
        category="networking",
        properties=[
            {"name": "name", "type": "string", "description": "Port group name"},
            {"name": "vlan_id", "type": "integer", "description": "VLAN ID"},
            {"name": "dvs_name", "type": "string", "description": "Parent distributed switch"},
            {"name": "num_ports", "type": "integer", "description": "Number of ports in group"},
        ],
    ),
    TypeDefinition(
        type_name="Event",
        description="An event from vCenter event log.",
        category="monitoring",
        properties=[
            {"name": "key", "type": "integer", "description": "Event key"},
            {"name": "type", "type": "string", "description": "Event type class"},
            {"name": "created_time", "type": "string", "description": "When event occurred"},
            {"name": "message", "type": "string", "description": "Event message"},
            {"name": "user", "type": "string", "description": "User who triggered event"},
        ],
    ),
    # Phase 96: NSX Entity Types
    TypeDefinition(
        type_name="NsxSegment",
        description="An NSX logical segment (virtual network). Connects VMs across hosts via overlay or VLAN transport.",
        category="networking",
        properties=[
            {"name": "display_name", "type": "string", "description": "Segment display name"},
            {"name": "type", "type": "string", "description": "Segment type: ROUTED, CONNECTED, DISCONNECTED"},
            {"name": "transport_zone_path", "type": "string", "description": "Transport zone this segment belongs to"},
            {"name": "admin_state", "type": "string", "description": "Administrative state: UP, DOWN"},
            {"name": "vlan_ids", "type": "array", "description": "VLAN IDs for VLAN-backed segments"},
        ],
    ),
    TypeDefinition(
        type_name="NsxFirewallPolicy",
        description="An NSX distributed firewall security policy containing rules that filter traffic between VMs.",
        category="networking",
        properties=[
            {"name": "display_name", "type": "string", "description": "Policy display name"},
            {"name": "category", "type": "string", "description": "Policy category: Emergency, Infrastructure, Environment, Application"},
            {"name": "rule_count", "type": "integer", "description": "Number of rules in this policy"},
        ],
    ),
    TypeDefinition(
        type_name="NsxTransportNode",
        description="An NSX transport node (ESXi host or Edge node) participating in the NSX overlay network.",
        category="networking",
        properties=[
            {"name": "display_name", "type": "string", "description": "Transport node display name"},
            {"name": "node_type", "type": "string", "description": "Node type: HostNode, EdgeNode"},
            {"name": "maintenance_mode", "type": "string", "description": "Maintenance mode state"},
            {"name": "status", "type": "string", "description": "Transport node status"},
        ],
    ),
    TypeDefinition(
        type_name="NsxGateway",
        description="An NSX Tier-0 or Tier-1 gateway providing routing between segments and external networks.",
        category="networking",
        properties=[
            {"name": "display_name", "type": "string", "description": "Gateway display name"},
            {"name": "tier", "type": "string", "description": "Gateway tier: Tier-0 or Tier-1"},
            {"name": "ha_mode", "type": "string", "description": "HA mode: ACTIVE_ACTIVE, ACTIVE_STANDBY"},
        ],
    ),
    # Phase 96: SDDC Entity Types
    TypeDefinition(
        type_name="SddcWorkloadDomain",
        description="A VCF workload domain -- a logical container of clusters, hosts, and vCenters managed by SDDC Manager.",
        category="inventory",
        properties=[
            {"name": "name", "type": "string", "description": "Domain name"},
            {"name": "type", "type": "string", "description": "Domain type: MANAGEMENT, VI (workload)"},
            {"name": "status", "type": "string", "description": "Domain status"},
            {"name": "cluster_count", "type": "integer", "description": "Number of clusters in domain"},
        ],
    ),
    TypeDefinition(
        type_name="SddcHost",
        description="A physical host managed by SDDC Manager, with commissioning status and domain assignment.",
        category="compute",
        properties=[
            {"name": "fqdn", "type": "string", "description": "Host fully qualified domain name"},
            {"name": "status", "type": "string", "description": "Host status: ASSIGNED, UNASSIGNED_USEABLE"},
            {"name": "domain", "type": "string", "description": "Assigned workload domain name"},
        ],
    ),
    TypeDefinition(
        type_name="SddcCertificate",
        description="A certificate managed by SDDC Manager for VCF component TLS.",
        category="system",
        properties=[
            {"name": "subject", "type": "string", "description": "Certificate subject"},
            {"name": "valid_to", "type": "string", "description": "Certificate expiration date"},
            {"name": "is_installed", "type": "boolean", "description": "Whether certificate is currently installed"},
        ],
    ),
    # Phase 96: vSAN Entity Type
    TypeDefinition(
        type_name="VsanCluster",
        description="A vSAN-enabled cluster providing distributed, software-defined storage across ESXi hosts.",
        category="storage",
        properties=[
            {"name": "cluster_name", "type": "string", "description": "Parent cluster name"},
            {"name": "vsan_enabled", "type": "boolean", "description": "Whether vSAN is enabled"},
            {"name": "capacity_gb", "type": "number", "description": "Total vSAN capacity in GB"},
            {"name": "free_space_gb", "type": "number", "description": "Available vSAN space in GB"},
            {"name": "disk_group_count", "type": "integer", "description": "Number of disk groups across all hosts"},
        ],
    ),
]
