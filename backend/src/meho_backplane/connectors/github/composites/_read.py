# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only ``gh.composite.*`` handler functions (1 composite at T4).

T4 (#1224) ships the first L1 composite for the gh-rest connector:
``gh.composite.pr_status_summary``. The pattern mirrors the vmware-rest
composites at
:mod:`meho_backplane.connectors.vmware_rest.composites._read` -- module-
level async functions that take the dispatcher's composite-branch
keyword args and route every sub-call through the injected
``dispatch_child`` (the
:class:`~meho_backplane.operations.composite.DispatchChild` callable).

Why module-level functions
--------------------------

:func:`~meho_backplane.operations.typed_register.derive_handler_ref`
rejects closures, ``functools.partial``, and lambdas at registration
time. Module-level ``async def`` is the only shape the dispatcher can
resolve via ``importlib.import_module`` + chained ``getattr`` at first-
dispatch time.

Why ``dispatch_child`` not direct httpx
---------------------------------------

The four invariants documented on the vmware-rest precedent apply
verbatim:

1. **Audit-tree linkage** -- ``dispatch_child`` binds
   ``parent_audit_id_var`` so every sub-op's audit row carries the
   composite parent's id.
2. **Bounded recursion** -- ``composite_depth_var`` enforces
   ``Settings.composite_max_depth``.
3. **Policy + broadcast** -- the dispatcher's policy gate and broadcast
   publish run on every dispatched sub-op.
4. **Param validation** -- each sub-op's ``parameter_schema`` validates
   inbound params at dispatch time.

Partial-failure tolerance
-------------------------

The composite degrades gracefully on the two *secondary* sub-calls:

* If ``GET:/repos/{owner}/{repo}/commits/{ref}/check-runs`` fails (e.g.
  the repo has no checks configured -> 404), the composite returns
  ``checks=None`` + ``checks_status="unknown"`` and continues.
* If ``GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews`` fails,
  the composite returns ``reviews=None`` + ``review_status="unknown"``
  and continues.

The *primary* sub-call (``GET:/repos/{owner}/{repo}/pulls/
{pull_number}``) is non-optional -- the composite cannot extract the
head SHA without it, so a failure there raises ``RuntimeError`` and the
dispatcher's outer exception branch wraps it as ``connector_error``.

The graceful-degradation design matches the issue body's acceptance
criterion: "if ``gh.pr.get_checks`` 404s (no checks configured for the
repo), the composite still returns the PR + reviews + ``checks: null``
cleanly -- does NOT bail mid-flight."
"""

from __future__ import annotations

import asyncio
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.github.composites._preflight import (
    preflight_l2_dependencies,
)
from meho_backplane.operations.composite import DispatchChild

__all__ = ["pr_status_summary_composite"]


# Connector-id constant: matches what the connector registers via
# ``register_connector_v2(product="gh", version="3", impl_id="gh-rest")``
# in the package ``__init__``. The version slot is the digit-prefix
# ``"3"`` (the parse_connector_id regex's constraint), and the
# operator-visible "v3" label lives in the catalog YAML and in docs.
_CONNECTOR_ID = "gh-rest-3"

# L2 sub-op-ids. The ingest pipeline emits op_ids as ``METHOD:/path``
# strings (see :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`).
_OP_GET_PULL = "GET:/repos/{owner}/{repo}/pulls/{pull_number}"
_OP_GET_CHECK_RUNS = "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs"
_OP_LIST_REVIEWS = "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews"

# Composite op_id constants -- used by the preflight cache key. Mirrors
# the vmware-rest module's pattern so the test-side coverage assertion
# (every registered composite has both a sub-op_id tuple and a cache-key
# constant) can read them by name.
_COMPOSITE_OP_ID_PR_STATUS_SUMMARY = "gh.composite.pr_status_summary"

# Per-composite sub-op-id tuple consumed by the L2 pre-flight check.
# Ordered to match the dispatch sequence (PR first, then the parallel
# pair) for readability when the exception payload surfaces the missing
# ops in declaration order.
_SUB_OPS_PR_STATUS_SUMMARY: tuple[str, ...] = (
    _OP_GET_PULL,
    _OP_GET_CHECK_RUNS,
    _OP_LIST_REVIEWS,
)


def _require_ok(result: OperationResult) -> Any:
    """Return :attr:`OperationResult.result` or raise on a non-OK status.

    Used for the *primary* sub-call only. The composite cannot proceed
    without the PR payload (the head SHA drives the checks call), so
    the all-or-nothing semantics are appropriate here -- the dispatcher
    wraps the raised ``RuntimeError`` into a ``connector_error`` for
    the composite parent.
    """
    if result.status != "ok":
        raise RuntimeError(
            f"composite sub-op {result.op_id!r} returned status="
            f"{result.status!r}: {result.error or '<no error message>'}"
        )
    return result.result


def _summarize_checks(checks_payload: Any) -> str:
    """Collapse the check-runs array into an agent-actionable status.

    GitHub returns ``{"total_count": N, "check_runs": [...]}``. Each
    run has a ``status`` (``queued`` / ``in_progress`` / ``completed``)
    and a ``conclusion`` (``success`` / ``failure`` / ``neutral`` /
    ``cancelled`` / ``skipped`` / ``timed_out`` / ``action_required`` /
    ``stale`` / null).

    Returns
    -------
    ``"all_passed"``
        Every check is completed with conclusion in ``{success, neutral,
        skipped}``.
    ``"any_failed"``
        At least one completed check has conclusion in ``{failure,
        cancelled, timed_out, action_required, stale}``.
    ``"pending"``
        At least one check is not yet completed (status != "completed")
        and no completed-fail check is present.
    ``"no_checks"``
        Empty ``check_runs`` array (repo has no checks configured /
        none ran for this commit).
    ``"unknown"``
        Payload shape was unexpected (e.g. not a dict, or
        ``check_runs`` missing). Caller treats this as
        "could not determine".
    """
    if not isinstance(checks_payload, dict):
        return "unknown"
    runs = checks_payload.get("check_runs")
    if not isinstance(runs, list):
        return "unknown"
    if not runs:
        return "no_checks"
    fail_conclusions = {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
    }
    pass_conclusions = {"success", "neutral", "skipped"}
    has_pending = False
    for run in runs:
        if not isinstance(run, dict):
            return "unknown"
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            has_pending = True
            continue
        if conclusion in fail_conclusions:
            return "any_failed"
        if conclusion not in pass_conclusions:
            # A completed run with an unexpected conclusion (e.g. null
            # when GitHub flips a check to completed before populating
            # conclusion). Be conservative: treat as pending.
            has_pending = True
    return "pending" if has_pending else "all_passed"


def _summarize_reviews(reviews_payload: Any) -> str:
    """Collapse the reviews array into an agent-actionable status.

    GitHub returns a list of review records, each with ``user.login``
    and ``state`` (``APPROVED`` / ``CHANGES_REQUESTED`` / ``COMMENTED``
    / ``PENDING`` / ``DISMISSED``). The list is in chronological order;
    the *latest* review per reviewer is what matters for the merge
    decision.

    Returns
    -------
    ``"changes_requested"``
        Any reviewer's latest review is ``CHANGES_REQUESTED``. A single
        outstanding "request changes" vetoes the PR.
    ``"approved"``
        At least one reviewer's latest is ``APPROVED`` and no reviewer
        has an outstanding ``CHANGES_REQUESTED``.
    ``"commented"``
        Reviewers have commented but no approval / changes-requested
        verdict is on file.
    ``"pending"``
        Only ``PENDING`` reviews exist (drafts the reviewer hasn't
        submitted).
    ``"no_reviews"``
        Empty array (no reviews yet).
    ``"unknown"``
        Payload was not a list (call failed earlier, or the upstream
        returned an unexpected shape).
    """
    if not isinstance(reviews_payload, list):
        return "unknown"
    if not reviews_payload:
        return "no_reviews"
    # Latest review per reviewer login wins -- the list is chronological
    # so iterating left-to-right and overwriting yields the latest.
    # ``DISMISSED`` reviews drop out of the tally (the dismissing UI
    # event is itself recorded as a state change; the dismissed entry
    # no longer counts).
    latest_per_reviewer: dict[str, str] = {}
    for review in reviews_payload:
        if not isinstance(review, dict):
            return "unknown"
        state = review.get("state")
        if not isinstance(state, str):
            continue
        login = _review_login(review)
        if login is None:
            continue
        if state == "DISMISSED":
            latest_per_reviewer.pop(login, None)
            continue
        latest_per_reviewer[login] = state
    if not latest_per_reviewer:
        return "no_reviews"
    states = set(latest_per_reviewer.values())
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    if "COMMENTED" in states:
        return "commented"
    if "PENDING" in states:
        return "pending"
    return "unknown"


def _review_login(review: dict[str, Any]) -> str | None:
    """Extract the reviewer login from a review record, defensively."""
    user = review.get("user")
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    return login if isinstance(login, str) else None


def _extract_head_sha(pr_payload: Any) -> str | None:
    """Return ``pr.head.sha`` or ``None`` when the shape is unexpected.

    The PR sub-call's response shape is the GitHub PR object; the head
    SHA lives at ``head.sha``. Defensive shape checks let the composite
    surface a typed error if the upstream returns a malformed payload
    rather than crashing on a chained ``[...]`` lookup.
    """
    if not isinstance(pr_payload, dict):
        return None
    head = pr_payload.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


async def pr_status_summary_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Aggregate PR metadata + checks + reviews + mergeable in one envelope.

    Op-id: ``gh.composite.pr_status_summary``.

    Sub-ops dispatched:

    1. ``GET:/repos/{owner}/{repo}/pulls/{pull_number}`` -- the PR
       payload. Required; failure raises ``RuntimeError``.
    2. ``GET:/repos/{owner}/{repo}/commits/{ref}/check-runs`` against
       the head SHA from step 1. Optional; failure surfaces as
       ``checks=None`` + ``checks_status="unknown"``.
    3. ``GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews`` --
       reviews list. Optional; failure surfaces as ``reviews=None`` +
       ``review_status="unknown"``.

    Steps 2 and 3 fire in parallel via :func:`asyncio.gather` -- they
    are independent (neither depends on the other's output) and run
    over the same authenticated session, so a parallel fanout halves
    the operator-perceived latency vs. sequential dispatch.

    Returns
    -------
    dict[str, Any]
        ``{"pr": <PR payload>, "checks": <check-runs payload | None>,
        "reviews": <reviews list | None>, "mergeable": <bool | None>,
        "mergeable_state": <str | None>, "checks_status": <enum>,
        "review_status": <enum>}``. See
        :data:`schemas.PR_STATUS_SUMMARY_RESPONSE_SCHEMA` for the
        per-key contract.

    Raises
    ------
    CompositeL2DependencyMissing
        Pre-flight detected that at least one of the three L2 sub-ops
        is not registered in ``endpoint_descriptor``. The exception
        carries the missing op-ids + the catalog command to run.
        Surfaced to the operator as a structured
        ``composite_l2_missing`` :class:`OperationResult` by the
        dispatcher's exception branch.
    RuntimeError
        The primary PR sub-call failed, or the PR payload was malformed
        (no head SHA extractable). Wrapped into a ``connector_error``
        :class:`OperationResult` by the dispatcher's outer exception
        branch.
    """
    await preflight_l2_dependencies(
        composite_op_id=_COMPOSITE_OP_ID_PR_STATUS_SUMMARY,
        sub_op_ids=_SUB_OPS_PR_STATUS_SUMMARY,
        connector_id=_CONNECTOR_ID,
        tenant_id=operator.tenant_id,
    )
    owner = params["owner"]
    repo = params["repo"]
    pull_number = params["pull_number"]
    pr_params = {"owner": owner, "repo": repo, "pull_number": pull_number}

    pr_payload = _require_ok(
        await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_GET_PULL,
            params=pr_params,
        )
    )
    head_sha = _extract_head_sha(pr_payload)
    if head_sha is None:
        # The PR call succeeded but the response shape is unexpected.
        # Surface as RuntimeError rather than silently using a stale /
        # empty SHA; the dispatcher's outer branch wraps it cleanly.
        raise RuntimeError(
            f"pr_status_summary: PR payload from {_OP_GET_PULL!r} did not carry a head.sha "
            f"string for {owner}/{repo}#{pull_number}; got payload type "
            f"{type(pr_payload).__name__}"
        )

    checks_result, reviews_result = await asyncio.gather(
        dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_GET_CHECK_RUNS,
            params={"owner": owner, "repo": repo, "ref": head_sha},
        ),
        dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_LIST_REVIEWS,
            params=pr_params,
        ),
        return_exceptions=False,
    )

    checks_payload: Any = checks_result.result if checks_result.status == "ok" else None
    reviews_payload: Any = reviews_result.result if reviews_result.status == "ok" else None

    pr_dict = pr_payload if isinstance(pr_payload, dict) else {}
    return {
        "pr": pr_payload,
        "checks": checks_payload,
        "reviews": reviews_payload,
        "mergeable": pr_dict.get("mergeable"),
        "mergeable_state": pr_dict.get("mergeable_state"),
        "checks_status": (
            _summarize_checks(checks_payload) if checks_payload is not None else "unknown"
        ),
        "review_status": (
            _summarize_reviews(reviews_payload) if reviews_payload is not None else "unknown"
        ),
    }
