# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Object Serializers.

Convert raw boto3 response dictionaries into normalized, consistently-keyed
dictionaries for all 8 AWS service types. Each serializer extracts relevant
fields and applies helpers for tag normalization and timestamp formatting.
"""

from typing import Any

from meho_app.modules.connectors.aws.helpers import format_aws_timestamp, normalize_tags


# =========================================================================
# EC2 SERIALIZERS
# =========================================================================


def serialize_ec2_instance(instance: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 EC2 instance dict to a normalized dictionary.

    Args:
        instance: Raw boto3 describe_instances response item.

    Returns:
        Normalized instance data.
    """
    tags = normalize_tags(instance.get("Tags"))
    name = tags.get("Name", "")

    security_groups = [
        {"id": sg.get("GroupId", ""), "name": sg.get("GroupName", "")}
        for sg in instance.get("SecurityGroups", [])
    ]

    return {
        "instance_id": instance.get("InstanceId", ""),
        "name": name,
        "instance_type": instance.get("InstanceType", ""),
        "state": (instance.get("State") or {}).get("Name", ""),
        "availability_zone": (instance.get("Placement") or {}).get("AvailabilityZone", ""),
        "private_ip": instance.get("PrivateIpAddress"),
        "public_ip": instance.get("PublicIpAddress"),
        "vpc_id": instance.get("VpcId"),
        "subnet_id": instance.get("SubnetId"),
        "launch_time": format_aws_timestamp(instance.get("LaunchTime")),
        "tags": tags,
        "security_groups": security_groups,
    }


