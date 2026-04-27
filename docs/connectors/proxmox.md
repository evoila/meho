# Proxmox VE

> Last verified: v2.0

MEHO's Proxmox connector provides native access to your Proxmox VE clusters using `proxmoxer`, the official Python SDK. With operations covering QEMU virtual machines, LXC containers, nodes, storage, and cluster management, MEHO gives you full visibility into your Proxmox infrastructure -- from cluster-wide resource overview down to individual container status.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| API Token (recommended) | `host`, `port`, `api_token_id`, `api_token_secret` | Token-based authentication with granular permissions |
| Username/Password | `host`, `port`, `username`, `password` | Session-based authentication (PAM or PVE realm) |

### Setup with API Token (Recommended)

1. **Create an API token** in the Proxmox web UI:
    - Navigate to **Datacenter > Permissions > API Tokens**
    - Select a user (e.g., `meho@pve`)
    - Create a new token, noting the **Token ID** and **Secret**
    - Uncheck "Privilege Separation" if you want the token to inherit the user's permissions

2. **Assign permissions** to the user:
    - For read-only access: Assign `PVEAuditor` role at the datacenter level
    - For operator access: Assign `PVEVMAdmin` role for VM/container lifecycle operations

3. **Add the connector in MEHO** using the Proxmox host (FQDN or IP), port (default 8006), token ID, and token secret.

!!! tip "Token ID Format"
    The token ID format is `user@realm!tokenname` (e.g., `meho@pve!monitoring`). The secret is only shown once during creation -- save it securely.

!!! warning "Self-Signed Certificates"
    Proxmox uses self-signed certificates by default. Enable `disable_ssl_verification` for testing, or install the Proxmox CA certificate on the MEHO server.

### Setup with Username/Password

Use the Proxmox host, port, and credentials for a PVE or PAM user. This creates a session-based connection that authenticates on each request.

## Operations

### Compute -- VMs (18 operations)

QEMU/KVM virtual machine lifecycle, configuration, snapshots, and migration.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_vms` | READ | List all QEMU VMs across all nodes with name, status, CPU/memory, disk size, and uptime |
| `get_vm` | READ | Detailed VM information including configuration and resource usage |
| `get_vm_status` | READ | Current status and resource metrics for a VM |
| `get_vm_config` | READ | Full VM configuration including CPU, memory, disks, and network |
| `start_vm` | WRITE | Power on a virtual machine |
| `stop_vm` | WRITE | Hard power off a virtual machine |
| `shutdown_vm` | WRITE | Graceful ACPI shutdown (requires QEMU Guest Agent for best results) |
| `restart_vm` | WRITE | Reboot a virtual machine |
| `reset_vm` | WRITE | Hard reset (like pressing the reset button) |
| `suspend_vm` | WRITE | Suspend VM to disk (hibernate) |
| `resume_vm` | WRITE | Resume a suspended VM |
| `clone_vm` | WRITE | Create a full or linked clone of a VM |
| `migrate_vm` | WRITE | Live migrate a VM to another node |
| `delete_vm` | DESTRUCTIVE | Delete a virtual machine |
| `list_vm_snapshots` | READ | List all snapshots for a VM |
| `create_vm_snapshot` | WRITE | Create a VM snapshot (optionally including RAM state) |
| `delete_vm_snapshot` | DESTRUCTIVE | Delete a VM snapshot |
| `rollback_vm_snapshot` | WRITE | Rollback a VM to a previous snapshot |

### Compute -- Containers (14 operations)

LXC container lifecycle, configuration, snapshots, and migration.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_containers` | READ | List all LXC containers across all nodes with status and resource usage |
| `get_container` | READ | Detailed container information including configuration and resources |
| `get_container_status` | READ | Current status and resource metrics for a container |
| `get_container_config` | READ | Full container configuration |
| `start_container` | WRITE | Start an LXC container |
| `stop_container` | WRITE | Immediately stop a container |
| `shutdown_container` | WRITE | Gracefully shutdown a container |
| `restart_container` | WRITE | Restart a container |
| `clone_container` | WRITE | Create a full or linked clone |
| `migrate_container` | WRITE | Migrate a container to another node |
| `delete_container` | DESTRUCTIVE | Delete an LXC container |
| `list_container_snapshots` | READ | List all container snapshots |
| `create_container_snapshot` | WRITE | Create a container snapshot |
| `delete_container_snapshot` | DESTRUCTIVE | Delete a container snapshot |

### Nodes (6 operations)

