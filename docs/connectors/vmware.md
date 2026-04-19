# VMware vSphere

> Last verified: v2.3

MEHO's VMware connector provides deep vSphere and VMware Cloud Foundation (VCF) integration using `pyvmomi`, NSX Manager REST API, and SDDC Manager REST API. With 209 operations across 10 categories -- compute, storage, networking, monitoring, inventory, system, vSAN, NSX, SDDC, and capacity planning -- MEHO delivers comprehensive visibility and control over your entire VMware environment, from datacenter-level overview down to individual VM disk I/O metrics, NSX micro-segmentation, and VCF lifecycle management.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| vCenter (Username/Password) | `vcenter_host`, `port`, `username`, `password` | vSphere API session authentication (pyvmomi) |
| NSX Manager (Basic Auth) | `nsx_host`, `nsx_username`, `nsx_password` | Optional. Separate REST credentials for NSX operations |
| SDDC Manager (Bearer Token) | `sddc_host`, `sddc_username`, `sddc_password` | Optional. Separate REST credentials for SDDC operations |

### Setup

1. **Create a dedicated vSphere user** with the appropriate permissions:
    - **Read-only:** Assign the `Read-only` global permission for diagnostic use
    - **Operator:** Assign `Virtual Machine` > `Interaction` permissions for VM lifecycle operations

2. **Get the vCenter hostname** (FQDN or IP address of your vCenter Server).

3. **Add the connector in MEHO** using the vCenter host, port (default 443), and credentials.

!!! tip "Service Account Best Practice"
    Create a dedicated service account (e.g., `meho-svc@vsphere.local`) rather than using personal admin credentials. This provides audit trail clarity and permission isolation.

!!! warning "SSL Certificates"
    vCenter often uses self-signed certificates. Enable `disable_ssl_verification` for initial testing, then configure proper certificates for production.

!!! tip "VMware Cloud Foundation (VCF)"
    For VCF environments, MEHO supports three separate API endpoints with independent credentials:

    - **vCenter** -- Core vSphere operations (compute, storage, networking, monitoring, inventory, system) + vSAN + capacity planning. Uses pyvmomi with `vcenter_host` credentials.
    - **NSX Manager** -- Network overlay operations (segments, firewall, gateways, load balancers, transport zones). Uses REST API with separate `nsx_host` credentials.
    - **SDDC Manager** -- Infrastructure lifecycle operations (workload domains, hosts, clusters, updates, certificates). Uses REST API with separate `sddc_host` credentials.

    Not all three are required -- add only what your environment has. vCenter alone provides 179 operations. NSX adds 12 and SDDC adds 8.

## Operations

MEHO provides 209 VMware operations organized into 10 categories. The tables below show all operations with their trust classification.

### Compute (91 operations)

