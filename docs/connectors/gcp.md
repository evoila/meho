# Google Cloud Platform

> Last verified: v2.0

MEHO's GCP connector provides native integration with Google Cloud using the official Python SDKs. Covering Compute Engine, GKE, networking, Cloud Monitoring, Cloud Build, and Artifact Registry, MEHO gives you cross-service visibility into your Google Cloud infrastructure -- from VM instances and Kubernetes clusters to CI/CD pipelines and container images.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Service Account JSON | `project_id`, `service_account_json`, `default_region`, `default_zone` | Service account key file for programmatic access |

### Setup

1. **Create a service account** in the Google Cloud Console:
    - Navigate to **IAM & Admin > Service Accounts**
    - Click **Create Service Account**
    - Name it something descriptive (e.g., `meho-reader`)

2. **Assign roles** to the service account:
    - **Read-only:** `Viewer` role (or more granular: `Compute Viewer`, `Monitoring Viewer`, `Container Cluster Viewer`)
    - **Operator:** Add `Compute Instance Admin` for VM lifecycle operations

3. **Create and download a JSON key**:
    - On the service account page, go to **Keys > Add Key > Create new key > JSON**
    - Download the JSON key file

4. **Add the connector in MEHO** using the project ID, JSON key contents, and optionally a default region/zone.

!!! tip "Least Privilege"
    Use predefined roles instead of `Editor` or `Owner`. For monitoring-only use cases, `Monitoring Viewer` + `Compute Viewer` is sufficient.

!!! warning "Key Security"
    Service account JSON keys are long-lived credentials. Rotate them periodically and store them securely. Consider using Workload Identity Federation for production environments.

## Operations

### Compute Engine (15 operations)

VM instances, persistent disks, snapshots, zones, and machine types.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_instances` | READ | List all VM instances in the project with name, zone, machine type, status, IPs, and labels |
| `get_instance` | READ | Detailed instance information including network interfaces, disks, and status |
| `start_instance` | WRITE | Start a stopped instance |
| `stop_instance` | WRITE | Gracefully stop a running instance (preserves persistent disks) |
| `reset_instance` | WRITE | Hard reset an instance (immediate restart without graceful shutdown) |
| `delete_instance` | DESTRUCTIVE | Permanently delete an instance |
| `get_instance_serial_port_output` | READ | Get serial port console log (useful for boot issue debugging) |
| `list_disks` | READ | List all persistent disks with name, size, type, status, and attached instances |
| `get_disk` | READ | Disk details including size, type, source image/snapshot, and users |
| `list_snapshots` | READ | List all disk snapshots with source disk, size, and status |
| `get_snapshot` | READ | Snapshot details including source disk and storage size |
| `create_snapshot` | WRITE | Create a disk snapshot (incremental, for backup) |
| `delete_snapshot` | DESTRUCTIVE | Delete a disk snapshot (irreversible) |
| `list_zones` | READ | List all available zones with region and status |
| `list_machine_types` | READ | List available machine types with vCPU count and memory specs |

### GKE (7 operations)

Google Kubernetes Engine clusters, node pools, and cluster operations.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_clusters` | READ | List all GKE clusters with name, location, status, version, node count, and endpoint |
| `get_cluster` | READ | Detailed cluster information including master version, node pools, networking, and addons |
| `get_cluster_health` | READ | Cluster health status including master health, node health, and recent conditions |
| `list_node_pools` | READ | List node pools with machine type, node count, and autoscaling config |
| `get_node_pool` | READ | Node pool details including disk config, autoscaling, and management settings |
| `get_cluster_credentials` | READ | Information needed to configure kubectl (endpoint and CA certificate) |
| `list_cluster_operations` | READ | Recent GKE operations (create, update, delete) for tracking changes |
| `delete_cluster` | DESTRUCTIVE | Delete a GKE cluster |

### Networking (7 operations)

VPC networks, subnets, firewall rules, routes, and static IPs.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_networks` | READ | List all VPC networks with routing mode, subnetworks, and peerings |
| `get_network` | READ | Detailed VPC network including routing config and peering connections |
| `list_subnetworks` | READ | List all subnetworks with region, IP range, and parent network |
| `get_subnetwork` | READ | Subnetwork details including IP range, gateway, and secondary ranges |
| `list_firewalls` | READ | List all firewall rules with direction, priority, and allowed/denied protocols |
| `get_firewall` | READ | Firewall rule details including source/target specs and traffic rules |
| `list_routes` | READ | List all routes with destination and next hop |
| `list_addresses` | READ | List reserved static IP addresses with type and status |

### Cloud Monitoring (8 operations)

Metrics, alert policies, notification channels, and uptime checks.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_metric_descriptors` | READ | List available metric types with labels and value types |
| `get_metric_descriptor` | READ | Details about a specific metric type |
| `get_time_series` | READ | Query time series data with timestamps and values (use instance_id for GCE resources) |
| `get_instance_metrics` | READ | Common metrics for a Compute Engine instance (CPU, disk, network) -- auto-resolves instance name to ID |
| `list_alert_policies` | READ | List all alert policies with conditions and enabled status |
| `get_alert_policy` | READ | Alert policy details including conditions and notification channels |
| `list_notification_channels` | READ | List notification channels (email, SMS, PagerDuty, etc.) |
| `list_uptime_checks` | READ | List uptime check configurations (HTTP, HTTPS, TCP) |

### Cloud Build (6 operations)