def serialize_security_group(sg: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 security group dict to a normalized dictionary.

    Args:
        sg: Raw boto3 describe_security_groups response item.

    Returns:
        Normalized security group data.
    """

    def _parse_ip_permissions(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rules = []
        for perm in permissions:
            sources = []
            for ip_range in perm.get("IpRanges", []):
                sources.append(ip_range.get("CidrIp", ""))
            for ipv6_range in perm.get("Ipv6Ranges", []):
                sources.append(ipv6_range.get("CidrIpv6", ""))
            for group in perm.get("UserIdGroupPairs", []):
                sources.append(group.get("GroupId", ""))

            rules.append({
                "protocol": perm.get("IpProtocol", ""),
                "from_port": perm.get("FromPort"),
                "to_port": perm.get("ToPort"),
                "sources": sources,
            })
        return rules

    return {
        "group_id": sg.get("GroupId", ""),
        "group_name": sg.get("GroupName", ""),
        "description": sg.get("Description", ""),
        "vpc_id": sg.get("VpcId"),
        "inbound_rules": _parse_ip_permissions(sg.get("IpPermissions", [])),
        "outbound_rules": _parse_ip_permissions(sg.get("IpPermissionsEgress", [])),
        "tags": normalize_tags(sg.get("Tags")),
    }


# =========================================================================
# CLOUDWATCH SERIALIZERS
# =========================================================================


def serialize_cloudwatch_metric(metric: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 CloudWatch metric dict.

    Args:
        metric: Raw boto3 list_metrics response item.

    Returns:
        Normalized metric data.
    """
    dimensions = [
        {"name": d.get("Name", ""), "value": d.get("Value", "")}
        for d in metric.get("Dimensions", [])
    ]

    return {
        "namespace": metric.get("Namespace", ""),
        "metric_name": metric.get("MetricName", ""),
        "dimensions": dimensions,
    }


def serialize_metric_data_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 CloudWatch GetMetricData result.

    Args:
        result: Raw boto3 get_metric_data MetricDataResults item.

    Returns:
        Normalized metric data result.
    """
    timestamps = [
        format_aws_timestamp(ts) for ts in result.get("Timestamps", [])
    ]

    return {
        "id": result.get("Id", ""),
        "label": result.get("Label", ""),
        "timestamps": timestamps,
        "values": [float(v) for v in result.get("Values", [])],
        "status_code": result.get("StatusCode", ""),
    }


def serialize_cloudwatch_alarm(alarm: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 CloudWatch alarm dict.

    Args:
        alarm: Raw boto3 describe_alarms response item.

    Returns:
        Normalized alarm data.
    """
    dimensions = [
        {"name": d.get("Name", ""), "value": d.get("Value", "")}
        for d in alarm.get("Dimensions", [])
    ]

    return {
        "alarm_name": alarm.get("AlarmName", ""),
        "alarm_arn": alarm.get("AlarmArn", ""),
        "state_value": alarm.get("StateValue", ""),
        "state_reason": alarm.get("StateReason", ""),
        "metric_name": alarm.get("MetricName", ""),
        "namespace": alarm.get("Namespace", ""),
        "threshold": alarm.get("Threshold"),
        "comparison_operator": alarm.get("ComparisonOperator", ""),
        "evaluation_periods": alarm.get("EvaluationPeriods"),
        "period": alarm.get("Period"),
        "statistic": alarm.get("Statistic", ""),
        "dimensions": dimensions,
        "actions_enabled": alarm.get("ActionsEnabled", False),
        "alarm_actions": alarm.get("AlarmActions", []),
        "updated_timestamp": format_aws_timestamp(alarm.get("StateUpdatedTimestamp")),
    }


# =========================================================================
# ECS SERIALIZERS
# =========================================================================


def serialize_ecs_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 ECS cluster dict.

    Args:
        cluster: Raw boto3 describe_clusters response item.

    Returns:
        Normalized ECS cluster data.
    """
    return {
        "cluster_name": cluster.get("clusterName", ""),
        "cluster_arn": cluster.get("clusterArn", ""),
        "status": cluster.get("status", ""),
        "running_tasks_count": cluster.get("runningTasksCount", 0),
        "pending_tasks_count": cluster.get("pendingTasksCount", 0),
        "active_services_count": cluster.get("activeServicesCount", 0),
        "registered_container_instances_count": cluster.get(
            "registeredContainerInstancesCount", 0
        ),
        "capacity_providers": cluster.get("capacityProviders", []),
        "tags": normalize_tags(cluster.get("tags")),
    }


def serialize_ecs_service(service: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 ECS service dict.

    Args:
        service: Raw boto3 describe_services response item.

    Returns:
        Normalized ECS service data.
    """
    deployments = []
    for dep in service.get("deployments", []):
        deployments.append({
            "id": dep.get("id", ""),
            "status": dep.get("status", ""),
            "task_definition": dep.get("taskDefinition", ""),
            "desired_count": dep.get("desiredCount", 0),
            "running_count": dep.get("runningCount", 0),
            "created_at": format_aws_timestamp(dep.get("createdAt")),
        })

    return {
        "service_name": service.get("serviceName", ""),
        "service_arn": service.get("serviceArn", ""),
        "cluster_arn": service.get("clusterArn", ""),
        "status": service.get("status", ""),
        "desired_count": service.get("desiredCount", 0),
        "running_count": service.get("runningCount", 0),
        "pending_count": service.get("pendingCount", 0),
        "task_definition": service.get("taskDefinition", ""),
        "launch_type": service.get("launchType", ""),
        "deployment_configuration": service.get("deploymentConfiguration"),
        "deployments": deployments,
        "load_balancers": service.get("loadBalancers", []),
        "tags": normalize_tags(service.get("tags")),
    }


def serialize_ecs_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 ECS task dict.

    Args:
        task: Raw boto3 describe_tasks response item.

    Returns:
        Normalized ECS task data.
    """
    containers = []
    for container in task.get("containers", []):
        containers.append({
            "name": container.get("name", ""),
            "image": container.get("image", ""),
            "status": container.get("lastStatus", ""),
            "exit_code": container.get("exitCode"),
            "reason": container.get("reason"),
        })

    return {
        "task_arn": task.get("taskArn", ""),
        "task_definition_arn": task.get("taskDefinitionArn", ""),
        "cluster_arn": task.get("clusterArn", ""),
        "container_instance_arn": task.get("containerInstanceArn"),
        "last_status": task.get("lastStatus", ""),
        "desired_status": task.get("desiredStatus", ""),
        "started_at": format_aws_timestamp(task.get("startedAt")),
        "stopped_at": format_aws_timestamp(task.get("stoppedAt")),
        "stopped_reason": task.get("stoppedReason"),
        "containers": containers,
        "cpu": task.get("cpu"),
        "memory": task.get("memory"),
        "launch_type": task.get("launchType", ""),
        "connectivity": task.get("connectivity"),
        "tags": normalize_tags(task.get("tags")),
    }


# =========================================================================
# EKS SERIALIZERS
# =========================================================================


def serialize_eks_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 EKS cluster dict.

    Args:
        cluster: Raw boto3 describe_cluster response item.

    Returns:
        Normalized EKS cluster data.
    """
    k8s_network = cluster.get("kubernetesNetworkConfig") or {}
    vpc_config = cluster.get("resourcesVpcConfig") or {}

    return {
        "name": cluster.get("name", ""),
        "arn": cluster.get("arn", ""),
        "status": cluster.get("status", ""),
        "version": cluster.get("version", ""),
        "endpoint": cluster.get("endpoint", ""),
        "role_arn": cluster.get("roleArn", ""),
        "platform_version": cluster.get("platformVersion", ""),
        "kubernetes_network_config": {
            "service_ipv4_cidr": k8s_network.get("serviceIpv4Cidr", ""),
            "ip_family": k8s_network.get("ipFamily", ""),
        },
        "vpc_config": {
            "vpc_id": vpc_config.get("vpcId", ""),
            "subnet_ids": vpc_config.get("subnetIds", []),
            "security_group_ids": vpc_config.get("securityGroupIds", []),
            "endpoint_public_access": vpc_config.get("endpointPublicAccess", False),
            "endpoint_private_access": vpc_config.get("endpointPrivateAccess", False),
        },
        "created_at": format_aws_timestamp(cluster.get("createdAt")),
        "tags": cluster.get("tags") or {},
    }


def serialize_eks_node_group(ng: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 EKS node group dict.

    Args:
        ng: Raw boto3 describe_nodegroup response item.

    Returns:
        Normalized EKS node group data.
    """
    scaling = ng.get("scalingConfig") or {}
    health = ng.get("health") or {}

    return {
        "node_group_name": ng.get("nodegroupName", ""),
        "node_group_arn": ng.get("nodegroupArn", ""),
        "cluster_name": ng.get("clusterName", ""),
        "status": ng.get("status", ""),
        "capacity_type": ng.get("capacityType", ""),
        "instance_types": ng.get("instanceTypes", []),
        "scaling_config": {
            "min_size": scaling.get("minSize", 0),
            "max_size": scaling.get("maxSize", 0),
            "desired_size": scaling.get("desiredSize", 0),
        },
        "disk_size": ng.get("diskSize"),
        "ami_type": ng.get("amiType", ""),
        "subnets": ng.get("subnets", []),
        "health": {
            "issues": [
                {"code": issue.get("code", ""), "message": issue.get("message", "")}
                for issue in health.get("issues", [])
            ],
        },
        "labels": ng.get("labels") or {},
        "tags": ng.get("tags") or {},
    }


# =========================================================================
# S3 SERIALIZERS
# =========================================================================


def serialize_s3_bucket(
    bucket: dict[str, Any], public_access: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Serialize a boto3 S3 bucket dict.

    Args:
        bucket: Raw boto3 list_buckets response item.
        public_access: Optional public access block configuration.

    Returns:
        Normalized S3 bucket data.
    """
    if public_access is None:
        public_access_block = {
            "block_public_acls": True,
            "ignore_public_acls": True,
            "block_public_policy": True,
            "restrict_public_buckets": True,
        }
    else:
        public_access_block = {
            "block_public_acls": public_access.get("BlockPublicAcls", True),
            "ignore_public_acls": public_access.get("IgnorePublicAcls", True),
            "block_public_policy": public_access.get("BlockPublicPolicy", True),
            "restrict_public_buckets": public_access.get("RestrictPublicBuckets", True),
        }

    return {
        "name": bucket.get("Name", ""),
        "creation_date": format_aws_timestamp(bucket.get("CreationDate")),
        "public_access_block": public_access_block,
    }


# =========================================================================
# LAMBDA SERIALIZERS
# =========================================================================


def serialize_lambda_function(func: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 Lambda function dict.

    Args:
        func: Raw boto3 get_function / list_functions response item.

    Returns:
        Normalized Lambda function data.
    """
    layers = [
        layer.get("Arn", "")
        for layer in func.get("Layers", [])
    ]

    return {
        "function_name": func.get("FunctionName", ""),
        "function_arn": func.get("FunctionArn", ""),
        "runtime": func.get("Runtime", ""),
        "handler": func.get("Handler", ""),
        "code_size": func.get("CodeSize", 0),
        "description": func.get("Description", ""),
        "timeout": func.get("Timeout"),
        "memory_size": func.get("MemorySize"),
        "last_modified": func.get("LastModified"),
        "state": func.get("State"),
        "state_reason": func.get("StateReason"),
        "package_type": func.get("PackageType", ""),
        "architectures": func.get("Architectures", []),
        "layers": layers,
        "tags": func.get("Tags") or {},
    }


# =========================================================================
# RDS SERIALIZERS
# =========================================================================


def serialize_rds_instance(instance: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 RDS instance dict.

    Args:
        instance: Raw boto3 describe_db_instances response item.

    Returns:
        Normalized RDS instance data.
    """
    endpoint = instance.get("Endpoint") or {}
    db_subnet_group = instance.get("DBSubnetGroup") or {}

    tag_list = instance.get("TagList", [])

    return {
        "db_instance_identifier": instance.get("DBInstanceIdentifier", ""),
        "db_instance_arn": instance.get("DBInstanceArn", ""),
        "engine": instance.get("Engine", ""),
        "engine_version": instance.get("EngineVersion", ""),
        "db_instance_class": instance.get("DBInstanceClass", ""),
        "db_instance_status": instance.get("DBInstanceStatus", ""),
        "endpoint": {
            "address": endpoint.get("Address", ""),
            "port": endpoint.get("Port"),
        },
        "multi_az": instance.get("MultiAZ", False),
        "availability_zone": instance.get("AvailabilityZone", ""),
        "allocated_storage": instance.get("AllocatedStorage", 0),
        "storage_type": instance.get("StorageType", ""),
        "storage_encrypted": instance.get("StorageEncrypted", False),
        "publicly_accessible": instance.get("PubliclyAccessible", False),
        "vpc_id": db_subnet_group.get("VpcId", ""),
        "tags": normalize_tags(tag_list),
    }


# =========================================================================
# VPC / NETWORKING SERIALIZERS
# =========================================================================


def serialize_vpc(vpc: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 VPC dict.

    Args:
        vpc: Raw boto3 describe_vpcs response item.

    Returns:
        Normalized VPC data.
    """
    cidr_associations = []
    for assoc in vpc.get("CidrBlockAssociationSet", []):
        cidr_associations.append({
            "cidr_block": assoc.get("CidrBlock", ""),
            "state": (assoc.get("CidrBlockState") or {}).get("State", ""),
            "association_id": assoc.get("AssociationId", ""),
        })

    return {
        "vpc_id": vpc.get("VpcId", ""),
        "state": vpc.get("State", ""),
        "cidr_block": vpc.get("CidrBlock", ""),
        "is_default": vpc.get("IsDefault", False),
        "dhcp_options_id": vpc.get("DhcpOptionsId", ""),
        "instance_tenancy": vpc.get("InstanceTenancy", ""),
        "tags": normalize_tags(vpc.get("Tags")),
        "cidr_block_associations": cidr_associations,
    }


def serialize_subnet(subnet: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a boto3 subnet dict.

    Args:
        subnet: Raw boto3 describe_subnets response item.

    Returns:
        Normalized subnet data.
    """
    return {
        "subnet_id": subnet.get("SubnetId", ""),
        "vpc_id": subnet.get("VpcId", ""),
        "availability_zone": subnet.get("AvailabilityZone", ""),
        "cidr_block": subnet.get("CidrBlock", ""),
        "available_ip_count": subnet.get("AvailableIpAddressCount", 0),
        "map_public_ip_on_launch": subnet.get("MapPublicIpOnLaunch", False),
        "state": subnet.get("State", ""),
        "tags": normalize_tags(subnet.get("Tags")),
    }