VM lifecycle, host management, cluster operations, snapshots, resource pools, and DRS.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_virtual_machines` | READ | List all VMs with power state, CPU/memory, guest info, and resource usage |
| `get_virtual_machine` | READ | Comprehensive VM details including runtime, config, guest info, and stats |
| `power_on_vm` | WRITE | Power on a virtual machine |
| `power_off_vm` | WRITE | Power off a VM (hard shutdown) |
| `shutdown_guest` | WRITE | Graceful OS shutdown via VMware Tools |
| `reboot_guest` | WRITE | Graceful OS reboot via VMware Tools |
| `suspend_vm` | WRITE | Suspend VM state to memory |
| `reset_vm` | WRITE | Hard reset a VM (like pressing the reset button) |
| `destroy_vm` | DESTRUCTIVE | Permanently delete a VM and its files |
| `rename_vm` | WRITE | Rename a virtual machine |
| `upgrade_vm_hardware` | WRITE | Upgrade VM hardware version |
| `reconfigure_vm` | WRITE | Change VM CPU, memory, or other settings |
| `clone_vm` | WRITE | Clone a VM (full or linked) |
| `create_vm_from_template` | WRITE | Deploy a new VM from a template |
| `convert_to_template` | WRITE | Convert a VM to a template |
| `convert_from_template` | WRITE | Convert a template back to a VM |
| `list_snapshots` | READ | List all snapshots for a VM |
| `create_snapshot` | WRITE | Create a VM snapshot |
| `revert_to_snapshot` | WRITE | Revert VM to a snapshot |
| `delete_snapshot` | DESTRUCTIVE | Delete a VM snapshot |
| `delete_all_snapshots` | DESTRUCTIVE | Remove all snapshots from a VM |
| `list_hosts` | READ | List all ESXi hosts with status, CPU, memory, and maintenance state |
| `get_host` | READ | Detailed host information including hardware model, CPU, and connection state |
| `enter_maintenance_mode` | WRITE | Put an ESXi host into maintenance mode |
| `exit_maintenance_mode` | WRITE | Take an ESXi host out of maintenance mode |
| `reboot_host` | WRITE | Reboot an ESXi host |
| `shutdown_host` | WRITE | Shut down an ESXi host |
| `disconnect_host` | WRITE | Disconnect a host from vCenter |
| `reconnect_host` | WRITE | Reconnect a host to vCenter |
| `list_clusters` | READ | List all clusters with host count, DRS/HA status, and capacity |
| `get_cluster` | READ | Cluster details including DRS behavior and HA configuration |
| `list_resource_pools` | READ | List all resource pools in a cluster |
| `get_resource_pool` | READ | Resource pool details including reservations and limits |
| `create_resource_pool` | WRITE | Create a new resource pool |
| `destroy_resource_pool` | DESTRUCTIVE | Delete a resource pool |
| `move_vm_to_resource_pool` | WRITE | Move a VM to a different resource pool |
| `list_drs_recommendations` | READ | Get DRS migration recommendations |
| `apply_drs_recommendation` | WRITE | Apply a DRS recommendation |
| `migrate_vm` | WRITE | vMotion a VM to another host |

*Plus additional VM configuration, guest operations, and advanced cluster operations.*

### Storage (28 operations)

Datastore management, disk operations, Storage DRS, and storage vMotion.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_datastores` | READ | List all datastores with capacity and free space |
| `get_datastore` | READ | Detailed datastore info including connected hosts |
| `get_vm_disks` | READ | List all virtual disks attached to a VM with size and datastore |
| `browse_datastore` | READ | Browse files and folders in a datastore |
| `get_datastore_vms` | READ | List all VMs stored on a specific datastore |
| `get_host_datastores` | READ | List datastores accessible from a specific host |
| `relocate_vm` | WRITE | Storage vMotion -- relocate VM to different datastore |
| `add_disk` | WRITE | Add a new virtual disk to a VM |
| `extend_disk` | WRITE | Extend an existing virtual disk |
| `attach_disk` | WRITE | Attach an existing VMDK to a VM |
| `detach_disk` | WRITE | Detach a virtual disk without deleting the VMDK |
| `consolidate_disks` | WRITE | Consolidate VM disk files after snapshot operations |
| `create_vmfs_datastore` | WRITE | Create a new VMFS datastore on a host |
| `create_nfs_datastore` | WRITE | Create a new NFS datastore on a host |
| `expand_vmfs_datastore` | WRITE | Expand a VMFS datastore |
| `destroy_datastore` | DESTRUCTIVE | Remove a datastore from vCenter |
| `remove_datastore` | DESTRUCTIVE | Unmount a datastore from a specific host |
| `enter_datastore_maintenance_mode` | WRITE | Put datastore into maintenance mode |
| `exit_datastore_maintenance_mode` | WRITE | Take datastore out of maintenance mode |
| `get_storage_pods` | READ | List Storage DRS pods (datastore clusters) |
| `get_storage_pod` | READ | Storage DRS pod details including member datastores |
| `scan_host_storage` | WRITE | Rescan storage adapters to discover new LUNs |

*Plus additional disk management, refresh, and CBT (Changed Block Tracking) operations.*

### Networking (17 operations)

