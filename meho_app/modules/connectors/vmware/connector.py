# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware vSphere Connector using pyvmomi (TASK-97)

Implements the BaseConnector interface using mixin pattern for organization.

Uses pyvmomi for native VMware API access instead of SOAP/zeep.
"""

import base64
import ssl
import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)

# Import all 12 handler mixins (8 existing + 4 new from Phase 96)
from meho_app.modules.connectors.vmware.handlers import (
    CapacityHandlerMixin,
    ClusterHandlerMixin,
    HostHandlerMixin,
    InventoryHandlerMixin,
    NetworkHandlerMixin,
    NsxHandlerMixin,
    PerformanceHandlerMixin,
    SddcHandlerMixin,
    StorageHandlerMixin,
    SystemHandlerMixin,
    VMHandlerMixin,
    VsanHandlerMixin,
)
from meho_app.modules.connectors.vmware.rest_client import VMwareRESTClient

# Import helpers
from meho_app.modules.connectors.vmware.helpers import (
    collect_snapshots,
    find_cluster,
    find_datastore,
    find_dvs,
    find_folder,
    find_host,
    find_network,
    find_resource_pool,
    find_snapshot,
    find_vm,
    make_guest_auth,
)
from meho_app.modules.connectors.vmware.operations import VMWARE_OPERATIONS

# Import serializers
from meho_app.modules.connectors.vmware.serializers import (
    serialize_cluster_properties,
    serialize_datacenter_properties,
    serialize_datastore_properties,
    serialize_dvs_properties,
    serialize_folder_properties,
    serialize_host_properties,
    serialize_network_properties,
    serialize_portgroup_properties,
    serialize_resource_pool_properties,
    serialize_vm_properties,
)
from meho_app.modules.connectors.vmware.types import VMWARE_TYPES

logger = get_logger(__name__)


class VMwareConnector(
    BaseConnector,
    VMHandlerMixin,
    HostHandlerMixin,
    ClusterHandlerMixin,
    StorageHandlerMixin,
    NetworkHandlerMixin,
    InventoryHandlerMixin,
    SystemHandlerMixin,
    PerformanceHandlerMixin,
    VsanHandlerMixin,
    CapacityHandlerMixin,
    NsxHandlerMixin,
    SddcHandlerMixin,
):
    """
    VMware vSphere/VCF connector using pyvmomi + REST APIs.

    Provides native access to vCenter Server, NSX Manager, and SDDC Manager
    for ~210 total operations across 12 handler mixins:

    Organization (12 handler mixins):
    - VM operations: 75 methods in vm_handlers.py
    - Host operations: 35 methods in host_handlers.py
    - Cluster operations: 13 methods in cluster_handlers.py
    - Storage operations: 12 methods in storage_handlers.py
    - Network operations: 12 methods in network_handlers.py
    - Inventory operations: 16 methods in inventory_handlers.py
    - System operations: 7 methods in system_handlers.py
    - Performance operations: 4 methods in performance_handlers.py
    - vSAN operations: 6 methods in vsan_handlers.py (Phase 96)
    - Capacity operations: 4 methods in capacity_handlers.py (Phase 96)
    - NSX operations: 12 methods in nsx_handlers.py (Phase 96)
    - SDDC operations: 8 methods in sddc_handlers.py (Phase 96)

    Connectivity:
    - vCenter: pyvmomi SmartConnect (always)
    - NSX Manager: REST client with Basic Auth (optional, if nsx_host configured)
    - SDDC Manager: REST client with Bearer Token (optional, if sddc_host configured)

    Example:
        connector = VMwareConnector(
            connector_id="abc123",
            config={
                "vcenter_host": "vcenter.example.com",
                "port": 443,
                "disable_ssl_verification": True,
                "nsx_host": "nsx.example.com",  # optional
                "sddc_host": "sddc.example.com",  # optional
            },
            credentials={
                "username": "administrator@vsphere.local",
                "password": "secret",
                "nsx_username": "admin",  # optional
                "nsx_password": "secret",  # optional
                "sddc_username": "admin@local",  # optional
                "sddc_password": "secret",  # optional
            }
        )

        async with connector:
            result = await connector.execute("list_virtual_machines", {})
            print(result.data)
    """

    def __init__(self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]):
        super().__init__(connector_id, config, credentials)
        self._connection: Any = None
        self._content: Any = None
        # NSX REST client (initialized in connect() if nsx_host configured)
        self._nsx_client: VMwareRESTClient | None = None
        # SDDC REST client (initialized in connect() if sddc_host configured)
        self._sddc_client: VMwareRESTClient | None = None
        self._sddc_auth_client: VMwareRESTClient | None = None
        self._sddc_access_token: str | None = None
        self._sddc_refresh_token_id: str | None = None
        self._sddc_token_time: float | None = None
        self._sddc_username: str | None = None
        self._sddc_password: str | None = None

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """Connect to vCenter Server."""
        try:
            # Import pyvmomi here to fail gracefully if not installed
            from pyVim.connect import SmartConnect
            from pyVmomi import vim  # noqa: F401
        except ImportError:
            raise ImportError(
                "pyvmomi is required for VMware connector. Install with: pip install pyvmomi"
            ) from None

        vcenter_host = self.config.get("vcenter_host")
        if not vcenter_host:
            raise ValueError("vcenter_host is required in config")

        username = self.credentials.get("username")
        password = self.credentials.get("password")

        if not username or not password:
            raise ValueError("username and password are required in credentials")

        port = self.config.get("port", 443)

        # SSL context
        ssl_context = None
        if self.config.get("disable_ssl_verification", False):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        logger.info(f"🔌 Connecting to vCenter: {vcenter_host}:{port}")

        try:
            self._connection = SmartConnect(
                host=vcenter_host,
                user=username,
                pwd=password,
                port=port,
                sslContext=ssl_context,
            )
            self._content = self._connection.RetrieveContent()
            self._is_connected = True

            logger.info(f"Connected to vCenter: {vcenter_host}")

            # Initialize NSX REST client if nsx_host configured
            nsx_host = self.config.get("nsx_host")
            if nsx_host:
                nsx_port = self.config.get("nsx_port", 443)
                nsx_user = self.credentials.get("nsx_username")
                nsx_pass = self.credentials.get("nsx_password")
                if nsx_user and nsx_pass:
                    verify_ssl = not self.config.get("disable_ssl_verification", False)
                    self._nsx_client = VMwareRESTClient(
                        base_url=f"https://{nsx_host}:{nsx_port}",
                        verify_ssl=verify_ssl,
                        timeout=30.0,
                    )
                    raw = f"{nsx_user}:{nsx_pass}"
                    encoded = base64.b64encode(raw.encode()).decode()
                    await self._nsx_client.connect(headers={
                        "Authorization": f"Basic {encoded}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    })
                    logger.info(f"Connected to NSX Manager: {nsx_host}")

            # Initialize SDDC Manager REST client if sddc_host configured
            sddc_host = self.config.get("sddc_host")
            if sddc_host:
                sddc_port = self.config.get("sddc_port", 443)
                self._sddc_username = self.credentials.get("sddc_username")
                self._sddc_password = self.credentials.get("sddc_password")
                if self._sddc_username and self._sddc_password:
                    verify_ssl = not self.config.get("disable_ssl_verification", False)
                    sddc_base_url = f"https://{sddc_host}:{sddc_port}"
                    # Auth client (no auth headers, used only for /v1/tokens)
                    self._sddc_auth_client = VMwareRESTClient(
                        base_url=sddc_base_url, verify_ssl=verify_ssl, timeout=30.0,
                    )
                    await self._sddc_auth_client.connect(
                        headers={"Accept": "application/json", "Content-Type": "application/json"}
                    )
                    # Main client (will get Bearer token on first use via _ensure_sddc_token)
                    self._sddc_client = VMwareRESTClient(
                        base_url=sddc_base_url, verify_ssl=verify_ssl, timeout=30.0,
                    )
                    # Pre-authenticate to validate credentials
                    await self._ensure_sddc_token()
                    logger.info(f"Connected to SDDC Manager: {sddc_host}")

            return True

        except Exception as e:
            logger.error(f"vCenter connection failed: {e}")
            self._is_connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from vCenter, NSX Manager, and SDDC Manager."""
        if self._connection:
            try:
                from pyVim.connect import Disconnect

                Disconnect(self._connection)
                logger.info("Disconnected from vCenter")
            except Exception as e:
                logger.warning(f"Error disconnecting from vCenter: {e}")
            finally:
                self._connection = None
                self._content = None
                self._is_connected = False

        # Tear down NSX REST client
        if self._nsx_client:
            await self._nsx_client.disconnect()
            self._nsx_client = None
            logger.info("Disconnected from NSX Manager")

        # Tear down SDDC REST clients
        if self._sddc_client:
            await self._sddc_client.disconnect()
            self._sddc_client = None
        if self._sddc_auth_client:
            await self._sddc_auth_client.disconnect()
            self._sddc_auth_client = None
        self._sddc_access_token = None
        self._sddc_refresh_token_id = None
        self._sddc_token_time = None
        if self.config.get("sddc_host"):
            logger.info("Disconnected from SDDC Manager")

    async def test_connection(self) -> bool:
        """Test vCenter connection."""
        try:
            if not self._content:
                await self.connect()

            about = self._content.about
            logger.info(f"✅ vCenter test: {about.fullName}")
            return True
        except Exception as e:
            logger.error(f"❌ Connection test failed: {e}")
            return False

    # =========================================================================
    # OPERATION & TYPE DISCOVERY
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get VMware operations for registration."""
        return VMWARE_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get VMware types for registration."""
        return VMWARE_TYPES

    # =========================================================================
    # OPERATION EXECUTION (Routes to handler mixins)
    # =========================================================================

    async def execute(self, operation_id: str, parameters: dict[str, Any]) -> OperationResult:
        """Execute a VMware operation."""

        start_time = time.time()

        # Map operation_id to handler method (from mixins)
        handlers = {
            # VM OPERATIONS (VMHandlerMixin)
            "list_virtual_machines": self._list_vms,
            "get_virtual_machine": self._get_vm,
            "power_on_vm": self._power_on_vm,
            "power_off_vm": self._power_off_vm,
            "shutdown_guest": self._shutdown_guest,
            "create_snapshot": self._create_snapshot,
            "list_snapshots": self._list_snapshots,
            "revert_snapshot": self._revert_snapshot,
            "delete_snapshot": self._delete_snapshot,
            "delete_all_snapshots": self._delete_all_snapshots,
            "reconfigure_vm_cpu": self._reconfigure_vm_cpu,
            "reconfigure_vm_memory": self._reconfigure_vm_memory,
            "get_vm_disks": self._get_vm_disks,
            "get_vm_nics": self._get_vm_nics,
            "rename_vm": self._rename_vm,
            "set_vm_annotation": self._set_vm_annotation,
            "migrate_vm": self._migrate_vm,
            "relocate_vm": self._relocate_vm,
            "clone_vm": self._clone_vm,
            "get_host_vms": self._get_host_vms,
            "get_datastore_vms": self._get_datastore_vms,
            "get_vm_performance": self._get_vm_performance,
            "reboot_guest": self._reboot_guest,
            "standby_guest": self._standby_guest,
            "suspend_vm": self._suspend_vm,
            "reset_vm": self._reset_vm,
            "mark_as_template": self._mark_as_template,
            "mark_as_virtual_machine": self._mark_as_virtual_machine,
            "upgrade_virtual_hardware": self._upgrade_virtual_hardware,
            "consolidate_disks": self._consolidate_disks,
            "defragment_all_disks": self._defragment_all_disks,
            "mount_tools_installer": self._mount_tools_installer,
            "unmount_tools_installer": self._unmount_tools_installer,
            "upgrade_tools": self._upgrade_tools,
            "export_vm": self._export_vm,
            "unregister_vm": self._unregister_vm,
            "destroy_vm": self._destroy_vm,
            "answer_vm_question": self._answer_vm_question,
            "acquire_mks_ticket": self._acquire_mks_ticket,
            "acquire_ticket": self._acquire_ticket,
            "query_changed_disk_areas": self._query_changed_disk_areas,
            "recommend_hosts_for_vm": self._recommend_hosts_for_vm,
            "place_vm": self._place_vm,
            "create_vmfs_datastore": self._create_vmfs_datastore,
            "expand_vmfs_datastore": self._expand_vmfs_datastore,
            "create_vm": self._create_vm,
            "deploy_ovf": self._deploy_ovf,
            "get_vm_guest_info": self._get_vm_guest_info,
            "list_guest_processes": self._list_guest_processes,
            "run_program_in_guest": self._run_program_in_guest,
            "upload_file_to_guest": self._upload_file_to_guest,
            "download_file_from_guest": self._download_file_from_guest,
            "create_directory_in_guest": self._create_directory_in_guest,
            "delete_file_in_guest": self._delete_file_in_guest,
            "set_custom_value": self._set_custom_value,
            "get_custom_values": self._get_custom_values,
            "set_screen_resolution": self._set_screen_resolution,
            "get_vm_tags": self._get_vm_tags,
            "assign_tag_to_vm": self._assign_tag_to_vm,
            "remove_tag_from_vm": self._remove_tag_from_vm,
            "revert_to_current_snapshot": self._revert_to_current_snapshot,
            "reset_guest_information": self._reset_guest_information,
            "list_templates": self._list_templates,
            "get_template": self._get_template,
            "attach_disk": self._attach_disk,
            "detach_disk": self._detach_disk,
            "add_disk": self._add_disk,
            "extend_disk": self._extend_disk,
            "customize_guest": self._customize_guest,
            "create_screenshot": self._create_screenshot,
            "set_boot_options": self._set_boot_options,
            "instant_clone": self._instant_clone,
            "register_vm": self._register_vm,
            "reload_vm": self._reload_vm,
            "terminate_fault_tolerance": self._terminate_fault_tolerance,
            "send_nmi": self._send_nmi,
            "acquire_cim_ticket": self._acquire_cim_ticket,
            # CLUSTER OPERATIONS (ClusterHandlerMixin)
            "list_clusters": self._list_clusters,
            "get_cluster": self._get_cluster,
            "get_drs_recommendations": self._get_drs_recommendations,
            "apply_drs_recommendation": self._apply_drs_recommendation,
            "cancel_drs_recommendation": self._cancel_drs_recommendation,
            "refresh_drs_recommendations": self._refresh_drs_recommendations,
            "get_cluster_resource_usage": self._get_cluster_resource_usage,
            "find_rules_for_vm": self._find_rules_for_vm,
            "get_cluster_ha_status": self._get_cluster_ha_status,
            "reconfigure_cluster": self._reconfigure_cluster,
            "destroy_cluster": self._destroy_cluster,
            "rename_cluster": self._rename_cluster,
            "get_evc_mode": self._get_evc_mode,
            "get_cluster_performance": self._get_cluster_performance,
            # HOST OPERATIONS (HostHandlerMixin)
            "list_hosts": self._list_hosts,
            "get_host": self._get_host,
            "enter_maintenance_mode": self._enter_maintenance_mode,
            "exit_maintenance_mode": self._exit_maintenance_mode,
            "get_host_datastores": self._get_host_datastores,
            "get_host_networks": self._get_host_networks,
            "get_host_performance": self._get_host_performance,
            "refresh_storage_info": self._refresh_storage_info,
            "reboot_host": self._reboot_host,
            "shutdown_host": self._shutdown_host,
            "disconnect_host": self._disconnect_host,
            "reconnect_host": self._reconnect_host,
            "enter_lockdown_mode": self._enter_lockdown_mode,
            "exit_lockdown_mode": self._exit_lockdown_mode,
            "enter_standby_mode": self._enter_standby_mode,
            "exit_standby_mode": self._exit_standby_mode,
            "query_host_connection_info": self._query_host_connection_info,
            "retrieve_hardware_uptime": self._retrieve_hardware_uptime,
            "query_memory_overhead": self._query_memory_overhead,
            "update_host_flags": self._update_host_flags,
            "query_tpm_attestation": self._query_tpm_attestation,
            "scan_host_storage": self._scan_host_storage,
            "refresh_host_services": self._refresh_host_services,
            "get_host_services": self._get_host_services,
            "start_host_service": self._start_host_service,
            "stop_host_service": self._stop_host_service,
            "restart_host_service": self._restart_host_service,
            "get_host_firewall_rules": self._get_host_firewall_rules,
            "enable_firewall_ruleset": self._enable_firewall_ruleset,
            "disable_firewall_ruleset": self._disable_firewall_ruleset,
            "add_host_to_cluster": self._add_host_to_cluster,
            "move_host_into_cluster": self._move_host_into_cluster,
            "enter_datastore_maintenance_mode": self._enter_datastore_maintenance_mode,
            "exit_datastore_maintenance_mode": self._exit_datastore_maintenance_mode,
            "cluster_enter_maintenance_mode": self._cluster_enter_maintenance_mode,
            # STORAGE OPERATIONS (StorageHandlerMixin)
            "list_datastores": self._list_datastores,
            "get_datastore": self._get_datastore,
            "browse_datastore": self._browse_datastore,
            "refresh_datastore": self._refresh_datastore,
            "rename_datastore": self._rename_datastore,
            "destroy_datastore": self._destroy_datastore,
            "refresh_datastore_storage_info": self._refresh_datastore_storage_info,
            "create_nfs_datastore": self._create_nfs_datastore,
            "remove_datastore": self._remove_datastore,
            "get_datastore_performance": self._get_datastore_performance,
            "get_storage_pods": self._get_storage_pods,
            "get_storage_pod": self._get_storage_pod,
            # NETWORK OPERATIONS (NetworkHandlerMixin)
            "list_networks": self._list_networks,
            "list_distributed_switches": self._list_distributed_switches,
            "get_distributed_switch": self._get_distributed_switch,
            "list_port_groups": self._list_port_groups,
            "get_port_group": self._get_port_group,
            "create_dvs_portgroup": self._create_dvs_portgroup,
            "destroy_dvs_portgroup": self._destroy_dvs_portgroup,
            "query_used_vlans": self._query_used_vlans,
            "refresh_dvs_port_state": self._refresh_dvs_port_state,
            "add_network_adapter": self._add_network_adapter,
            "remove_network_adapter": self._remove_network_adapter,
            "change_network": self._change_network,
            # INVENTORY OPERATIONS (InventoryHandlerMixin)
            "list_datacenters": self._list_datacenters,
            "list_resource_pools": self._list_resource_pools,
            "list_folders": self._list_folders,
            "create_folder": self._create_folder,
            "rename_folder": self._rename_folder,
            "destroy_folder": self._destroy_folder,
            "move_into_folder": self._move_into_folder,
            "create_resource_pool": self._create_resource_pool,
            "destroy_resource_pool": self._destroy_resource_pool,
            "update_resource_pool": self._update_resource_pool,
            "list_content_libraries": self._list_content_libraries,
            "get_content_library_items": self._get_content_library_items,
            "deploy_library_item": self._deploy_library_item,
            "list_tags": self._list_tags,
            "list_tag_categories": self._list_tag_categories,
            "search_inventory": self._search_inventory,
            "get_inventory_path": self._get_inventory_path,
            # SYSTEM OPERATIONS (SystemHandlerMixin)
            "get_vcenter_info": self._get_vcenter_info,
            "list_tasks": self._list_tasks,
            "list_alarms": self._list_alarms,
            "get_events": self._get_events,
            "acknowledge_alarm": self._acknowledge_alarm,
            "get_license_info": self._get_license_info,
            "get_licensed_features": self._get_licensed_features,
            # DETAILED PERFORMANCE OPERATIONS (PerformanceHandlerMixin)
            # These use PerformanceManager API for disk I/O, network, historical metrics
            # NOTE: Datastores are NOT valid performance providers in vSphere - use get_datastore_performance for capacity info
            "get_detailed_vm_performance": self._get_detailed_vm_performance,
            "get_detailed_host_performance": self._get_detailed_host_performance,
            "get_cluster_detailed_performance": self._get_cluster_detailed_performance,
            "list_available_metrics": self._list_available_metrics,
            # vSAN OPERATIONS (VsanHandlerMixin) - Phase 96
            "get_vsan_cluster_health": self._get_vsan_cluster_health,
            "get_vsan_disk_groups": self._get_vsan_disk_groups,
            "get_vsan_capacity": self._get_vsan_capacity,
            "get_vsan_resync_status": self._get_vsan_resync_status,
            "get_vsan_storage_policies": self._get_vsan_storage_policies,
            "get_vsan_objects_health": self._get_vsan_objects_health,
            # CAPACITY OPERATIONS (CapacityHandlerMixin) - Phase 96
            "get_cluster_capacity": self._get_cluster_capacity,
            "get_cluster_overcommitment": self._get_cluster_overcommitment,
            "get_datastore_utilization": self._get_datastore_utilization,
            "get_host_load_distribution": self._get_host_load_distribution,
            # NSX OPERATIONS (NsxHandlerMixin) - Phase 96
            "list_nsx_segments": self._list_nsx_segments,
            "get_nsx_segment": self._get_nsx_segment,
            "list_nsx_firewall_policies": self._list_nsx_firewall_policies,
            "get_nsx_firewall_rule": self._get_nsx_firewall_rule,
            "list_nsx_security_groups": self._list_nsx_security_groups,
            "list_nsx_tier0_gateways": self._list_nsx_tier0_gateways,
            "list_nsx_tier1_gateways": self._list_nsx_tier1_gateways,
            "list_nsx_load_balancers": self._list_nsx_load_balancers,
            "list_nsx_transport_zones": self._list_nsx_transport_zones,
            "list_nsx_transport_nodes": self._list_nsx_transport_nodes,
            "get_nsx_transport_node": self._get_nsx_transport_node,
            "search_nsx": self._search_nsx,
            # SDDC MANAGER OPERATIONS (SddcHandlerMixin) - Phase 96
            "get_sddc_system_info": self._get_sddc_system_info,
            "list_sddc_workload_domains": self._list_sddc_workload_domains,
            "get_sddc_workload_domain": self._get_sddc_workload_domain,
            "list_sddc_hosts": self._list_sddc_hosts,
            "list_sddc_clusters": self._list_sddc_clusters,
            "get_sddc_update_history": self._get_sddc_update_history,
            "get_sddc_certificates": self._get_sddc_certificates,
            "get_sddc_prechecks": self._get_sddc_prechecks,
        }

        handler = handlers.get(operation_id)
        if not handler:
            return OperationResult(
                success=False,
                error=f"Unknown operation: {operation_id}",
                operation_id=operation_id,
            )

        try:
            data = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"✅ {operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=data,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"❌ {operation_id} failed: {e}", exc_info=True)

            return OperationResult(
                success=False,
                error=str(e),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    # =========================================================================
    # HELPER METHOD WRAPPERS (Delegate to helpers module)
    # =========================================================================

    def _find_vm(self, name: str) -> Any:
        """Find VM by name."""
        return find_vm(self._content, name)

    def _find_cluster(self, name: str) -> Any:
        """Find cluster by name."""
        return find_cluster(self._content, name)

    def _find_host(self, name: str) -> Any:
        """Find host by name."""
        return find_host(self._content, name)

    def _find_datastore(self, name: str) -> Any:
        """Find datastore by name."""
        return find_datastore(self._content, name)

    def _find_folder(self, name: str) -> Any:
        """Find folder by name."""
        return find_folder(self._content, name)

    def _find_snapshot(self, vm: Any, snapshot_name: str) -> Any:
        """Find snapshot by name."""
        return find_snapshot(vm, snapshot_name)

    def _find_resource_pool(self, name: str) -> Any:
        """Find resource pool by name."""
        return find_resource_pool(self._content, name)

    def _find_dvs(self, name: str) -> Any:
        """Find DVS by name."""
        return find_dvs(self._content, name)

    def _find_network(self, name: str) -> Any:
        """Find network by name."""
        return find_network(self._content, name)

    def _make_guest_auth(self, username: str, password: str) -> Any:
        """Create guest auth credentials."""
        return make_guest_auth(username, password)

    def _collect_snapshots(
        self, snapshot_list: list, results: list[dict], parent: str | None = None
    ) -> None:
        """Collect snapshots recursively."""
        return collect_snapshots(snapshot_list, results, parent)

    # =========================================================================
    # SERIALIZER METHOD WRAPPERS (Delegate to serializers module)
    # =========================================================================

    def _serialize_vm_properties(self, vm: Any) -> dict[str, Any]:
        """Serialize VM properties."""
        return serialize_vm_properties(vm)

    def _serialize_host_properties(self, host: Any) -> dict[str, Any]:
        """Serialize host properties."""
        return serialize_host_properties(host)

    def _serialize_datastore_properties(self, ds: Any) -> dict[str, Any]:
        """Serialize datastore properties."""
        return serialize_datastore_properties(ds)

    def _serialize_cluster_properties(self, cluster: Any) -> dict[str, Any]:
        """Serialize cluster properties."""
        return serialize_cluster_properties(cluster)

    def _serialize_network_properties(self, network: Any) -> dict[str, Any]:
        """Serialize network properties."""
        return serialize_network_properties(network)

    def _serialize_resource_pool_properties(self, pool: Any) -> dict[str, Any]:
        """Serialize resource pool properties."""
        return serialize_resource_pool_properties(pool)

    def _serialize_folder_properties(self, folder: Any) -> dict[str, Any]:
        """Serialize folder properties."""
        return serialize_folder_properties(folder)

    def _serialize_datacenter_properties(self, dc: Any) -> dict[str, Any]:
        """Serialize datacenter properties."""
        return serialize_datacenter_properties(dc)

    def _serialize_dvs_properties(self, dvs: Any) -> dict[str, Any]:
        """Serialize DVS properties."""
        return serialize_dvs_properties(dvs)

    def _serialize_portgroup_properties(self, pg: Any) -> dict[str, Any]:
        """Serialize portgroup properties."""
        return serialize_portgroup_properties(pg)
