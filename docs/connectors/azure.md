# Microsoft Azure

> Last verified: v2.3

MEHO's Azure connector provides native integration with Microsoft Azure using the official Azure SDK for Python with first-class async support. Covering Compute (VMs, disks), Azure Monitor (metrics, alerts, activity log), AKS (managed Kubernetes), Networking (VNets, NSGs, load balancers), Storage (accounts, blob containers), and Web (App Service, Function Apps), MEHO gives you cross-service visibility into your Azure infrastructure.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Service Principal | `tenant_id`, `client_id`, `client_secret`, `subscription_id` | Azure AD app registration with client secret |

### Setup

1. **Register an application** in Azure Active Directory:
    - Navigate to **Azure AD > App registrations > New registration**
    - Name it something descriptive (e.g., `meho-reader`)
    - Set **Supported account types** to single tenant

2. **Create a client secret**:
    - On the app registration page, go to **Certificates & secrets > New client secret**
    - Set an appropriate expiration (recommended: 12 months)
    - Copy the secret value immediately -- it cannot be viewed again

3. **Assign the Reader role** on the subscription:
    - Navigate to **Subscriptions > your-subscription > Access control (IAM) > Add role assignment**
    - Select **Reader** role (or more granular: `Monitoring Reader`, `Network Reader`)
    - Assign to the app registration you created

4. **Add the connector in MEHO** using the tenant ID, client ID, client secret, and subscription ID. Optionally set a `resource_group_filter` to scope operations to a specific resource group.

!!! tip "Least Privilege"
    Use the `Reader` role for broad read access. For monitoring-only use cases, `Monitoring Reader` + `Reader` on specific resource groups is sufficient. Avoid `Contributor` or `Owner` roles.

!!! warning "Secret Expiration"
    Azure client secrets have an expiration date. Set a calendar reminder to rotate the secret before it expires. When creating a new secret, update the connector credentials in MEHO and delete the old secret. Consider using managed identities for production deployments.

## Operations

### Compute (8 operations)

Virtual machines, managed disks, availability sets, and resource groups.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_azure_vms` | READ | List all VMs with name, size, location, provisioning state, OS type, and tags -- filterable by resource group |
| `get_azure_vm` | READ | Get detailed VM information including power state, VM agent status, and OS details |
| `get_azure_vm_instance_view` | READ | Get runtime status: power state, VM agent version, boot diagnostics, and maintenance state |
| `get_azure_vm_metrics` | READ | Get common VM performance metrics (CPU, memory, disk, network) from Azure Monitor |
| `list_azure_disks` | READ | List managed disks with size, SKU, state, and OS type |
| `get_azure_disk` | READ | Get detailed disk information including size, SKU, and provisioning state |
| `list_azure_availability_sets` | READ | List availability sets with fault/update domain counts |
| `list_azure_resource_groups` | READ | List all resource groups with location, state, and tags |

### Monitor (7 operations)

Metrics queries, metric definitions, alerts, activity log, and action groups.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `get_azure_metrics` | READ | Query Azure Monitor metrics for any resource by ARM URI with configurable timespan and aggregation |
| `list_azure_metric_definitions` | READ | List available metric definitions for a resource (discover what metrics exist) |
| `list_azure_metric_namespaces` | READ | List metric namespaces for a resource |
| `list_azure_metric_alerts` | READ | List metric alert rules with severity, criteria, and thresholds |
| `get_azure_metric_alert` | READ | Get detailed alert rule information including evaluation criteria and action groups |
| `list_azure_activity_log` | READ | List activity log events with OData filter support |
| `list_azure_action_groups` | READ | List notification action groups used by alert rules |

### AKS (7 operations)

Azure Kubernetes Service clusters, node pools, credentials, and upgrades.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_azure_aks_clusters` | READ | List AKS clusters with Kubernetes version, power state, FQDN, and agent pool count |
| `get_azure_aks_cluster` | READ | Get detailed cluster information including network plugin/policy and service CIDR |
| `get_azure_aks_cluster_health` | READ | Get cluster health summary with per-pool provisioning and power state |
| `list_azure_aks_node_pools` | READ | List node pools with VM size, count, auto-scaling, OS type, and availability zones |
| `get_azure_aks_node_pool` | READ | Get detailed node pool information including auto-scaling configuration |
| `get_azure_aks_credentials` | READ | Get kubeconfig-relevant metadata (FQDN, API server access profile, AAD profile) |
| `list_azure_aks_upgrades` | READ | List available Kubernetes version upgrades for a cluster |

### Network (8 operations)

Virtual networks, subnets, NSGs, load balancers, and public IPs.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_azure_vnets` | READ | List virtual networks with address prefixes, DNS servers, and subnet count |
| `get_azure_vnet` | READ | Get detailed VNet information including all subnets with NSG associations |
| `list_azure_subnets` | READ | List subnets in a VNet with address prefix, NSG, route table, and service endpoints |
| `list_azure_nsgs` | READ | List network security groups with custom and default security rules |
| `get_azure_nsg` | READ | Get detailed NSG rules including priority, direction, access, protocol, and port ranges |
| `list_azure_load_balancers` | READ | List load balancers with SKU, frontend IPs, and backend pool counts |
| `get_azure_load_balancer` | READ | Get detailed load balancer information including frontend IPs and backend pools |
| `list_azure_public_ips` | READ | List public IP addresses with allocation method, IP version, and SKU |

### Storage (6 operations)

Storage accounts, blob containers, managed disks, account keys, and usage.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_azure_storage_accounts` | READ | List storage accounts with SKU, kind, access tier, HNS status, and primary endpoints |
| `get_azure_storage_account` | READ | Get detailed storage account information including SKU and access tier |
| `list_azure_blob_containers` | READ | List blob containers in a storage account with access level and lease state |
| `list_azure_managed_disks` | READ | List managed disks (storage-centric view) with name, size, SKU, and state |
| `get_azure_storage_account_keys` | READ | Get storage account access key metadata (key names and permissions, not key values) |
| `list_azure_storage_usage` | READ | List storage usage statistics for a region (quota consumption) |