Build pipelines, step-level execution, logs, triggers, and build management.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_builds` | READ | List Cloud Build executions with status, timing, source (repo, commit, branch), and output images |
| `get_build` | READ | Detailed build info with step-by-step execution, per-step status/duration, failure point, and output images |
| `list_build_triggers` | READ | List automated trigger configurations with source repos and branch/tag patterns |
| `get_build_logs` | READ | Per-step build logs for diagnosing failures (tailed to 200 lines per step) |
| `cancel_build` | WRITE | Cancel a running build (must be in QUEUED or WORKING status) |
| `retry_build` | WRITE | Retry a failed or cancelled build (creates a new build with same config) |

### Artifact Registry (2 operations)

Container image repositories and Docker image browsing.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_artifact_repositories` | READ | List Artifact Registry repositories with format (Docker, Maven, NPM), location, and size |
| `list_docker_images` | READ | List Docker images with tags, digests, upload time, and size grouped by image name |

## Example Queries

Ask MEHO questions like:

- "List all Compute Engine instances in the us-central1 region"
- "Show me instances that are stopped but still have disks attached"
- "What's the CPU utilization for my-web-server over the last hour?"
- "Are there any firing alert policies right now?"
- "Show me the GKE clusters and their node pool autoscaling configuration"
- "What disk snapshots exist and when were they last created?"
- "List all firewall rules that allow SSH from 0.0.0.0/0"
- "Show me the recent Cloud Build failures and their logs"
- "Which Artifact Registry repositories have images and how large are they?"
- "Get the serial console output for my-stuck-vm to debug its boot issue"

## Topology

Google Cloud Platform discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| Instance | id, name, zone, machine_type, status, internal_ip, external_ip, labels | **Instance -> K8s Node** via providerID (`gce://project/zone/name`), Instance -> Disk (attached), Instance -> Network (NIC) |
| Disk | id, name, zone, size_gb, type, status, users | Attached to Instances, source for Snapshots |
| Snapshot | id, name, source_disk, disk_size_gb, storage_bytes | Created from Disks, used to create new Disks |
| VPCNetwork | id, name, routing_mode, subnetworks, mtu | Contains Subnetworks and Firewall rules |
| Subnetwork | id, name, network, region, ip_cidr_range, gateway | Regional network within a VPC |
| Firewall | id, name, network, direction, priority, source_ranges, allowed | Controls traffic to/from Instances |
| GKECluster | name, location, status, master_version, node_count, endpoint | Managed Kubernetes, contains NodePools |
| NodePool | name, status, machine_type, disk_size_gb, autoscaling | Group of GCE Instances within a GKE Cluster |
| MetricDescriptor | type, display_name, metric_kind, value_type | Defines available monitoring metrics |
| AlertPolicy | name, display_name, enabled, conditions, notification_channels | Monitoring alert configuration |

### Cross-System Links

The most important cross-system link is **GCE Instance -> Kubernetes Node via providerID**. GKE nodes expose their providerID in the format `gce://project-id/zone/instance-name`, which MEHO uses to correlate Kubernetes workload issues back to the underlying GCE instance. This enables tracing from a failing pod to its node, to the GCE instance, to Cloud Monitoring metrics -- revealing whether the root cause is at the application, Kubernetes, or infrastructure layer.

Cloud Build and Artifact Registry connect the CI/CD pipeline story: build failures can be traced through step-level logs, and deployed container images can be tracked from registry to GKE cluster.

## Troubleshooting

### Service Account Permissions

**Symptom:** Operations return 403 Forbidden or "The caller does not have permission"
**Cause:** The service account lacks the necessary IAM roles for the requested operation
**Fix:** Add the appropriate roles in **IAM & Admin > IAM**. Common roles needed:

- `roles/compute.viewer` -- Read Compute Engine resources
- `roles/container.clusterViewer` -- Read GKE clusters
- `roles/monitoring.viewer` -- Read Cloud Monitoring data
- `roles/cloudbuild.builds.viewer` -- Read Cloud Build data
- `roles/artifactregistry.reader` -- Read Artifact Registry

### Project Scoping

**Symptom:** Operations return empty results when resources exist
**Cause:** The connector is configured with the wrong project ID
**Fix:** Verify the project ID matches the project containing your resources. Use `gcloud projects list` to see all available projects.

### API Enablement

**Symptom:** Operations fail with "API not enabled" or "has not been used in project"
**Cause:** The required Google Cloud API is not enabled for the project
**Fix:** Enable the necessary APIs in **APIs & Services > Library**:

- Compute Engine API (`compute.googleapis.com`)
- Kubernetes Engine API (`container.googleapis.com`)
- Cloud Monitoring API (`monitoring.googleapis.com`)
- Cloud Build API (`cloudbuild.googleapis.com`)
- Artifact Registry API (`artifactregistry.googleapis.com`)

### Cloud Monitoring Instance ID

**Symptom:** `get_time_series` returns no data for a specific instance
**Cause:** Cloud Monitoring uses numeric instance IDs, not instance names
**Fix:** Use `get_instance_metrics` instead, which automatically resolves instance names to IDs. If using `get_time_series` directly, first get the numeric ID from `get_instance` and pass it as `instance_id`.

### Quota and Rate Limits

**Symptom:** Operations fail with "Rate Limit Exceeded" or "Quota exceeded"
**Cause:** Too many API requests in a short time period
**Fix:** GCP APIs have per-project rate limits. If MEHO is making many concurrent queries, consider increasing your API quotas in **IAM & Admin > Quotas** or spacing out operations.
