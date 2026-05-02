# Amazon Web Services

> Last verified: v2.3

MEHO's AWS connector provides native integration with Amazon Web Services using the boto3 SDK. Covering CloudWatch metrics, EC2 instances, ECS container services, EKS managed Kubernetes, Lambda functions, RDS databases, S3 storage, and VPC networking, MEHO gives you cross-service visibility into your AWS infrastructure -- from virtual machines and container clusters to serverless functions and database instances.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| IAM Access Key | `aws_access_key_id`, `aws_secret_access_key`, `default_region` | IAM user programmatic access key |

### Setup

1. **Create an IAM user** in the AWS Console:
    - Navigate to **IAM > Users > Create user**
    - Name it something descriptive (e.g., `meho-reader`)
    - Select **Programmatic access** (access key)

2. **Attach a permissions policy** to the IAM user:
    - **Read-only:** Attach the `ReadOnlyAccess` managed policy for broad read access
    - **Least privilege:** Use granular policies like `CloudWatchReadOnlyAccess`, `AmazonEC2ReadOnlyAccess`, `AmazonECS_FullAccess` (read), `AmazonEKSClusterPolicy`

3. **Generate an access key**:
    - On the user page, go to **Security credentials > Create access key**
    - Select **Third-party service** as the use case
    - Download or copy the access key ID and secret access key

4. **Add the connector in MEHO** using the access key ID, secret access key, and your default AWS region (e.g., `us-east-1`).

!!! tip "Least Privilege"
    Avoid using `AdministratorAccess`. For monitoring-only use cases, `CloudWatchReadOnlyAccess` + `AmazonEC2ReadOnlyAccess` is sufficient. Create a custom policy for the exact services you need.

!!! warning "Access Key Rotation"
    AWS access keys are long-lived credentials. Rotate them every 90 days using **IAM > Users > Security credentials > Create access key**, then deactivate the old key. MEHO does not use environment variables or instance profiles -- each connector has its own isolated credentials.

## Operations

### CloudWatch (4 operations)

Metrics, time series, alarms, and alarm history.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_metric_descriptors` | READ | List available CloudWatch metrics, filterable by namespace (e.g., AWS/EC2, AWS/RDS) |
| `get_time_series` | READ | Get time series data for a specific metric with configurable time window and statistics |
| `list_alarms` | READ | List CloudWatch metric alarms, filterable by state (OK, ALARM, INSUFFICIENT_DATA) |
| `get_alarm_history` | READ | Get state transition history for a specific alarm |

### EC2 (4 operations)

Instances and security groups.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_instances` | READ | List all EC2 instances with type, IPs, VPC, security groups, and tags -- filterable by state and tag |
| `get_instance` | READ | Get detailed instance information including network interfaces and security groups |
| `list_security_groups` | READ | List security groups with inbound/outbound rules, filterable by VPC |
| `get_security_group` | READ | Get detailed security group rules including protocols, ports, and CIDRs |

### ECS (5 operations)

Elastic Container Service clusters, services, and tasks.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_ecs_clusters` | READ | List all ECS clusters with running/pending task counts and capacity providers |
| `list_ecs_services` | READ | List services in a cluster with deployment details and load balancer configuration |
| `list_ecs_tasks` | READ | List tasks in a cluster, optionally filtered by service |
| `get_ecs_service` | READ | Get detailed service information including deployments and task definition |
| `get_ecs_task` | READ | Get detailed task information including containers and resource allocation |

### EKS (4 operations)

Elastic Kubernetes Service clusters and node groups.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_eks_clusters` | READ | List all EKS clusters with version, endpoint, VPC config, and status |
| `get_eks_cluster` | READ | Get detailed cluster information including Kubernetes version and network settings |
| `list_eks_node_groups` | READ | List node groups for a cluster with scaling config, instance types, and health |
| `get_eks_node_group` | READ | Get detailed node group information including scaling configuration and health |

### Lambda (2 operations)

Serverless functions.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_functions` | READ | List all Lambda functions with runtime, handler, memory, timeout, and state |
| `get_function` | READ | Get detailed function information including configuration, layers, and state |

### RDS (2 operations)

Managed databases.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_rds_instances` | READ | List all RDS instances with engine, version, class, status, and endpoint |
| `get_rds_instance` | READ | Get detailed instance information including storage configuration and Multi-AZ status |

### S3 (1 operation)

