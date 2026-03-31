# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS EC2 Handlers.

Handlers for EC2 operations: instances and security groups.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.aws.serializers import (
    serialize_ec2_instance,
    serialize_security_group,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.aws.connector import AWSConnector

logger = get_logger(__name__)


class EC2HandlerMixin:
    """Mixin providing EC2 operation handlers."""

    # =========================================================================
    # INSTANCE OPERATIONS
    # =========================================================================

    async def _handle_list_instances(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List EC2 instances with optional tag and state filtering.

        CRITICAL: EC2 describe_instances returns a two-level structure:
        Reservations -> Instances (Pitfall 1).

        Args:
            params: Optional keys: tag_filter ({key, value}), state, region.

        Returns:
            List of serialized EC2 instances.
        """
        client = self._get_client("ec2", params.get("region"))

        # Build filters
        filters: list[dict[str, Any]] = []
        tag_filter = params.get("tag_filter")
        if tag_filter and isinstance(tag_filter, dict):
            key = tag_filter.get("key", "")
            value = tag_filter.get("value", "")
            if key and value:
                filters.append({"Name": f"tag:{key}", "Values": [value]})

        state = params.get("state")
        if state:
            filters.append({"Name": "instance-state-name", "Values": [state]})

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_instances")
            paginate_kwargs: dict[str, Any] = {}
            if filters:
                paginate_kwargs["Filters"] = filters

            results: list[dict[str, Any]] = []
            for page in paginator.paginate(**paginate_kwargs):
                # Two-level nesting: Reservations -> Instances
                for reservation in page.get("Reservations", []):
                    results.extend(reservation.get("Instances", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_ec2_instance(i) for i in raw]

    async def _handle_get_instance(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific EC2 instance.

        Args:
            params: Required keys: instance_id. Optional keys: region.

        Returns:
            Serialized EC2 instance.
        """
        client = self._get_client("ec2", params.get("region"))
        instance_id = params["instance_id"]

        def _get_instance() -> dict[str, Any]:
            response = client.describe_instances(InstanceIds=[instance_id])
            reservations = response.get("Reservations", [])
            if reservations and reservations[0].get("Instances"):
                return reservations[0]["Instances"][0]
            return {}

        raw = await asyncio.to_thread(_get_instance)
        return serialize_ec2_instance(raw)

    # =========================================================================
    # SECURITY GROUP OPERATIONS
    # =========================================================================

    async def _handle_list_security_groups(
        self: "AWSConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        List EC2 security groups, optionally filtered by VPC.

        Args:
            params: Optional keys: vpc_id, region.

        Returns:
            List of serialized security groups.
        """
        client = self._get_client("ec2", params.get("region"))

        filters: list[dict[str, Any]] = []
        vpc_id = params.get("vpc_id")
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})

        def _list_paginated() -> list[dict[str, Any]]:
            paginator = client.get_paginator("describe_security_groups")
            paginate_kwargs: dict[str, Any] = {}
            if filters:
                paginate_kwargs["Filters"] = filters

            results: list[dict[str, Any]] = []
            for page in paginator.paginate(**paginate_kwargs):
                results.extend(page.get("SecurityGroups", []))
            return results

        raw = await asyncio.to_thread(_list_paginated)
        return [serialize_security_group(sg) for sg in raw]

    async def _handle_get_security_group(
        self: "AWSConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get details for a specific security group.

        Args:
            params: Required keys: group_id. Optional keys: region.

        Returns:
            Serialized security group.
        """
        client = self._get_client("ec2", params.get("region"))
        group_id = params["group_id"]

        def _get_sg() -> dict[str, Any]:
            response = client.describe_security_groups(GroupIds=[group_id])
            groups = response.get("SecurityGroups", [])
            return groups[0] if groups else {}

        raw = await asyncio.to_thread(_get_sg)
        return serialize_security_group(raw)
