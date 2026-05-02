# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Actions Handler Mixin.

Handles workflow runs, jobs, logs, and re-running failed jobs.
This is the largest handler mixin with 5 operations.
"""

from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.github.serializers import (
    serialize_workflow_job,
    serialize_workflow_run,
)

logger = get_logger(__name__)


class ActionsHandlerMixin:
    """Mixin for GitHub Actions operations: runs, jobs, logs, rerun."""

    # These will be provided by GitHubConnector (base class)
    async def _get(self, path: str, params: dict | None = None) -> dict: ...  # type: ignore[empty-body]

    async def _post(self, path: str, json: Any = None) -> dict: ...  # type: ignore[empty-body]

    async def _get_text(self, path: str, params: dict | None = None) -> str: ...  # type: ignore[empty-body]

    async def _get_paginated(  # type: ignore[empty-body]
        self,
        path: str,
        params: dict | None = None,
        max_pages: int = 5,
        per_page: int = 30,
    ) -> list: ...

    organization: str

    # =========================================================================
    # HANDLER METHODS
    # =========================================================================

    async def _list_workflow_runs_handler(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """
        List workflow runs in a repository.

        Uses GET /repos/{org}/{repo}/actions/runs. GitHub wraps results
        in {"workflow_runs": [...]}, which _get_paginated handles.
        """
        repo = params["repo"]
        query_params: dict[str, Any] = {}

        status = params.get("status")
        if status:
            query_params["status"] = status

        branch = params.get("branch")
        if branch:
            query_params["branch"] = branch

        per_page = params.get("per_page")
        if per_page:
            query_params["per_page"] = per_page

        runs = await self._get_paginated(
            f"/repos/{self.organization}/{repo}/actions/runs",
            params=query_params,
        )
        return [serialize_workflow_run(r) for r in runs]

    async def _get_workflow_run_handler(self, params: dict[str, Any]) -> dict:
        """
        Get detailed workflow run information.

        Uses GET /repos/{org}/{repo}/actions/runs/{run_id}.
        """
        repo = params["repo"]
        run_id = params["run_id"]

        data = await self._get(
            f"/repos/{self.organization}/{repo}/actions/runs/{run_id}",
        )
        return serialize_workflow_run(data)

    async def _list_workflow_jobs_handler(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """
        List jobs within a workflow run with step-level status.

        Uses GET /repos/{org}/{repo}/actions/runs/{run_id}/jobs.
        GitHub wraps results in {"jobs": [...]}, which _get_paginated handles.
        """
        repo = params["repo"]
        run_id = params["run_id"]

        jobs = await self._get_paginated(
            f"/repos/{self.organization}/{repo}/actions/runs/{run_id}/jobs",
        )
        return [serialize_workflow_job(j) for j in jobs]

    async def _get_workflow_logs_handler(self, params: dict[str, Any]) -> dict:
        """
        Download logs for a specific workflow job.

        Uses GET /repos/{org}/{repo}/actions/jobs/{job_id}/logs.
        This endpoint returns a 302 redirect to a CDN-served plain text file,
        so we use _get_text with follow_redirects=True.

        Tails the last N lines (default 200) to keep response within token budget.
        """
        repo = params["repo"]
        job_id = params["job_id"]
        tail_lines = params.get("tail_lines", 200)

        try:
            log_text = await self._get_text(
                f"/repos/{self.organization}/{repo}/actions/jobs/{job_id}/logs",
            )
        except Exception as e:
            logger.warning(
                f"Failed to download logs for job {job_id}: {e}",
                extra={"connector_id": getattr(self, "connector_id", None)},
            )
            return {
                "job_id": job_id,
                "error": f"Failed to download logs: {e}. Logs may have expired (retained for 90 days).",
            }

        # Tail last N lines
        lines = log_text.splitlines()
        if len(lines) > tail_lines:
            lines = lines[-tail_lines:]
            truncated = True
        else:
            truncated = False

        return {
            "job_id": job_id,
            "log_lines": len(lines),
            "truncated": truncated,
            "log": "\n".join(lines),
        }

    async def _rerun_failed_jobs_handler(self, params: dict[str, Any]) -> dict:
        """
        Re-run only the failed jobs in a workflow run.

        Uses POST /repos/{org}/{repo}/actions/runs/{run_id}/rerun-failed-jobs.
        Requires WRITE trust level. Returns 201 on success.
        """
        repo = params["repo"]
        run_id = params["run_id"]

        await self._post(
            f"/repos/{self.organization}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
        )
        return {
            "run_id": run_id,
            "status": "rerun_requested",
        }
