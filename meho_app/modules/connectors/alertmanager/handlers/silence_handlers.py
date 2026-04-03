# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Alertmanager Silence Handler Mixin.

Handles silence listing, creation (with duration parsing), expiration,
and convenience silence-from-alert (auto-builds matchers from alert labels).

WRITE operations: create_silence, silence_alert, expire_silence.
"""

from datetime import UTC, datetime
from typing import Any

from meho_app.modules.connectors.alertmanager.serializers import (
    _parse_duration_string,
    serialize_silences,
)


class SilenceHandlerMixin:
    """Mixin for Alertmanager silence CRUD handlers."""

    # These will be provided by AlertmanagerConnector (base class)
    async def _api_get(self, path: str, params: dict | None = None) -> Any: ...

    async def _api_post(self, path: str, json_body: Any) -> Any: ...

    async def _api_delete(self, path: str) -> None: ...

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_silences_handler(self, params: dict[str, Any]) -> dict:
        """
        List all silences with state summary.

        Returns summary header (active/pending/expired counts) and
        compact table of silences.
        """
        query_params: dict[str, Any] = {}
        filter_expr = params.get("filter")
        if filter_expr:
            query_params["filter"] = filter_expr

        silences = await self._api_get("/api/v2/silences", params=query_params or None)
        return serialize_silences(silences)

    async def _create_silence_handler(self, params: dict[str, Any]) -> dict:
        """
        Create a silence with explicit matchers and duration.

        Computes startsAt/endsAt from duration string (default "2h").
        Explicit starts_at/ends_at override duration-based calculation.
        Sets createdBy to "MEHO (operator: {username})" format.
        """
        matchers = params["matchers"]
        comment = params["comment"]
        created_by = params.get("created_by", "MEHO (operator: system)")

        # Determine start and end times
        now = datetime.now(UTC)

        starts_at = params.get("starts_at")
        ends_at = params.get("ends_at")

        if starts_at and ends_at:
            # Explicit timestamps override
            starts_at_str = starts_at
            ends_at_str = ends_at
        else:
            # Duration-based calculation
            duration_str = params.get("duration", "2h")
            try:
                duration = _parse_duration_string(duration_str)
            except ValueError:
                return {
                    "error": f"Invalid duration format: '{duration_str}'",
                    "hint": "Use formats like '30m', '2h', '1d', '4h30m'",
                }

            start_dt = (
                datetime.fromisoformat(starts_at.replace("Z", "+00:00")) if starts_at else now
            )
            end_dt = start_dt + duration

            starts_at_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            ends_at_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Build silence body for Alertmanager v2 API
        silence_body = {
            "matchers": matchers,
            "startsAt": starts_at_str,
            "endsAt": ends_at_str,
            "createdBy": created_by,
            "comment": comment,
        }

        result = await self._api_post("/api/v2/silences", silence_body)

        silence_id = result.get("silenceID", result.get("id", "unknown"))
        return {
            "success": True,
            "silence_id": silence_id,
            "starts_at": starts_at_str,
            "ends_at": ends_at_str,
            "created_by": created_by,
            "matchers": matchers,
            "comment": comment,
        }

    async def _silence_alert_handler(self, params: dict[str, Any]) -> dict:
        """
        Silence a specific alert by fingerprint (convenience).

        Fetches alerts, finds by fingerprint, extracts labels,
        builds matchers from labels, delegates to _create_silence_handler.
        """
        alert_fingerprint = params["alert_fingerprint"]
        duration = params.get("duration", "2h")
        comment = params["comment"]

        # Fetch alerts and find by fingerprint
        alerts = await self._api_get("/api/v2/alerts")

        target_alert = None
        for alert in alerts:
            if alert.get("fingerprint") == alert_fingerprint:
                target_alert = alert
                break

        if not target_alert:
            return {
                "error": f"Alert with fingerprint '{alert_fingerprint}' not found",
                "hint": "The alert may have resolved. Use list_alerts to see current alerts.",
            }

        # Build matchers from alert labels
        labels = target_alert.get("labels", {})
        matchers = []
        for name, value in labels.items():
            matchers.append(
                {
                    "name": name,
                    "value": str(value),
                    "isRegex": False,
                    "isEqual": True,
                }
            )

        # Delegate to create_silence with constructed matchers
        create_params = {
            "matchers": matchers,
            "duration": duration,
            "comment": comment,
        }

        result = await self._create_silence_handler(create_params)

        # Enrich result with alert context
        if result.get("success"):
            result["alert_fingerprint"] = alert_fingerprint
            result["alert_name"] = labels.get("alertname", "unknown")

        return result

    async def _expire_silence_handler(self, params: dict[str, Any]) -> dict:
        """
        Expire an active silence by ID.

        Calls DELETE /api/v2/silence/{silenceID}.
        """
        silence_id = params["silence_id"]

        try:
            await self._api_delete(f"/api/v2/silence/{silence_id}")
            return {
                "success": True,
                "silence_id": silence_id,
                "message": f"Silence {silence_id} expired successfully",
            }
        except Exception as e:
            return {
                "error": f"Failed to expire silence {silence_id}: {e!s}",
                "hint": "Verify the silence ID is correct and the silence is still active.",
            }
