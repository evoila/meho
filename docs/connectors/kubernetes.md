# Kubernetes

> Last verified: v2.0

MEHO's Kubernetes connector provides native API access to your clusters using `kubernetes-asyncio`. With 40+ operations spanning pods, deployments, services, storage, and events, MEHO can diagnose issues, inspect workloads, and take corrective action across your entire Kubernetes estate -- all from a single conversation.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Service Account Token | `server_url`, `token` | Bearer token authentication (kubeconfig or in-cluster) |

### Setup

1. **Create a service account** with the appropriate RBAC permissions:

    ```bash
    kubectl create serviceaccount meho-reader -n default
    kubectl create clusterrolebinding meho-reader-binding \
      --clusterrole=view \
      --serviceaccount=default:meho-reader
    ```

2. **Get the token** (Kubernetes 1.24+):

    ```bash
    kubectl create token meho-reader -n default --duration=8760h
    ```

3. **Get the API server URL**:

    ```bash
    kubectl cluster-info | grep "Kubernetes control plane"
    ```

4. **Add the connector in MEHO** using the server URL and token.

!!! tip "Least Privilege"
    Start with the `view` ClusterRole for read-only access. Add `edit` only when operators need MEHO to take write actions like scaling deployments or restarting pods.

!!! warning "Skip TLS Verification"
    For local dev clusters (minikube, kind), you can enable `skip_tls_verification`. Never use this in production -- provide a CA certificate instead.

## Operations

### Core (Pods, Nodes, Namespaces, ConfigMaps, Secrets)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_pods` | READ | List all pods in a namespace or across all namespaces with status, containers, resource usage, IPs, and node placement |
| `get_pod` | READ | Get detailed pod information including status, containers, volumes, conditions, and events |
| `get_pod_logs` | READ | Retrieve logs from a pod container with tail lines, since time, and previous container support |
| `describe_pod` | READ | Comprehensive pod information including events, conditions, and volumes (like `kubectl describe pod`) |
| `delete_pod` | DESTRUCTIVE | Delete a pod (recreated if managed by a controller) |
| `list_nodes` | READ | List all cluster nodes with status, capacity, and allocatable resources |
| `get_node` | READ | Detailed node information including conditions, capacity, and system info |
| `describe_node` | READ | Comprehensive node information including running pods and events |
| `cordon_node` | WRITE | Mark a node as unschedulable (existing pods unaffected) |
| `uncordon_node` | WRITE | Mark a node as schedulable again |
| `list_namespaces` | READ | List all namespaces with status and labels |
| `get_namespace` | READ | Get namespace details including labels and annotations |
| `list_configmaps` | READ | List all ConfigMaps in a namespace |
| `get_configmap` | READ | Get a specific ConfigMap including data keys and values |
| `list_secrets` | READ | List all Secrets in a namespace (data values NOT returned for security) |
| `get_secret` | READ | Get Secret metadata (data values base64 encoded) |

### Workloads (Deployments, StatefulSets, DaemonSets, Jobs, CronJobs)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_deployments` | READ | List all deployments with replica counts, conditions, and availability |
| `get_deployment` | READ | Deployment details including replicas, strategy, and pod template |
| `scale_deployment` | WRITE | Scale a deployment to a specific number of replicas |
| `restart_deployment` | WRITE | Trigger a rolling restart of all pods in a deployment |
| `list_replicasets` | READ | List all ReplicaSets (useful for deployment history) |
| `get_replicaset` | READ | ReplicaSet details |
| `list_statefulsets` | READ | List all StatefulSets |
| `get_statefulset` | READ | StatefulSet details including replicas and volume claims |
| `scale_statefulset` | WRITE | Scale a StatefulSet to a specific number of replicas |
| `list_daemonsets` | READ | List all DaemonSets |
| `get_daemonset` | READ | DaemonSet details including desired/ready counts |
| `list_jobs` | READ | List all Jobs |
| `get_job` | READ | Job details including completion status and conditions |
| `list_cronjobs` | READ | List all CronJobs |
| `get_cronjob` | READ | CronJob details including schedule and last run time |

### Networking (Services, Ingresses, Endpoints, NetworkPolicies)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_services` | READ | List all services with type, cluster IP, external IP, and ports |
| `get_service` | READ | Service details including cluster IP, ports, and selectors |
| `list_ingresses` | READ | List all ingresses |
| `get_ingress` | READ | Ingress details including hosts, paths, and backends |
| `list_endpoints` | READ | List all endpoints (shows which pods back a service) |
| `get_endpoints` | READ | Endpoints for a specific service with ready/not-ready addresses |
| `list_network_policies` | READ | List all NetworkPolicies |
| `get_network_policy` | READ | NetworkPolicy details including ingress/egress rules |

