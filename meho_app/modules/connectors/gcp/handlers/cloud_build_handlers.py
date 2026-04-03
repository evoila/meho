# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Cloud Build Handlers (Phase 49)

Handlers for Cloud Build operations: list builds, get build details,
list triggers, get build logs, cancel build, and retry build.
"""

import asyncio
import itertools
import re
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.serializers import (
    serialize_build_detail,
    serialize_build_summary,
    serialize_build_trigger,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class CloudBuildHandlerMixin:
    """Mixin providing Cloud Build operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _cloud_build_client: Any
        _storage_client: Any
        _credentials: Any
        project_id: str
        default_region: str

    # =========================================================================
    # BUILD OPERATIONS (READ)
    # =========================================================================

    async def _handle_list_builds(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List Cloud Build executions with status, timing, source, and output images."""
        from google.cloud.devtools import cloudbuild_v1

        filter_str = params.get("filter")
        limit = params.get("limit", 25)

        request = cloudbuild_v1.ListBuildsRequest(
            project_id=self.project_id,
            filter=filter_str or "",
            page_size=limit,
        )

        # Cloud Build SDK client -- use _cloud_build_client if available,
        # otherwise create a temporary one (before Plan 03 wiring)
        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        # SDK list_builds returns a pager that lazily fetches pages.
        # Use page_size to limit the first page, then consume with islice.
        pager = await asyncio.to_thread(lambda: client.list_builds(request=request))

        builds = []
        for build in itertools.islice(pager, limit):
            builds.append(serialize_build_summary(build))

        return builds

    async def _handle_get_build(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get detailed information about a specific Cloud Build execution."""
        from google.cloud.devtools import cloudbuild_v1

        build_id = params["build_id"]

        request = cloudbuild_v1.GetBuildRequest(
            project_id=self.project_id,
            id=build_id,
        )

        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        build = await asyncio.to_thread(lambda: client.get_build(request=request))

        return serialize_build_detail(build)

    async def _handle_list_build_triggers(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List automated Cloud Build trigger configurations."""
        from google.cloud.devtools import cloudbuild_v1

        limit = params.get("limit", 50)

        request = cloudbuild_v1.ListBuildTriggersRequest(
            project_id=self.project_id,
            page_size=limit,
        )

        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        pager = await asyncio.to_thread(lambda: client.list_build_triggers(request=request))

        triggers = []
        for trigger in itertools.islice(pager, limit):
            triggers.append(serialize_build_trigger(trigger))

        return triggers

    async def _handle_get_build_logs(  # type: ignore[misc]  # NOSONAR (cognitive complexity)
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Get per-step build logs from Cloud Storage for diagnosing failures.

        Build logs live in Cloud Storage, NOT in the Cloud Build API.
        Log lines are prefixed with step markers like 'Step #N - ' or 'Step #N:'.
        """
        from google.api_core import exceptions as gcp_exceptions
        from google.cloud import (  # type: ignore[attr-defined]  # google-cloud-storage stubs not available
            storage,
        )
        from google.cloud.devtools import cloudbuild_v1

        build_id = params["build_id"]
        step_index = params.get("step_index")
        tail_lines = params.get("tail_lines", 200)

        # First, get the build to find the logs bucket
        request = cloudbuild_v1.GetBuildRequest(
            project_id=self.project_id,
            id=build_id,
        )

        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        build = await asyncio.to_thread(lambda: client.get_build(request=request))

        logs_bucket = getattr(build, "logs_bucket", None)
        log_url = getattr(build, "log_url", None)

        if not logs_bucket:
            return {
                "build_id": build_id,
                "error": "No logs bucket found for this build",
                "log_url": log_url,
                "message": (
                    "Build logs are not available. The build may still be "
                    "queued, or logs may have expired. Check the log URL for "
                    "browser access."
                ),
            }

        # Read the combined log from Cloud Storage
        bucket_name = logs_bucket.replace("gs://", "")
        blob_name = f"log-{build_id}.txt"

        # Use _storage_client if available, otherwise create a temporary one
        storage_client = getattr(self, "_storage_client", None)
        if storage_client is None:
            storage_client = await asyncio.to_thread(
                lambda: storage.Client(credentials=self._credentials)
            )
        assert storage_client is not None  # noqa: S101

        try:
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            log_content = await asyncio.to_thread(blob.download_as_text)
        except gcp_exceptions.NotFound:
            return {
                "build_id": build_id,
                "error": "Build log not found in Cloud Storage",
                "log_url": log_url,
                "message": (
                    "Build log file not found. The build may still be running, "
                    "or logs may have been deleted. Check the log URL for "
                    "browser access."
                ),
            }
        except Exception as e:
            return {
                "build_id": build_id,
                "error": f"Failed to read build logs: {e!s}",
                "log_url": log_url,
                "message": "Could not read build logs from Cloud Storage.",
            }

        # Parse per-step logs
        # Cloud Build log lines are prefixed with: "Step #N - " or "Step #N: "
        step_pattern = re.compile(r"^Step #(\d+)")
        step_logs: dict[int, list[str]] = {}
        current_step: int = -1

        for line in log_content.splitlines():
            match = step_pattern.match(line)
            if match:
                current_step = int(match.group(1))
            if current_step >= 0:
                if current_step not in step_logs:
                    step_logs[current_step] = []
                step_logs[current_step].append(line)

        # If no step markers found, treat entire log as step 0
        if not step_logs:
            step_logs[0] = log_content.splitlines()

        if step_index is not None:
            # Return specific step's logs
            lines = step_logs.get(step_index, [])
            tailed = lines[-tail_lines:] if len(lines) > tail_lines else lines
            return {
                "build_id": build_id,
                "step_index": step_index,
                "log_lines": tailed,
                "line_count": len(lines),
                "tailed": len(lines) > tail_lines,
            }
        else:
            # Return all steps' logs
            steps_result = []
            for idx in sorted(step_logs.keys()):
                lines = step_logs[idx]
                tailed = lines[-tail_lines:] if len(lines) > tail_lines else lines
                steps_result.append(
                    {
                        "step_index": idx,
                        "log_lines": tailed,
                        "line_count": len(lines),
                        "tailed": len(lines) > tail_lines,
                    }
                )

            return {
                "build_id": build_id,
                "steps": steps_result,
                "total_steps": len(steps_result),
            }

    # =========================================================================
    # BUILD OPERATIONS (WRITE)
    # =========================================================================

    async def _handle_cancel_build(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Cancel a running Cloud Build execution."""
        from google.cloud.devtools import cloudbuild_v1

        build_id = params["build_id"]

        request = cloudbuild_v1.CancelBuildRequest(
            project_id=self.project_id,
            id=build_id,
        )

        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        # cancel_build returns a Build object directly (not an Operation)
        build = await asyncio.to_thread(lambda: client.cancel_build(request=request))

        result = serialize_build_summary(build)
        result["action"] = "cancelled"
        return result

    async def _handle_retry_build(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Retry a failed or cancelled Cloud Build execution."""
        from google.cloud.devtools import cloudbuild_v1

        build_id = params["build_id"]

        request = cloudbuild_v1.RetryBuildRequest(
            project_id=self.project_id,
            id=build_id,
        )

        client = getattr(self, "_cloud_build_client", None)
        if client is None:
            client = await asyncio.to_thread(
                lambda: cloudbuild_v1.CloudBuildClient(credentials=self._credentials)
            )
        assert client is not None  # noqa: S101

        # retry_build returns a long-running Operation, NOT a Build directly.
        # Do NOT call operation.result() -- that would block until the build completes.
        operation = await asyncio.to_thread(lambda: client.retry_build(request=request))

        return {
            "action": "retried",
            "operation_name": operation.operation.name
            if hasattr(operation, "operation")
            else str(operation),
            "build_id": build_id,
            "message": ("Build retry initiated. Use list_builds to check the new build status."),
        }