### Web (6 operations)

App Service web apps, Function Apps, and App Service plans.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_azure_web_apps` | READ | List App Service web apps with state, hostname, runtime stack, and HTTPS status |
| `get_azure_web_app` | READ | Get detailed web app information including App Service plan and runtime |
| `list_azure_function_apps` | READ | List Azure Function Apps with state, hostname, and runtime stack |
| `get_azure_function_app` | READ | Get detailed Function App information including kind and runtime |
| `list_azure_app_service_plans` | READ | List App Service plans with SKU tier, capacity, and hosted site count |
| `get_azure_app_service_plan` | READ | Get detailed App Service plan information including SKU and status |

## Example Queries

Ask MEHO questions like:

- "List all Azure VMs in the production resource group and their power state"
- "What's the CPU utilization for my-web-vm over the last 4 hours?"
- "Are there any metric alerts firing right now?"
- "Show me the AKS clusters and their node pool autoscaling configuration"
- "What Kubernetes version upgrades are available for my AKS cluster?"
- "List all NSG rules that allow inbound traffic from the internet"
- "Show me the storage accounts and their access tiers"
- "What web apps are running and what runtime stacks are they using?"
- "List the activity log events for the production resource group in the last hour"
- "Show me the load balancers and their backend pool configuration"

## Topology

Microsoft Azure discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| AzureVM | id, name, location, resource_group, vm_size, provisioning_state, os_type, tags | **AzureVM -> K8s Node** via AKS node resource group, AzureVM -> AzureDisk (attached), AzureVM -> AzureVNet (NIC) |
| AzureDisk | id, name, location, resource_group, size_gb, sku_name, disk_state | Attached to AzureVMs |
| AzureAKSCluster | id, name, location, resource_group, kubernetes_version, fqdn, agent_pool_count | Managed Kubernetes, contains AzureNodePools |
| AzureNodePool | name, vm_size, count, os_type, mode, enable_auto_scaling | Group of AzureVMs within an AKS cluster |
| AzureVNet | id, name, location, resource_group, address_prefixes, subnet_count | Contains AzureSubnets |
| AzureSubnet | id, name, address_prefix, nsg_id, route_table_id, service_endpoints | Network segment within a VNet |
| AzureNSG | id, name, location, resource_group, security_rules | Controls traffic to VNet resources |
| AzureLoadBalancer | id, name, location, resource_group, sku_name, frontend_ip_count | Distributes traffic across backend pools |
| AzureStorageAccount | id, name, location, resource_group, sku_name, kind, access_tier | Blob, file, queue, and table storage |
| AzureAppService | id, name, location, resource_group, state, default_host_name, runtime_stack | PaaS web hosting |
| AzureFunctionApp | id, name, location, resource_group, state, default_host_name, runtime_stack | Serverless compute |
| AzureResourceGroup | id, name, location, provisioning_state, tags | Logical container for Azure resources |

### Cross-System Links

The most important cross-system link is **AKS Node Pool -> Kubernetes Node**. AKS creates a node resource group containing the underlying VMs for each node pool. MEHO correlates AKS cluster nodes with Kubernetes nodes to enable tracing from a failing pod to its AKS node pool, to the underlying VM, to Azure Monitor metrics -- revealing whether the root cause is at the application, Kubernetes, or infrastructure layer.

Azure Monitor connects metrics across all resource types: VM performance metrics, storage account throughput, App Service response times, and AKS cluster health can all be queried through the same `get_azure_metrics` operation using the resource's ARM URI.

## Troubleshooting

### Service Principal Permissions

**Symptom:** Operations return `AuthenticationFailed` or `AuthorizationFailed`
**Cause:** The service principal lacks the necessary RBAC roles for the requested operation
**Fix:** Assign the appropriate roles in **Subscriptions > Access control (IAM)**. Common roles needed:

- `Reader` -- Read all resources in the subscription
- `Monitoring Reader` -- Read Azure Monitor metrics and alerts
- `Network Reader` -- Read networking resources (VNets, NSGs, LBs)
- `Storage Account Key Operator Service Role` -- Read storage account key metadata

### Subscription Scoping

**Symptom:** Operations return empty results when resources exist
**Cause:** The connector is configured with a different subscription ID, or the `resource_group_filter` is set to a resource group that doesn't contain the target resources
**Fix:** Verify the `subscription_id` matches the subscription containing your resources. If using `resource_group_filter`, check that the filter matches the correct resource group name. Clear the filter to query across all resource groups.

### Client Secret Expiration

**Symptom:** All operations fail with `ClientAuthenticationError`
**Cause:** The service principal's client secret has expired
**Fix:** Create a new client secret in **Azure AD > App registrations > your-app > Certificates & secrets**, update the connector credentials in MEHO, and delete the expired secret.

### Azure SDK Dependencies

**Symptom:** Connection fails with `ImportError` mentioning azure packages
**Cause:** Required Azure SDK packages are not installed in the MEHO Docker image
**Fix:** The following packages are required: `azure-identity`, `azure-mgmt-compute`, `azure-mgmt-monitor`, `azure-mgmt-containerservice`, `azure-mgmt-network`, `azure-mgmt-storage`, `azure-mgmt-web`, `azure-mgmt-resource`. These are included in the standard MEHO Docker image but may be missing in custom builds.