Object storage.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_buckets` | READ | List all S3 buckets (global) with public access block status |

### VPC (3 operations)

Networking.

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_vpcs` | READ | List all VPCs with CIDR blocks, state, tenancy, and tags |
| `get_vpc` | READ | Get detailed VPC information including CIDR associations and DHCP options |
| `list_subnets` | READ | List subnets with availability zone, CIDR block, and available IPs -- filterable by VPC |

## Example Queries

Ask MEHO questions like:

- "List all running EC2 instances in the production account"
- "What's the CPU utilization for my-web-server over the last 4 hours?"
- "Are there any CloudWatch alarms in ALARM state right now?"
- "Show me the EKS clusters and their node group scaling configuration"
- "List all ECS services in the api-cluster and their deployment status"
- "What Lambda functions exist and what runtimes are they using?"
- "Show me the RDS instances and their Multi-AZ configuration"
- "List all security groups that have inbound rules allowing traffic from 0.0.0.0/0"
- "What S3 buckets exist and which have public access enabled?"
- "Show me the VPCs and their CIDR blocks"

## Topology

Amazon Web Services discovers these entity types:

| Entity Type | Key Properties | Cross-System Links |
|-------------|----------------|-------------------|
| EC2Instance | instance_id, name, instance_type, state, private_ip, public_ip, vpc_id, tags | **EC2Instance -> K8s Node** via providerID (`aws:///zone/instance-id`), EC2Instance -> VPC, EC2Instance -> SecurityGroup |
| EKSCluster | name, arn, status, version, endpoint, vpc_id, tags | Managed Kubernetes, contains node groups running on EC2 instances |
| ECSCluster | cluster_name, cluster_arn, status, running_tasks_count, active_services_count | ECS clusters run tasks on EC2 or Fargate |
| VPC | vpc_id, state, cidr_block, is_default, tags | Contains Subnets and SecurityGroups |
| Subnet | subnet_id, vpc_id, availability_zone, cidr_block, tags | Regional network segment within a VPC |
| SecurityGroup | group_id, group_name, vpc_id, description | Virtual firewall controlling traffic for EC2, RDS, and other resources |

### Cross-System Links

The most important cross-system link is **EC2 Instance -> Kubernetes Node via providerID**. EKS worker nodes are EC2 instances that expose their providerID in the format `aws:///availability-zone/instance-id`, which MEHO uses to correlate Kubernetes workload issues back to the underlying EC2 instance. This enables tracing from a failing pod to its node, to the EC2 instance, to CloudWatch metrics -- revealing whether the root cause is at the application, Kubernetes, or infrastructure layer.

ECS provides a separate container orchestration path: ECS clusters run tasks (containers) either on EC2 instances or AWS Fargate. When using EC2 launch type, container issues can be traced to the underlying instance.

## Troubleshooting

### IAM Permissions

**Symptom:** Operations return `AccessDenied` or `UnauthorizedOperation`
**Cause:** The IAM user lacks the necessary permissions for the requested operation
**Fix:** Attach the appropriate IAM policies. Common policies needed:

- `CloudWatchReadOnlyAccess` -- Read CloudWatch metrics and alarms
- `AmazonEC2ReadOnlyAccess` -- Read EC2 instances, security groups, VPCs
- `AmazonECS_FullAccess` -- Read ECS clusters, services, tasks (no write-only policy exists)
- `AmazonEKSClusterPolicy` -- Read EKS clusters and node groups
- `AWSLambda_ReadOnlyAccess` -- Read Lambda functions
- `AmazonRDSReadOnlyAccess` -- Read RDS instances
- `AmazonS3ReadOnlyAccess` -- Read S3 buckets
- `AmazonVPCReadOnlyAccess` -- Read VPCs and subnets

### Region Scoping

**Symptom:** Operations return empty results when resources exist
**Cause:** The connector is configured with a different default region than where your resources are deployed
**Fix:** Verify the `default_region` matches the region containing your resources. Most AWS operations are region-scoped -- resources in `us-west-2` are not visible when querying `us-east-1`. You can override the region per-query using the `region` parameter.

### API Rate Limiting

**Symptom:** Operations fail with `ThrottlingException` or `TooManyRequestsException`
**Cause:** Too many API requests in a short time period
**Fix:** AWS APIs have per-account, per-region rate limits. If MEHO is making many concurrent queries, the agent will automatically retry. For persistent throttling, consider requesting a rate limit increase via **AWS Support** or spacing out investigations.
