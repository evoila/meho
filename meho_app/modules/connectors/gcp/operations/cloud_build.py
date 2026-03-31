# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Cloud Build Operation Definitions (Phase 49)

Operations for inspecting Cloud Build pipelines: list builds, view step-level
execution details, read per-step logs, list triggers, and cancel/retry builds.
"""

from meho_app.modules.connectors.base import OperationDefinition

CLOUD_BUILD_OPERATIONS = [
    # Build Operations (READ)
    OperationDefinition(
        operation_id="list_builds",
        name="List Cloud Builds",
        description=(
            "List Cloud Build executions with status, timing, source info (repo, "
            "commit, branch), and output images. Use filter to narrow results "
            "(e.g., 'status=\"FAILURE\"' or 'build_trigger_id=\"abc123\"'). "
            "Returns build summaries sorted by create time descending."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "filter",
                "type": "string",
                "required": False,
                "description": (
                    "Cloud Build filter expression "
                    "(e.g., 'status=\"FAILURE\"', 'build_trigger_id=\"abc\"')"
                ),
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of builds to return (default: 25)",
            },
        ],
        example="list_builds(filter='status=\"FAILURE\"')",
    ),
    OperationDefinition(
        operation_id="get_build",
        name="Get Build Details",
        description=(
            "Get detailed information about a specific Cloud Build execution "
            "including step-by-step execution with per-step status, duration, "
            "and container images used. Shows failure point, output images "
            "produced, substitutions, and build options."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "build_id",
                "type": "string",
                "required": True,
                "description": "The ID of the build to inspect",
            },
        ],
        example="get_build(build_id='abc123-def456')",
    ),
    OperationDefinition(
        operation_id="list_build_triggers",
        name="List Build Triggers",
        description=(
            "List automated Cloud Build trigger configurations including "
            "source repository, branch/tag patterns, and build config filenames. "
            "Shows which triggers are active or disabled."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of triggers to return (default: 50)",
            },
        ],
        example="list_build_triggers()",
    ),
    OperationDefinition(
        operation_id="get_build_logs",
        name="Get Build Logs",
        description=(
            "Get per-step build logs for diagnosing failures. Reads logs from "
            "Cloud Storage for the specified build. Can target a specific step "
            "or return all steps' logs. Tailed to 200 lines per step by default."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "build_id",
                "type": "string",
                "required": True,
                "description": "The ID of the build whose logs to fetch",
            },
            {
                "name": "step_index",
                "type": "integer",
                "required": False,
                "description": (
                    "Zero-based index of a specific step to fetch logs for. "
                    "If omitted, returns logs for all steps."
                ),
            },
            {
                "name": "tail_lines",
                "type": "integer",
                "required": False,
                "description": "Number of lines to tail per step (default: 200)",
            },
        ],
        example="get_build_logs(build_id='abc123-def456', step_index=2)",
    ),
    # Build Operations (WRITE)
    OperationDefinition(
        operation_id="cancel_build",
        name="Cancel Build",
        description=(
            "Cancel a running Cloud Build execution. The build must be in "
            "QUEUED or WORKING status. Returns the updated build status."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "build_id",
                "type": "string",
                "required": True,
                "description": "The ID of the build to cancel",
            },
        ],
        example="cancel_build(build_id='abc123-def456')",
    ),
    OperationDefinition(
        operation_id="retry_build",
        name="Retry Build",
        description=(
            "Retry a failed or cancelled Cloud Build execution. Creates a new "
            "build with the same configuration. Returns operation info -- use "
            "list_builds to check the new build status."
        ),
        category="ci_cd",
        parameters=[
            {
                "name": "build_id",
                "type": "string",
                "required": True,
                "description": "The ID of the build to retry",
            },
        ],
        example="retry_build(build_id='abc123-def456')",
    ),
]
