# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
System Operation Handlers

Mixin class containing 7 system operation handlers.
"""

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class SystemHandlerMixin:
    """Mixin for system operation handlers."""

    # These will be provided by VMwareConnector (base class)
    _content: Any

    # Helper methods (will be provided by VMwareConnector) - stubs for type checking
    def _find_vm(self, name: str) -> Any | None:
        return None

    def _find_host(self, name: str) -> Any | None:
        return None

    def _find_cluster(self, name: str) -> Any | None:
        return None

    def _find_datastore(self, name: str) -> Any | None:
        return None

    async def _get_vcenter_info(self, params: dict[str, Any]) -> dict:
        """Get vCenter Server information."""
        about = self._content.about
        return {
            "name": about.name,
            "full_name": about.fullName,
            "version": about.version,
            "build": about.build,
            "api_version": about.apiVersion,
            "instance_uuid": about.instanceUuid,
        }

    async def _list_tasks(self, params: dict[str, Any]) -> list[dict]:
        """List recent tasks."""
        limit = params.get("limit", 20)

        task_manager = self._content.taskManager
        tasks = task_manager.recentTask or []

        results = []
        for task in tasks[:limit]:
            try:
                info = task.info
                results.append(
                    {
                        "key": str(task._moId),
                        "name": info.name if info else None,
                        "state": str(info.state) if info else None,
                        "progress": info.progress if info else None,
                        "start_time": str(info.startTime) if info and info.startTime else None,
                    }
                )
            except Exception as e:
                logger.warning(f"Could not access task info for {task._moId}: {e}")

        return results

    async def _list_alarms(self, params: dict[str, Any]) -> list[dict]:
        """List triggered alarms."""

        alarm_manager = self._content.alarmManager
        if not alarm_manager:
            return []

        # Get alarms from root folder (cascades down)
        alarms = []
        try:
            for alarm_state in self._content.rootFolder.triggeredAlarmState or []:
                alarm_info = alarm_state.alarm.info if alarm_state.alarm else None
                alarms.append(
                    {
                        "entity": alarm_state.entity.name if alarm_state.entity else None,
                        "alarm_name": alarm_info.name if alarm_info else None,
                        "status": str(alarm_state.overallStatus)
                        if alarm_state.overallStatus
                        else None,
                        "time": str(alarm_state.time) if alarm_state.time else None,
                    }
                )
        except Exception as e:
            logger.warning(f"Error listing alarms: {e}")

        return alarms

    async def _get_events(self, params: dict[str, Any]) -> list[dict]:
        """Get recent events."""
        from pyVmomi import vim

        limit = params.get("limit", 50)

        event_manager = self._content.eventManager

        # Create filter spec
        filter_spec = vim.event.EventFilterSpec()
        filter_spec.maxCount = limit

        events = event_manager.QueryEvents(filter_spec)

        return [
            {
                "key": event.key,
                "type": type(event).__name__,
                "created_time": str(event.createdTime) if event.createdTime else None,
                "message": event.fullFormattedMessage[:200] if event.fullFormattedMessage else None,
                "user": event.userName,
            }
            for event in (events or [])[:limit]
        ]

    async def _acknowledge_alarm(self, params: dict[str, Any]) -> dict:
        """Acknowledge alarm on entity."""
        entity_name = params.get("entity_name")
        if not entity_name:
            raise ValueError("entity_name is required")

        entity_type = params.get("entity_type", "vm")

        # Find entity
        entity = None
        if entity_type == "vm":
            entity = self._find_vm(entity_name)
        elif entity_type == "host":
            entity = self._find_host(entity_name)
        elif entity_type == "cluster":
            entity = self._find_cluster(entity_name)
        elif entity_type == "datastore":
            entity = self._find_datastore(entity_name)

        if not entity:
            raise ValueError(f"{entity_type} not found: {entity_name}")

        alarm_manager = self._content.alarmManager

        # Iterate triggered alarms on the entity to find active ones
        triggered_alarms = getattr(entity, "triggeredAlarmState", None) or []
        acknowledged_count = 0

        for alarm_state in triggered_alarms:
            try:
                alarm_manager.AcknowledgeAlarm(alarm=alarm_state.alarm, entity=entity)
                acknowledged_count += 1
            except Exception as e:
                logger.warning(
                    f"Failed to acknowledge alarm {getattr(alarm_state.alarm, 'info', {})}: {e}"
                )

        if acknowledged_count == 0:
            return {"message": f"No active alarms on {entity_name}"}

        return {
            "message": f"Acknowledged {acknowledged_count} alarm(s) on {entity_name}",
            "count": acknowledged_count,
        }

    async def _get_license_info(self, params: dict[str, Any]) -> dict:
        """Get license information."""
        lm = self._content.licenseManager
        return {
            "licenses": [
                {
                    "name": lic.name,
                    "license_key": lic.licenseKey[:5] + "..." if lic.licenseKey else None,
                    "total": lic.total,
                    "used": lic.used,
                }
                for lic in lm.licenses or []
            ],
        }

    async def _get_licensed_features(self, params: dict[str, Any]) -> list[str]:
        """Get licensed features."""
        lm = self._content.licenseManager
        features = []
        for lic in lm.licenses or []:
            for prop in lic.properties or []:
                if prop.key == "feature":
                    features.append(prop.value)
        return features
