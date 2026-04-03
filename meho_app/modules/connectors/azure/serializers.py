# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Object Serializers (Phase 92).

Convert Azure SDK model objects to JSON-serializable dictionaries.
Each serializer flattens to 2-level depth max (matching GCP serializer depth).
"""

from typing import Any

from meho_app.modules.connectors.azure.helpers import (
    _extract_os_type,
    _extract_resource_group,
    _format_azure_timestamp,
    _safe_list,
    _safe_tags,
)


def serialize_azure_vm(vm: Any) -> dict[str, Any]:
    """Serialize an Azure Virtual Machine to a dictionary.

    Args:
        vm: azure.mgmt.compute.models.VirtualMachine

    Returns:
        Serialized VM data.
    """
    zones = _safe_list(getattr(vm, "zones", None))
    return {
        "id": vm.id,
        "name": vm.name,
        "location": vm.location,
        "resource_group": _extract_resource_group(vm.id or ""),
        "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
        "provisioning_state": vm.provisioning_state,
        "os_type": _extract_os_type(vm),
        "os_disk_name": (
            vm.storage_profile.os_disk.name
            if vm.storage_profile and vm.storage_profile.os_disk
            else None
        ),
        "data_disk_count": (
            len(vm.storage_profile.data_disks)
            if vm.storage_profile and vm.storage_profile.data_disks
            else 0
        ),
        "tags": _safe_tags(vm.tags),
        "zones": zones,
    }


def serialize_azure_vm_instance_view(iv: Any) -> dict[str, Any]:
    """Serialize an Azure VM Instance View to a dictionary.

    Args:
        iv: azure.mgmt.compute.models.VirtualMachineInstanceView

    Returns:
        Serialized instance view data.
    """
    # Extract power state from statuses
    power_state = None
    for status in _safe_list(getattr(iv, "statuses", None)):
        code = getattr(status, "code", "") or ""
        if code.startswith("PowerState/"):
            power_state = code.split("/", 1)[1]
            break

    vm_agent_status = None
    if iv.vm_agent and iv.vm_agent.vm_agent_version:
        vm_agent_status = iv.vm_agent.vm_agent_version

    os_name = getattr(iv, "os_name", None)
    os_version = getattr(iv, "os_version", None)

    maintenance_state = None
    if hasattr(iv, "maintenance_redeploy_status") and iv.maintenance_redeploy_status:
        maintenance_state = getattr(
            iv.maintenance_redeploy_status, "is_customer_initiated_maintenance_allowed", None
        )

    boot_diag_status = None
    if hasattr(iv, "boot_diagnostics") and iv.boot_diagnostics:
        boot_diag_status = getattr(iv.boot_diagnostics, "status", None)
        if boot_diag_status and hasattr(boot_diag_status, "code"):
            boot_diag_status = boot_diag_status.code

    return {
        "vm_agent_status": vm_agent_status,
        "os_name": os_name,
        "os_version": os_version,
        "power_state": power_state,
        "maintenance_state": maintenance_state,
        "boot_diagnostics_status": boot_diag_status,
    }


def serialize_azure_disk(disk: Any) -> dict[str, Any]:
    """Serialize an Azure Managed Disk to a dictionary.

    Args:
        disk: azure.mgmt.compute.models.Disk

    Returns:
        Serialized disk data.
    """
    os_type = None
    if disk.os_type is not None:
        os_type = str(disk.os_type.value) if hasattr(disk.os_type, "value") else str(disk.os_type)

    return {
        "id": disk.id,
        "name": disk.name,
        "location": disk.location,
        "resource_group": _extract_resource_group(disk.id or ""),
        "size_gb": disk.disk_size_gb,
        "sku_name": disk.sku.name if disk.sku else None,
        "provisioning_state": disk.provisioning_state,
        "disk_state": getattr(disk, "disk_state", None),
        "os_type": os_type,
        "time_created": _format_azure_timestamp(getattr(disk, "time_created", None)),
        "tags": _safe_tags(disk.tags),
    }


def serialize_azure_aks_cluster(cluster: Any) -> dict[str, Any]:
    """Serialize an Azure AKS Cluster to a dictionary.

    Args:
        cluster: azure.mgmt.containerservice.models.ManagedCluster

    Returns:
        Serialized AKS cluster data.
    """
    # Extract network profile fields
    network_plugin = None
    network_policy = None
    service_cidr = None
    if cluster.network_profile:
        network_plugin = getattr(cluster.network_profile, "network_plugin", None)
        if network_plugin and hasattr(network_plugin, "value"):
            network_plugin = network_plugin.value
        network_policy = getattr(cluster.network_profile, "network_policy", None)
        if network_policy and hasattr(network_policy, "value"):
            network_policy = network_policy.value
        service_cidr = getattr(cluster.network_profile, "service_cidr", None)

    # Power state
    power_state = None
    if hasattr(cluster, "power_state") and cluster.power_state:
        code = getattr(cluster.power_state, "code", None)
        if code and hasattr(code, "value"):
            power_state = code.value
        elif code:
            power_state = str(code)

    agent_pools = _safe_list(getattr(cluster, "agent_pool_profiles", None))

    return {
        "id": cluster.id,
        "name": cluster.name,
        "location": cluster.location,
        "resource_group": _extract_resource_group(cluster.id or ""),
        "kubernetes_version": cluster.kubernetes_version,
        "provisioning_state": cluster.provisioning_state,
        "power_state": power_state,
        "fqdn": cluster.fqdn,
        "dns_prefix": getattr(cluster, "dns_prefix", None),
        "node_resource_group": getattr(cluster, "node_resource_group", None),
        "network_plugin": network_plugin,
        "network_policy": network_policy,
        "service_cidr": service_cidr,
        "agent_pool_count": len(agent_pools),
        "tags": _safe_tags(cluster.tags),
    }


def serialize_azure_node_pool(pool: Any) -> dict[str, Any]:
    """Serialize an Azure AKS Node Pool (AgentPoolProfile) to a dictionary.

    Args:
        pool: azure.mgmt.containerservice.models.ManagedClusterAgentPoolProfile
              or azure.mgmt.containerservice.models.AgentPool

    Returns:
        Serialized node pool data.
    """
    # Power state
    power_state = None
    if hasattr(pool, "power_state") and pool.power_state:
        code = getattr(pool.power_state, "code", None)
        if code and hasattr(code, "value"):
            power_state = code.value
        elif code:
            power_state = str(code)

    # Mode
    mode = getattr(pool, "mode", None)
    if mode and hasattr(mode, "value"):
        mode = mode.value

    # OS type
    os_type = getattr(pool, "os_type", None)
    if os_type and hasattr(os_type, "value"):
        os_type = os_type.value

    return {
        "name": pool.name,
        "vm_size": getattr(pool, "vm_size", None),
        "count": getattr(pool, "count", None),
        "os_type": os_type,
        "os_disk_size_gb": getattr(pool, "os_disk_size_gb", None),
        "provisioning_state": getattr(pool, "provisioning_state", None),
        "power_state": power_state,
        "mode": mode,
        "min_count": getattr(pool, "min_count", None),
        "max_count": getattr(pool, "max_count", None),
        "enable_auto_scaling": getattr(pool, "enable_auto_scaling", None),
        "kubernetes_version": getattr(pool, "orchestrator_version", None),
        "availability_zones": _safe_list(getattr(pool, "availability_zones", None)),
    }


def serialize_azure_vnet(vnet: Any) -> dict[str, Any]:
    """Serialize an Azure Virtual Network to a dictionary.

    Args:
        vnet: azure.mgmt.network.models.VirtualNetwork

    Returns:
        Serialized VNet data.
    """
    address_prefixes = []
    if vnet.address_space and vnet.address_space.address_prefixes:
        address_prefixes = list(vnet.address_space.address_prefixes)

    dns_servers = []
    if vnet.dhcp_options and vnet.dhcp_options.dns_servers:
        dns_servers = list(vnet.dhcp_options.dns_servers)

    subnets = _safe_list(getattr(vnet, "subnets", None))

    return {
        "id": vnet.id,
        "name": vnet.name,
        "location": vnet.location,
        "resource_group": _extract_resource_group(vnet.id or ""),
        "address_prefixes": address_prefixes,
        "dns_servers": dns_servers,
        "subnet_count": len(subnets),
        "provisioning_state": vnet.provisioning_state,
        "tags": _safe_tags(vnet.tags),
    }


def serialize_azure_subnet(subnet: Any) -> dict[str, Any]:
    """Serialize an Azure Subnet to a dictionary.

    Args:
        subnet: azure.mgmt.network.models.Subnet

    Returns:
        Serialized subnet data.
    """
    service_endpoints = []
    for ep in _safe_list(getattr(subnet, "service_endpoints", None)):
        service_endpoints.append(getattr(ep, "service", None))

    return {
        "id": subnet.id,
        "name": subnet.name,
        "address_prefix": subnet.address_prefix,
        "provisioning_state": subnet.provisioning_state,
        "nsg_id": subnet.network_security_group.id if subnet.network_security_group else None,
        "route_table_id": subnet.route_table.id if subnet.route_table else None,
        "service_endpoints": service_endpoints,
    }


def _serialize_nsg_rule(rule: Any) -> dict[str, Any]:
    """Flatten an NSG security rule to a dictionary.

    Args:
        rule: azure.mgmt.network.models.SecurityRule

    Returns:
        Flattened rule dict.
    """
    return {
        "name": rule.name,
        "priority": rule.priority,
        "direction": str(rule.direction.value)
        if hasattr(rule.direction, "value")
        else str(rule.direction),
        "access": str(rule.access.value) if hasattr(rule.access, "value") else str(rule.access),
        "protocol": str(rule.protocol.value)
        if hasattr(rule.protocol, "value")
        else str(rule.protocol),
        "source_address_prefix": getattr(rule, "source_address_prefix", None),
        "destination_address_prefix": getattr(rule, "destination_address_prefix", None),
        "destination_port_range": getattr(rule, "destination_port_range", None),
    }


def serialize_azure_nsg(nsg: Any) -> dict[str, Any]:
    """Serialize an Azure Network Security Group to a dictionary.

    Args:
        nsg: azure.mgmt.network.models.NetworkSecurityGroup

    Returns:
        Serialized NSG data.
    """
    security_rules = [
        _serialize_nsg_rule(r) for r in _safe_list(getattr(nsg, "security_rules", None))
    ]
    default_rules = [
        _serialize_nsg_rule(r) for r in _safe_list(getattr(nsg, "default_security_rules", None))
    ]

    return {
        "id": nsg.id,
        "name": nsg.name,
        "location": nsg.location,
        "resource_group": _extract_resource_group(nsg.id or ""),
        "security_rules": security_rules,
        "default_security_rules": default_rules,
        "provisioning_state": nsg.provisioning_state,
        "tags": _safe_tags(nsg.tags),
    }


def serialize_azure_load_balancer(lb: Any) -> dict[str, Any]:
    """Serialize an Azure Load Balancer to a dictionary.

    Args:
        lb: azure.mgmt.network.models.LoadBalancer

    Returns:
        Serialized load balancer data.
    """
    return {
        "id": lb.id,
        "name": lb.name,
        "location": lb.location,
        "resource_group": _extract_resource_group(lb.id or ""),
        "sku_name": lb.sku.name if lb.sku else None,
        "frontend_ip_count": len(_safe_list(getattr(lb, "frontend_ip_configurations", None))),
        "backend_pool_count": len(_safe_list(getattr(lb, "backend_address_pools", None))),
        "inbound_rule_count": len(_safe_list(getattr(lb, "inbound_nat_rules", None))),
        "provisioning_state": lb.provisioning_state,
        "tags": _safe_tags(lb.tags),
    }


def serialize_azure_storage_account(account: Any) -> dict[str, Any]:
    """Serialize an Azure Storage Account to a dictionary.

    Args:
        account: azure.mgmt.storage.models.StorageAccount

    Returns:
        Serialized storage account data.
    """
    # Extract primary endpoints
    primary_endpoints = {}
    if account.primary_endpoints:
        primary_endpoints = {
            "blob": getattr(account.primary_endpoints, "blob", None),
            "file": getattr(account.primary_endpoints, "file", None),
            "queue": getattr(account.primary_endpoints, "queue", None),
            "table": getattr(account.primary_endpoints, "table", None),
        }

    # Kind
    kind = getattr(account, "kind", None)
    if kind and hasattr(kind, "value"):
        kind = kind.value

    # Access tier
    access_tier = getattr(account, "access_tier", None)
    if access_tier and hasattr(access_tier, "value"):
        access_tier = access_tier.value

    return {
        "id": account.id,
        "name": account.name,
        "location": account.location,
        "resource_group": _extract_resource_group(account.id or ""),
        "sku_name": account.sku.name if account.sku else None,
        "kind": kind,
        "provisioning_state": getattr(account, "provisioning_state", None),
        "access_tier": access_tier,
        "is_hns_enabled": getattr(account, "is_hns_enabled", None),
        "creation_time": _format_azure_timestamp(getattr(account, "creation_time", None)),
        "primary_endpoints": primary_endpoints,
        "tags": _safe_tags(account.tags),
    }


def serialize_azure_web_app(site: Any) -> dict[str, Any]:
    """Serialize an Azure Web App (App Service) to a dictionary.

    Args:
        site: azure.mgmt.web.models.Site

    Returns:
        Serialized web app data.
    """
    return {
        "id": site.id,
        "name": site.name,
        "location": site.location,
        "resource_group": _extract_resource_group(site.id or ""),
        "kind": getattr(site, "kind", None),
        "state": getattr(site, "state", None),
        "default_host_name": getattr(site, "default_host_name", None),
        "https_only": getattr(site, "https_only", None),
        "app_service_plan_id": getattr(site, "server_farm_id", None),
        "runtime_stack": _extract_runtime_stack(site),
        "provisioning_state": getattr(site, "provisioning_state", None),
        "tags": _safe_tags(site.tags),
    }


def serialize_azure_function_app(site: Any) -> dict[str, Any]:
    """Serialize an Azure Function App to a dictionary.

    Same structure as web app, validates that the site is a function app.

    Args:
        site: azure.mgmt.web.models.Site (with kind containing "functionapp").

    Returns:
        Serialized function app data.
    """
    return {
        "id": site.id,
        "name": site.name,
        "location": site.location,
        "resource_group": _extract_resource_group(site.id or ""),
        "kind": getattr(site, "kind", None),
        "state": getattr(site, "state", None),
        "default_host_name": getattr(site, "default_host_name", None),
        "https_only": getattr(site, "https_only", None),
        "app_service_plan_id": getattr(site, "server_farm_id", None),
        "runtime_stack": _extract_runtime_stack(site),
        "provisioning_state": getattr(site, "provisioning_state", None),
        "tags": _safe_tags(site.tags),
    }


def serialize_azure_resource_group(rg: Any) -> dict[str, Any]:
    """Serialize an Azure Resource Group to a dictionary.

    Args:
        rg: azure.mgmt.resource.models.ResourceGroup

    Returns:
        Serialized resource group data.
    """
    return {
        "id": rg.id,
        "name": rg.name,
        "location": rg.location,
        "provisioning_state": (
            getattr(rg, "properties", None) and getattr(rg.properties, "provisioning_state", None)
        )
        or getattr(rg, "provisioning_state", None),
        "tags": _safe_tags(rg.tags),
    }


def _extract_runtime_stack(site: Any) -> str | None:
    """Extract runtime stack from site config if available.

    Args:
        site: Azure Site SDK object.

    Returns:
        Runtime stack string or None.
    """
    site_config = getattr(site, "site_config", None)
    if not site_config:
        return None
    # Try linux_fx_version first (e.g. "PYTHON|3.11")
    linux_fx = getattr(site_config, "linux_fx_version", None)
    if linux_fx:
        return str(linux_fx)
    # Fall back to windows stack
    net_version = getattr(site_config, "net_framework_version", None)
    if net_version:
        return f".NET {net_version}"
    return None