Networks, distributed switches, port groups, NICs, and firewall rules.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_networks` | READ | List all networks in vCenter |
| `get_vm_nics` | READ | List all network adapters on a VM with MAC and network info |
| `get_host_networks` | READ | List all networks available on a specific host |
| `list_distributed_switches` | READ | List all vSphere Distributed Switches |
| `get_distributed_switch` | READ | Detailed DVS configuration |
| `list_port_groups` | READ | List all port groups (standard and distributed) |
| `get_port_group` | READ | Port group details including VLAN configuration |
| `create_dvs_portgroup` | WRITE | Create a new distributed port group |
| `destroy_dvs_portgroup` | DESTRUCTIVE | Delete a distributed port group |
| `query_used_vlans` | READ | Get VLAN IDs in use on a DVS |
| `add_network_adapter` | WRITE | Add a NIC to a VM |
| `remove_network_adapter` | WRITE | Remove a NIC from a VM |
| `change_network` | WRITE | Change which network a VM adapter connects to |
| `get_host_firewall_rules` | READ | Get ESXi host firewall rules |
| `enable_firewall_ruleset` | WRITE | Enable a firewall ruleset on a host |
| `disable_firewall_ruleset` | WRITE | Disable a firewall ruleset on a host |

### Monitoring (18 operations)

Performance metrics (quickStats and PerformanceManager API), alarms, and events.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_alarms` | READ | Get currently triggered alarms in vCenter |
| `get_vm_performance` | READ | VM performance from quickStats (CPU MHz, memory MB, uptime) |
| `get_host_performance` | READ | Host performance from quickStats (CPU, memory, fairness) |
| `get_cluster_performance` | READ | Aggregated performance metrics for a cluster |
| `get_detailed_vm_performance` | READ | **Comprehensive VM metrics via PerformanceManager** -- disk I/O, network throughput, CPU ready time, memory swap. Supports realtime to 7-day intervals. |
| `get_detailed_host_performance` | READ | **Comprehensive host metrics via PerformanceManager** -- per-NIC throughput, storage latency, CPU per-core |
| `get_cluster_detailed_performance` | READ | Aggregated PerformanceManager metrics across all hosts in a cluster |
| `get_datastore_performance` | READ | Datastore capacity and status (type, free space, maintenance mode) |
| `list_available_metrics` | READ | Discover available performance counters for an entity |
| `get_events` | READ | Recent events from the vCenter event log |
| `acknowledge_alarm` | WRITE | Acknowledge a triggered alarm |
| `retrieve_hardware_uptime` | READ | ESXi host hardware uptime in seconds |
| `get_cluster_resource_usage` | READ | Current resource usage summary for a cluster |
| `get_cluster_ha_status` | READ | High Availability runtime information |
| `query_memory_overhead` | READ | Query memory overhead for a VM on a host |

### Inventory (15 operations)

Datacenters, folders, templates, content libraries, tags, and search.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_datacenters` | READ | List all datacenters in vCenter |
| `list_folders` | READ | List all VM folders |
| `create_folder` | WRITE | Create a new inventory folder |
| `rename_folder` | WRITE | Rename an inventory folder |
| `destroy_folder` | DESTRUCTIVE | Delete an empty folder |
| `move_into_folder` | WRITE | Move a VM or folder into another folder |
| `register_vm` | WRITE | Register an existing VM (VMX file) into vCenter |
| `list_content_libraries` | READ | List all content libraries |
| `get_content_library_items` | READ | List items in a content library |
| `list_tags` | READ | List all tags defined in vCenter |
| `list_tag_categories` | READ | List all tag categories |
| `list_templates` | READ | List all VM templates |
| `get_template` | READ | Template details |
| `search_inventory` | READ | Search vCenter inventory by name pattern |
| `get_inventory_path` | READ | Get the full inventory path of an entity |

### System (10 operations)

vCenter info, licensing, tasks, and host services.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_vcenter_info` | READ | vCenter Server version and build information |
| `list_tasks` | READ | Recent tasks from the vCenter task manager |
| `query_host_connection_info` | READ | Connection information for a host |
| `get_host_services` | READ | List services on an ESXi host with status |
| `start_host_service` | WRITE | Start a service on an ESXi host (e.g., SSH, NTP) |
| `stop_host_service` | WRITE | Stop a service on an ESXi host |
| `restart_host_service` | WRITE | Restart a service on an ESXi host |
| `refresh_host_services` | WRITE | Refresh the service list on a host |
| `get_license_info` | READ | vCenter license information and usage |
| `get_licensed_features` | READ | List of licensed features in vCenter |

### vSAN (6 operations)

