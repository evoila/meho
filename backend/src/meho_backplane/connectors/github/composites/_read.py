# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only ``gh.composite.*`` handler functions (1 composite at T4).

T4 (#1224) ships the first L1 composite for the gh-rest connector:
``gh.composite.pr_status_summary``. The pattern mirrors the vmware-rest
composites at
:mod:`meho_backplane.connectors.vmware_rest.composites._read` -- module-
level async functions that take the dispatcher's composite-branch
keyword args.

Why module-level functions
--------------------------

:func:`~meho_backplane.operations.typed_register.derive_handler_ref`
rejects closures, ``functools.partial``, and lambdas at registration
time. Module-level ``async def`` is the only shape the dispatcher can
resolve via ``importlib.import_module`` + chained ``getattr`` at first-
dispatch time.

Direct-session dispatch (#2255)
-------------------------------

The handler declares a ``connector`` parameter and issues its three
reads through the resolved :class:`GitHubRestConnector`'s own session
(``connector._get_json`` against ``connector.mount_op_path``), bypassing
``endpoint_descriptor`` entirely. This is the #2251 direct-session
substrate: it makes the composite work on a **fresh deploy with no gh
catalog ingest** (the #2050 ``composite_l2_missing`` dead-end is gone),
so the gh-only ``composite_backing`` machinery is no longer needed here.

Taking the direct path deliberately drops two of the four guarantees the
old ``dispatch_child`` seam carried (documented on the vmware-rest
precedent's "four reasons" note) and relocates the other two:

* **Bounded recursion is moot** -- a direct session call cannot re-enter
  the dispatcher, so there is no recursion to bound.
* **Per-sub-op param validation goes away** -- the handler builds each
  request in code from the already-schema-validated top-level params, so
  re-validating against a persisted ingested schema is redundant.
* **Audit-tree linkage** collapses to the top-level op's own audit row.
* **Per-sub-op policy-gate + broadcast is evaded** -- acceptable for a
  **read** composite (the top-level op is already gated); write
  composites keep this question open (Initiative #2249).

Partial-failure tolerance
-------------------------

The composite degrades gracefully on the two *secondary* sub-calls:

* If ``/repos/{owner}/{repo}/commits/{ref}/check-runs`` fails (e.g. the
  repo has no checks configured -> 404), the composite returns
  ``checks=None`` + ``checks_status="unknown"`` and continues.
* If ``/repos/{owner}/{repo}/pulls/{pull_number}/reviews`` fails, the
  composite returns ``reviews=None`` + ``review_status="unknown"`` and
  continues.

The *primary* sub-call (``/repos/{owner}/{repo}/pulls/{pull_number}``)
is non-optional -- the composite cannot extract the head SHA without it,
so a failure there propagates (an ``httpx.HTTPStatusError`` or a
``RuntimeError`` on a malformed payload) and the dispatcher's outer
exception branch wraps it as ``connector_error``.

The graceful-degradation design matches the issue body's acceptance
criterion: "if the checks call 404s (no checks configured for the repo),
the composite still returns the PR + reviews + ``checks: null`` cleanly
-- does NOT bail mid-flight."
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator

if TYPE_CHECKING:
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = ["pr_status_summary_composite"]

_log = structlog.get_logger(__name__)

# GitHub REST wire paths, keyed with ``str.format`` placeholders. The
# connector's ``_base_url`` mounts them under ``https://api.github.com``;
# ``mount_op_path`` is identity for github today (a future GHES override
# would remap here, matching the vmware-rest ``/api`` vs ``/rest`` mount).
_PR_PATH = "/repos/{owner}/{repo}/pulls/{pull_number}"
_CHECK_RUNS_PATH = "/repos/{owner}/{repo}/commits/{ref}/check-runs"
_REVIEWS_PATH = "/repos/{owner}/{repo}/pulls/{pull_number}/reviews"


async def _optional_get(
    connector: GitHubRestConnector,
    target: Any,
    path: str,
    operator: Operator,
) -> Any:
    """Return the GET payload for *path*, or ``None`` when the call fails.

    Wraps a *secondary* sub-call (checks / reviews) so a 404 (the repo
    has no checks configured), a rate-limit, or a transport error
    degrades to ``None`` -- the composite then surfaces
    ``checks_status`` / ``review_status`` of ``"unknown"`` rather than
    aborting the whole envelope. The primary PR sub-call does NOT go
    through this helper: its failure propagates to the dispatcher's
    ``connector_error`` branch (the head SHA is load-bearing).
    """
    try:
        return await connector._get_json(target, path, operator=operator)
    except (httpx.HTTPError, OSError) as exc:
        _log.info(
            "pr_status_summary_secondary_subcall_failed",
            path=path,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None


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


def _assemble_envelope(
    pr_payload: Any,
    checks_payload: Any,
    reviews_payload: Any,
) -> dict[str, Any]:
    """Build the seven-key PR-status envelope from the three sub-call payloads.

    ``mergeable`` / ``mergeable_state`` pass through from the PR payload
    verbatim (GitHub's tri-state); the two ``*_status`` summaries collapse
    the checks / reviews arrays into agent-actionable enums, degrading to
    ``"unknown"`` when the corresponding secondary read failed (``None``).
    """
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


async def pr_status_summary_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Aggregate PR metadata + checks + reviews + mergeable in one envelope.

    Op-id: ``gh.composite.pr_status_summary``.

    Reads (all through the injected ``connector``'s own session, #2255):

    1. ``GET /repos/{owner}/{repo}/pulls/{pull_number}`` -- the PR
       payload. Required; failure propagates as ``connector_error``.
    2. ``GET /repos/{owner}/{repo}/commits/{ref}/check-runs`` against
       the head SHA from step 1. Optional; failure surfaces as
       ``checks=None`` + ``checks_status="unknown"``.
    3. ``GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews`` --
       reviews list. Optional; failure surfaces as ``reviews=None`` +
       ``review_status="unknown"``.

    Steps 2 and 3 fire in parallel via :func:`asyncio.gather` -- they
    are independent (neither depends on the other's output) and run
    over the same authenticated session, so a parallel fanout halves
    the operator-perceived latency vs. sequential dispatch.

    Each path is routed through :meth:`connector.mount_op_path` before
    the wire call. That override is identity for github.com today; a
    future GHES connector remaps the mount there, matching the
    vmware-rest ``/api`` vs ``/rest`` precedent.

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
    httpx.HTTPStatusError
        The primary PR sub-call returned a non-2xx status (e.g. 404 /
        401). Mapped by the dispatcher's outer exception branch to the
        matching structured :class:`OperationResult` (auth-class
        statuses to their specific shapes, everything else to
        ``connector_error``).
    RuntimeError
        The PR payload was malformed (no head SHA extractable). Wrapped
        into a ``connector_error`` :class:`OperationResult` by the
        dispatcher's outer exception branch.
    """
    owner = params["owner"]
    repo = params["repo"]
    pull_number = params["pull_number"]

    pr_path = await connector.mount_op_path(
        target,
        _PR_PATH.format(owner=owner, repo=repo, pull_number=pull_number),
        operator,
    )
    pr_payload = await connector._get_json(target, pr_path, operator=operator)
    head_sha = _extract_head_sha(pr_payload)
    if head_sha is None:
        # The PR call succeeded but the response shape is unexpected.
        # Surface as RuntimeError rather than silently using a stale /
        # empty SHA; the dispatcher's outer branch wraps it cleanly.
        raise RuntimeError(
            f"pr_status_summary: PR payload from {pr_path!r} did not carry a head.sha "
            f"string for {owner}/{repo}#{pull_number}; got payload type "
            f"{type(pr_payload).__name__}"
        )

    checks_path = await connector.mount_op_path(
        target,
        _CHECK_RUNS_PATH.format(owner=owner, repo=repo, ref=head_sha),
        operator,
    )
    reviews_path = await connector.mount_op_path(
        target,
        _REVIEWS_PATH.format(owner=owner, repo=repo, pull_number=pull_number),
        operator,
    )
    checks_payload, reviews_payload = await asyncio.gather(
        _optional_get(connector, target, checks_path, operator),
        _optional_get(connector, target, reviews_path, operator),
    )
    return _assemble_envelope(pr_payload, checks_payload, reviews_payload)