Node (host) management and cluster status.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_nodes` | READ | List all nodes in the cluster with status, CPU/memory/disk usage, and uptime |
| `get_node` | READ | Detailed node information including hardware and resource usage |
| `get_node_status` | READ | Node status with kernel version, PVE version, and boot info |
| `get_node_resources` | READ | CPU, memory, and disk resource usage breakdown |
| `get_cluster_status` | READ | Overall cluster status including quorum state and node membership |
| `get_cluster_resources` | READ | All cluster resources (VMs, containers, storage, nodes) with status |

### Storage (4 operations)

Storage pool management and content browsing.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_storage` | READ | List all storage pools with type, capacity, usage, and content types |
| `get_storage` | READ | Detailed storage pool information including capacity and configuration |
| `get_storage_content` | READ | Browse storage contents (ISO images, VM disks, templates, backups) |
| `get_storage_status` | READ | Storage health and status information |

## Example Queries

Ask MEHO questions like:

- "Show me all VMs running on node pve-01"
- "What's the storage utilization across all Proxmox nodes?"
- "List all containers that are stopped"
- "Which VMs have snapshots and how old are they?"
- "What's the CPU and memory usage for the Proxmox cluster?"
- "Migrate VM 105 from pve1 to pve2"
- "Show me the cluster quorum status -- are all nodes online?"
- "Which storage pools are more than 80% full?"
- "Create a snapshot of VM 200 named 'before-upgrade'"
- "List all VMs across the cluster sorted by memory usage"

## Topology

Proxmox VE discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| Node | name, status, cpu_usage, memory_used/total, disk_used/total, uptime, pve_version | Physical host running VMs and containers |
| VM | vmid, name, node, status, cpu_count, memory_mb, disk_size_gb, tags | Runs on a Node, stores data on Storage |
| Container | vmid, name, node, status, cpu_count, memory_mb, swap_mb, unprivileged | Runs on a Node (shared kernel), stores data on Storage |
| Storage | storage, type, content, total_gb, used_gb, shared, active | Accessible from Nodes, stores VM/container disks and backups |
| Snapshot | name, description, snaptime, parent, vmstate | Point-in-time capture of VM or container state |
| Cluster | cluster_name, quorate, node_count, online_count | Group of Nodes with shared configuration |
| Task | upid, type, status, node, user, exitstatus | Async operations (migrations, backups, etc.) |

### Cross-System Links

Proxmox VMs and containers can host Kubernetes clusters. When Kubernetes nodes run on Proxmox VMs, MEHO can correlate via hostname or IP address matching -- tracing from a Kubernetes pod issue to the underlying Proxmox VM and the physical node it runs on.

## Troubleshooting

### API Token Permissions

**Symptom:** Operations return 403 Forbidden or "Permission check failed"
**Cause:** The API token doesn't have sufficient permissions, or "Privilege Separation" is enabled and the token has no dedicated permissions
**Fix:** Either uncheck "Privilege Separation" to inherit the user's permissions, or explicitly assign roles to the token. Use `PVEAuditor` for read-only or `PVEVMAdmin` for VM/container operations.

### Self-Signed Certificates

**Symptom:** Connection fails with SSL/TLS certificate verification errors
**Cause:** Proxmox uses self-signed certificates by default
**Fix:** Enable `disable_ssl_verification` in the connector configuration, or add the Proxmox CA certificate (`/etc/pve/pve-root-ca.pem`) to the MEHO server's trust store.

### Node vs. Cluster Access

**Symptom:** Operations only show resources from one node, not the entire cluster
**Cause:** Connected to a single node that is not part of a cluster, or the node is not aware of cluster membership
**Fix:** Ensure you're connecting to a node that is part of a Proxmox cluster. All nodes in a cluster share the same configuration and API. Verify cluster status with `get_cluster_status`.

### QEMU Guest Agent Not Installed

**Symptom:** `shutdown_vm` times out or fails; no guest IP information available
**Cause:** The QEMU Guest Agent is not installed or not running in the guest OS
**Fix:** Install `qemu-guest-agent` in the VM guest OS and enable the QEMU Guest Agent option in the VM hardware settings. Without the agent, use `stop_vm` for immediate power-off.

### VM ID Conflicts

**Symptom:** Clone or create operations fail with "VM ID already exists"
**Cause:** The specified VM ID is already in use by another VM or container in the cluster
**Fix:** VM IDs are unique across the entire Proxmox cluster (VMs and containers share the same ID space). Use `get_cluster_resources` to see all used IDs and choose an unused one.