vSAN health diagnostics, disk groups, capacity, resync status, storage policies, and object health. Uses existing vCenter credentials via pyvmomi.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_vsan_cluster_health` | READ | Check vSAN configuration and health status for a cluster |
| `get_vsan_disk_groups` | READ | List vSAN disk groups per host showing cache SSDs and capacity disks |
| `get_vsan_capacity` | READ | Get vSAN datastore capacity, free space, and provisioned storage |
| `get_vsan_resync_status` | READ | Check if vSAN objects are resyncing after host/disk failures |
| `get_vsan_storage_policies` | READ | Get vSAN default storage policy including failures to tolerate and stripe width |
| `get_vsan_objects_health` | READ | Check vSAN object health and disk status per host |

### NSX Manager (12 operations)

NSX logical networking -- segments, distributed firewall, security groups, gateways, load balancers, transport zones/nodes, and search. All operations are READ-only.

!!! note "Separate Credentials Required"
    NSX operations require separate REST credentials (`nsx_host`, `nsx_username`, `nsx_password`) configured on the connector. These are different from vCenter credentials. NSX operations use the Policy API v1 with Management API fallback for transport node details.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_nsx_segments` | READ | List NSX logical segments with type, VLAN IDs, subnets, and transport zone |
| `get_nsx_segment` | READ | Get detailed segment info including segment ports |
| `list_nsx_firewall_policies` | READ | List distributed firewall security policies and rules |
| `get_nsx_firewall_rule` | READ | Get firewall rule details including direction, IP protocol, and scope |
| `list_nsx_security_groups` | READ | List security groups with membership criteria |
| `list_nsx_tier0_gateways` | READ | List Tier-0 gateways for north-south routing analysis |
| `list_nsx_tier1_gateways` | READ | List Tier-1 gateways for east-west routing and micro-segmentation |
| `list_nsx_load_balancers` | READ | List load balancer services for traffic distribution analysis |
| `list_nsx_transport_zones` | READ | List transport zones (OVERLAY/VLAN) with host switch binding |
| `list_nsx_transport_nodes` | READ | List transport nodes to verify host preparation and tunnel status |
| `get_nsx_transport_node` | READ | Get transport node details including host switch spec and IPs |
| `search_nsx` | READ | Search NSX objects by query string with optional resource type filter |

### SDDC Manager (8 operations)

VMware Cloud Foundation (VCF) lifecycle management -- workload domains, hosts, clusters, update compliance, and certificate health. All operations are READ-only.

!!! note "Separate Credentials Required"
    SDDC operations require separate REST credentials (`sddc_host`, `sddc_username`, `sddc_password`) configured on the connector. These are different from vCenter credentials. SDDC Manager uses Bearer Token authentication.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_sddc_system_info` | READ | Get SDDC Manager version, build, hostname, and FQDN |
| `list_sddc_workload_domains` | READ | List VCF workload domains with type, status, and cluster/host counts |
| `get_sddc_workload_domain` | READ | Get workload domain details including NSX cluster, vCenter, and network pools |
| `list_sddc_hosts` | READ | List VCF-managed hosts with assignment status and hardware info |
| `list_sddc_clusters` | READ | List VCF clusters with host counts and datastore configuration |
| `get_sddc_update_history` | READ | Get VCF upgrade/update history with status and release versions |
| `get_sddc_certificates` | READ | Check certificate health including subject, issuer, and validity period |
| `get_sddc_prechecks` | READ | Get compliance/precheck results for upgrade readiness |

### Capacity Planning (4 operations)

Cluster capacity summary, overcommitment ratios, datastore utilization, and host load distribution. Uses existing vCenter credentials via pyvmomi.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_cluster_capacity` | READ | Get cluster-level capacity summary (total/effective CPU and memory, host/VM count) |
| `get_cluster_overcommitment` | READ | Calculate vCPU-to-pCPU and memory overcommitment ratios (healthy: vCPU:pCPU < 4:1) |
| `get_datastore_utilization` | READ | Get capacity, free space, and utilization percentage for all datastores |
| `get_host_load_distribution` | READ | Get per-host CPU and memory utilization to identify load imbalance |

## Example Queries

Ask MEHO questions like:

