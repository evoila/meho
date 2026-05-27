# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter + response schemas for gh-rest composites.

Same conventions as the vmware-rest composite schemas module
(G3.1-T5 #508 / T6 #509):

* ``additionalProperties=False`` on every parameter schema so an
  operator typo on an optional key surfaces as a clear validation error
  rather than disappearing through a permissive shape.
* Schemas declare only what the handler reads. Per-composite
  documentation lives on ``description`` keys; ``describe_operation``
  surfaces the schema verbatim to LLM clients.
* T4 ships exactly one composite -- ``gh.composite.pr_status_summary``
  -- which is read-only (``safety_level="read"`` /
  ``requires_approval=False`` per the issue body). Future T7+ Tasks
  add write composites and reuse this module's pattern.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PR_STATUS_SUMMARY_PARAMETER_SCHEMA",
    "PR_STATUS_SUMMARY_RESPONSE_SCHEMA",
]


PR_STATUS_SUMMARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "pull_number"],
    "properties": {
        "owner": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository owner (user or organisation login).",
        },
        "repo": {
            "type": "string",
            "minLength": 1,
            "description": "GitHub repository name (without the owner prefix).",
        },
        "pull_number": {
            "type": "integer",
            "minimum": 1,
            "description": "The pull-request number (the integer suffix in /pull/<N> URLs).",
        },
    },
    "description": (
        "Parameters for gh.composite.pr_status_summary. Returns the PR "
        "metadata, the head-commit check runs, the reviews, and the "
        "mergeable state in a single envelope -- answers 'is this PR "
        "ready to merge?' without three separate L2 calls."
    ),
}


PR_STATUS_SUMMARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "pr",
        "checks",
        "reviews",
        "mergeable",
        "mergeable_state",
        "checks_status",
        "review_status",
    ],
    "properties": {
        "pr": {
            "description": (
                "Pull-request payload from GET /repos/{owner}/{repo}/pulls/"
                "{pull_number}. The composite surfaces it verbatim so an "
                "operator can read any field the upstream returns. Required "
                "(the composite errors out when the PR itself cannot be "
                "fetched -- partial-failure tolerance applies only to the "
                "checks and reviews sub-calls)."
            ),
        },
        "checks": {
            "description": (
                "Response from GET /repos/{owner}/{repo}/commits/{ref}/"
                "check-runs against the PR's head SHA. ``null`` when the "
                "sub-call failed (e.g. 404 because the repo has no checks "
                "configured); the composite degrades gracefully on this "
                "particular sub-call rather than aborting the whole "
                "envelope. ``checks_status`` summarises whether checks "
                "are all-passing / any-failed / pending / unknown."
            ),
        },
        "reviews": {
            "description": (
                "List of review records from GET /repos/{owner}/{repo}/"
                "pulls/{pull_number}/reviews. ``null`` when the sub-call "
                "failed; the composite degrades gracefully here too. "
                "``review_status`` summarises the latest disposition per "
                "reviewer (approved / changes_requested / commented / "
                "pending / unknown)."
            ),
        },
        "mergeable": {
            "type": ["boolean", "null"],
            "description": (
                "PR's ``mergeable`` field. GitHub computes this "
                "asynchronously after a push and returns ``null`` until "
                "the background job runs; the composite passes that "
                "tri-state through verbatim."
            ),
        },
        "mergeable_state": {
            "type": ["string", "null"],
            "description": (
                "PR's ``mergeable_state`` (``clean``, ``dirty``, "
                "``blocked``, ``behind``, ``unknown``, etc.). "
                "Pass-through from the upstream payload."
            ),
        },
        "checks_status": {
            "type": "string",
            "enum": [
                "all_passed",
                "any_failed",
                "pending",
                "no_checks",
                "unknown",
            ],
            "description": (
                "Composite-computed summary of the check-runs payload. "
                "``unknown`` when the checks sub-call failed; "
                "``no_checks`` when the call succeeded but returned an "
                "empty array; otherwise derived from the per-run "
                "``conclusion`` / ``status`` fields."
            ),
        },
        "review_status": {
            "type": "string",
            "enum": [
                "approved",
                "changes_requested",
                "commented",
                "pending",
                "no_reviews",
                "unknown",
            ],
            "description": (
                "Composite-computed summary of the reviews payload. "
                "``unknown`` when the reviews sub-call failed; "
                "``no_reviews`` when it returned an empty array; "
                "otherwise derived from the latest-per-reviewer "
                "disposition with ``changes_requested`` as the dominant "
                "veto."
            ),
        },
    },
    "description": (
        "Aggregated PR-status envelope. Designed for the 'is this PR "
        "ready to merge?' agent question -- one composite call yields "
        "the four signals the LLM client needs (metadata, checks, "
        "reviews, mergeable) plus two pre-computed summaries that "
        "collapse the per-record arrays into agent-actionable states."
    ),
}
