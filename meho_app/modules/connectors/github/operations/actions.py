# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub Actions Operations.

Operations for workflow runs, jobs, logs, and re-running failed jobs.
"""

from meho_app.modules.connectors.base import OperationDefinition

ACTION_OPERATIONS = [
    OperationDefinition(
        operation_id="list_workflow_runs",
        name="List Workflow Runs",
        description=(
            "List GitHub Actions workflow runs in a repository with status, "
            "conclusion, triggering branch/commit, and timing. Supports "
            "filtering by status and branch."
        ),
        category="actions",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "status",
                "type": "string",
                "required": False,
                "description": "Filter by status: queued, in_progress, completed, action_required, cancelled, failure, neutral, skipped, stale, success, timed_out, waiting",
            },
            {
                "name": "branch",
                "type": "string",
                "required": False,
                "description": "Filter by branch name",
            },
            {
                "name": "per_page",
                "type": "integer",
                "required": False,
                "description": "Number of runs per page (default: 30, max: 100)",
            },
        ],
        example='{"repo": "my-service", "status": "failure"}',
        response_entity_type="WorkflowRun",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_workflow_run",
        name="Get Workflow Run",
        description=(
            "Get detailed workflow run information including status, "
            "conclusion, triggering event, head SHA, and timing."
        ),
        category="actions",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "run_id",
                "type": "integer",
                "required": True,
                "description": "Workflow run ID",
            },
        ],
        example='{"repo": "my-service", "run_id": 12345678}',
        response_entity_type="WorkflowRun",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_workflow_jobs",
        name="List Workflow Jobs",
        description=(
            "List jobs within a workflow run with step-level status, "
            "conclusion, timing, and runner info. Use this to find the "
            "specific failing job before fetching logs."
        ),
        category="actions",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "run_id",
                "type": "integer",
                "required": True,
                "description": "Workflow run ID",
            },
        ],
        example='{"repo": "my-service", "run_id": 12345678}',
        response_entity_type="WorkflowJob",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_workflow_logs",
        name="Get Workflow Logs",
        description=(
            "Download logs for a specific workflow job. Returns the last "
            "200 lines by default (configurable via tail_lines). Use "
            "list_workflow_jobs first to find the failing job ID."
        ),
        category="actions",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "job_id",
                "type": "integer",
                "required": True,
                "description": "Workflow job ID (from list_workflow_jobs)",
            },
            {
                "name": "tail_lines",
                "type": "integer",
                "required": False,
                "description": "Number of lines to return from end of log (default: 200)",
            },
        ],
        example='{"repo": "my-service", "job_id": 87654321, "tail_lines": 200}',
        response_entity_type=None,
        response_identifier_field=None,
        response_display_name_field=None,
    ),
    OperationDefinition(
        operation_id="rerun_failed_jobs",
        name="Re-run Failed Jobs",
        description=(
            "Re-run only the failed jobs in a workflow run. Requires WRITE "
            "trust level. This triggers a new attempt for failed jobs only, "
            "preserving successful job results."
        ),
        category="actions",
        parameters=[
            {
                "name": "repo",
                "type": "string",
                "required": True,
                "description": "Repository name (without org prefix)",
            },
            {
                "name": "run_id",
                "type": "integer",
                "required": True,
                "description": "Workflow run ID to re-run failed jobs for",
            },
        ],
        example='{"repo": "my-service", "run_id": 12345678}',
        response_entity_type=None,
        response_identifier_field=None,
        response_display_name_field=None,
    ),
]