### Storage (PVCs, PVs, StorageClasses)

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_pvcs` | READ | List all PersistentVolumeClaims |
| `get_pvc` | READ | PVC details including status, capacity, and bound PV |
| `list_pvs` | READ | List all PersistentVolumes (cluster-scoped) |
| `get_pv` | READ | PV details including capacity, access modes, and claim reference |
| `list_storageclasses` | READ | List all StorageClasses (cluster-scoped) |
| `get_storageclass` | READ | StorageClass details including provisioner and parameters |

### Events

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_events` | READ | List events in a namespace (debugging pod failures, image pull errors, scheduling) |
| `get_events_for_resource` | READ | Get all events related to a specific resource (pod, deployment, node, etc.) |
| `describe_deployment` | READ | Comprehensive deployment information including events and conditions |
| `describe_service` | READ | Comprehensive service information including endpoints and events |

## Example Queries

Ask MEHO questions like:

- "Show me all pods in CrashLoopBackOff in the production namespace"
- "What's the resource utilization of node worker-3?"
- "Scale the nginx deployment to 5 replicas"
- "Which pods are running on nodes that are NotReady?"
- "Get the logs from the api-gateway pod -- show me the last 200 lines"
- "List all PVCs that are in Pending state"
- "What events happened in the kube-system namespace in the last hour?"
- "Describe the frontend deployment and show me why pods are failing"
- "Which services don't have any endpoints backing them?"
- "Cordon worker-02 so no new pods get scheduled there"

## Topology

Kubernetes discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| Namespace | name, phase, labels | Scope container for all namespaced resources |
| Node | name, status, capacity, allocatable, kubelet_version | **Node -> VM** via `providerID` (links to VMware VM, GCP Instance, or AWS EC2) |
| Pod | name, namespace, phase, pod_ip, node_name, containers | Pod -> Node (scheduled on), Pod -> Service (via label selectors) |
| Deployment | name, namespace, replicas, ready_replicas, strategy | Manages ReplicaSets, owns Pods |
| ReplicaSet | name, namespace, replicas, ready_replicas | Created by Deployments, manages Pods |
| StatefulSet | name, namespace, replicas, service_name | Manages stateful Pods with persistent identity |
| DaemonSet | name, namespace, desired_number_scheduled, number_ready | Runs pods on all matching Nodes |
| Service | name, namespace, type, cluster_ip, ports, selector | Routes traffic to Pods via label selectors |
| Ingress | name, namespace, hosts, ingress_class, tls | Routes external traffic to Services |
| ConfigMap | name, namespace, data | Referenced by Pods for configuration |
| Secret | name, namespace, type | Referenced by Pods for sensitive data |
| PersistentVolumeClaim | name, namespace, phase, capacity, storage_class | Bound to PersistentVolumes |
| PersistentVolume | name, phase, capacity, reclaim_policy | Bound to PVCs, provisioned by StorageClasses |
| StorageClass | name, provisioner, reclaim_policy, volume_binding_mode | Dynamic provisioning of PVs |
| Event | type, reason, message, count, involved_object | Debugging information for all resource types |

### Cross-System Links

The most powerful cross-system link is **Node -> VM via providerID**. Kubernetes nodes expose a `spec.providerID` field that identifies the underlying infrastructure:

- **GCE:** `gce://project-id/zone/instance-name`
- **AWS:** `aws:///zone/instance-id` (with `_extracted_instance_id` attribute)
- **Azure:** VMSS format `vmss-name_instance-id` (case-insensitive matching per kubernetes/kubernetes#71994)
- **vSphere:** Matched via hostname or IP address to VMware VMs

This enables MEHO to trace from a failing pod, to the node it runs on, to the underlying VM in VMware/GCP/AWS -- automatically correlating across systems.

## Troubleshooting

### RBAC Permissions Insufficient

**Symptom:** Operations return 403 Forbidden errors
**Cause:** The service account token lacks permissions for the requested operation
**Fix:** Create a ClusterRoleBinding with the appropriate ClusterRole. Use `view` for read-only, `edit` for write operations, or create a custom Role with specific verbs and resources.

### Kubeconfig Context Issues

**Symptom:** Connection fails or connects to the wrong cluster
**Cause:** The server URL points to the wrong cluster or is unreachable
**Fix:** Verify the server URL with `kubectl cluster-info`. Ensure the MEHO server can reach the Kubernetes API server (check firewalls, VPN, network policies).

### Namespace Scoping

**Symptom:** Operations return empty results when resources exist
**Cause:** Operations are scoped to a specific namespace but resources are in a different one
**Fix:** Omit the namespace parameter to search across all namespaces, or verify the correct namespace name with `list_namespaces`.

### Token Expiration

**Symptom:** Operations that previously worked start returning 401 Unauthorized
**Cause:** Service account tokens created with `kubectl create token` have an expiration time
**Fix:** Create a new token with a longer duration (`--duration=8760h`) or use a ServiceAccount with a long-lived token Secret.

### Self-Signed Certificates

**Symptom:** Connection fails with TLS/SSL certificate verification errors
**Cause:** The cluster uses a self-signed CA certificate
**Fix:** Either provide the CA certificate when creating the connector, or enable `skip_tls_verification` for non-production environments.