- "List all VMs with snapshots older than 30 days"
- "Show CPU and memory utilization for the Production cluster"
- "What datastores are running low on space -- under 20% free?"
- "Which VMs are powered off and using more than 100GB of storage?"
- "Get the disk I/O latency for web-server-01 over the past hour"
- "Are there any triggered alarms in vCenter right now?"
- "Show me the DRS recommendations for the Production cluster"
- "Which ESXi hosts are in maintenance mode?"
- "What's the network throughput for db-server-02?"
- "Find all VMs with the name pattern 'test-*' and show their resource usage"
- "Is vSAN healthy on the Production cluster? Any resyncing objects?"
- "What's the vCPU-to-pCPU overcommitment ratio on the Production cluster?"
- "Show me all NSX firewall policies and their rules -- is anything blocking traffic?"
- "List the VCF workload domains and their update compliance status"
- "Which hosts have the highest CPU utilization -- is the load balanced across the cluster?"
- "Are there any expiring certificates in the SDDC Manager?"
- "What NSX segments exist and which transport zone are they in?"

## Topology

VMware vSphere discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| Datacenter | name | Top-level container for all vSphere objects |
| ClusterComputeResource | name, num_hosts, total_cpu_mhz, total_memory_mb, drs_enabled, ha_enabled | Contains Hosts, manages DRS and HA |
| HostSystem | name, connection_state, power_state, maintenance_mode, cpu_model, memory_mb | **Host -> Cluster** (member of), hosts VMs |
| VirtualMachine | name, power_state, num_cpu, memory_mb, ip_address, guest_os, vm_tools_status | **VM -> K8s Node** via providerID matching (hostname/IP), VM -> Host (running on), VM -> Datastore (storage) |
| Datastore | name, type, capacity_gb, free_space_gb, accessible | Connected to Hosts, stores VM files |
| Network | name, type, vlan_id | Connected to VMs via NICs |
| ResourcePool | name, cpu_reservation, memory_reservation_mb | Contains VMs, nested within Clusters |
| Folder | name, child_count | Organizes VMs and other inventory objects |
| Snapshot | name, description, created, state, quiesced | Point-in-time capture of VM state |
| DistributedVirtualSwitch | name, uuid, num_ports, num_hosts | Centralized network management across hosts |
| DistributedPortGroup | name, vlan_id, dvs_name, num_ports | Port group on a distributed switch |

### Cross-System Links

The key cross-system link is **VM -> Kubernetes Node**. When Kubernetes runs on vSphere (e.g., Tanzu, Rancher, or manual kubeadm), MEHO matches Kubernetes node `providerID` and hostnames back to VMware VMs. This enables tracing from a failing Kubernetes pod to the underlying vSphere VM and its ESXi host -- revealing whether the root cause is at the application, OS, or infrastructure layer.

## Troubleshooting

### vSphere API Version Compatibility

**Symptom:** Some operations fail with "method not found" or similar errors
**Cause:** Older vCenter versions may not support all operations (e.g., PerformanceManager detailed metrics require vSphere 6.5+)
**Fix:** Check your vCenter version with `get_vcenter_info`. Consider upgrading if running vSphere 6.0 or earlier.

### SSL Certificate Issues

**Symptom:** Connection fails with certificate verification errors
**Cause:** vCenter uses a self-signed certificate or an untrusted CA
**Fix:** Enable `disable_ssl_verification` for testing, or install the vCenter CA certificate on the MEHO server. You can download the CA cert from `https://<vcenter>/certs/download.zip`.

### Performance Statistics Level

**Symptom:** `get_detailed_vm_performance` returns incomplete or empty metrics
**Cause:** vCenter statistics collection level is too low (Level 1 only collects basic counters)
**Fix:** Increase the statistics collection level in vCenter: **Administration > vCenter Server Settings > Statistics > Statistics Intervals**. Level 2 or higher provides disk and network counters.

### Permission Denied on Write Operations

**Symptom:** Read operations work but write operations (power on, snapshot, etc.) return permission errors
**Cause:** The service account has read-only permissions
**Fix:** Assign appropriate privileges to the MEHO service account. For VM lifecycle operations, add `Virtual Machine > Interaction` privileges. For storage operations, add `Datastore` privileges.

### VMware Tools Not Installed

**Symptom:** `shutdown_guest` and `reboot_guest` fail; VM shows "Tools not installed"
**Cause:** VMware Tools (or open-vm-tools) is not installed in the guest OS
**Fix:** Install VMware Tools in the guest. Without Tools, use `power_off_vm` and `reset_vm` instead (these are hard operations that don't require guest cooperation).
