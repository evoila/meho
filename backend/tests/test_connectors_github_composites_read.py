# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``gh.composite.pr_status_summary`` (G3.11-T4 #1224).

Coverage matrix:

* Happy path -- three sub-ops fire in the expected order with the right
  params; head SHA flows from the PR sub-call into the check-runs call;
  the aggregated envelope matches the documented shape.
* Parallelism -- the two secondary sub-ops fire concurrently
  (``asyncio.gather``) rather than sequentially.
* Partial-failure tolerance:
  * Checks sub-call errors -> ``checks=None`` + ``checks_status="unknown"``;
    composite still returns the PR + reviews cleanly.
  * Reviews sub-call errors -> ``reviews=None`` + ``review_status="unknown"``;
    composite still returns the PR + checks cleanly.
  * Both secondaries fail -> composite still returns the PR with the
    two None / "unknown" payloads.
* Primary failure -- PR sub-call returns ``status="error"`` raises
  ``RuntimeError`` (load-bearing for the dispatcher's
  ``connector_error`` wrapping at the composite parent).
* Malformed PR payload (no head.sha) raises ``RuntimeError``.
* Status summarisers (``_summarize_checks`` / ``_summarize_reviews``)
  collapse the raw arrays into the agent-actionable enum values.

The L2 pre-flight is exercised separately in
``test_connectors_github_composites_l2_preflight.py``; this module
primes the pre-flight cache so handler-direct tests skip the DB walk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.github.composites import _preflight
from meho_backplane.connectors.github.composites._read import (
    _summarize_checks,
    _summarize_reviews,
    pr_status_summary_composite,
)


@pytest.fixture(autouse=True)
def _prime_preflight_cache() -> Iterator[None]:
    """Prime the L2 pre-flight cache so handler-direct tests skip the DB walk.

    Same pattern as the vmware-rest read-composite tests. The pre-flight
    behaviour (cache miss + DB walk; missing-L2 -> structured exception)
    is exercised in the dedicated preflight test module where a stub
    ``lookup_descriptor`` covers both code paths.
    """
    _preflight.reset_preflight_cache()
    _preflight._PREFLIGHT_CACHE.add("gh.composite.pr_status_summary")
    yield
    _preflight.reset_preflight_cache()


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-gh-composite-read",
        name="GH Composite Read Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _ok_result(op_id: str, result: Any) -> OperationResult:
    return OperationResult(status="ok", op_id=op_id, result=result, duration_ms=1.0)


def _err_result(op_id: str, error: str) -> OperationResult:
    return OperationResult(status="error", op_id=op_id, error=error, duration_ms=1.0)


class _RecordingDispatchChild:
    """Lightweight ``dispatch_child`` stub matching the DispatchChild Protocol.

    Maps op_id -> canned :class:`OperationResult` (or raw payload, in
    which case it's wrapped as ``status="ok"``). Records every call's
    keyword args so the test can assert call-shape and parallelism.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []
        # Allows simulating gather() concurrency by holding both
        # secondary calls until the second one arrives.
        self.barrier: asyncio.Event | None = None
        self._gather_targets: set[str] = set()

    def set_gather_barrier(self, op_ids: set[str]) -> None:
        """Hold every dispatch of these op_ids until both have entered.

        Used by ``test_secondary_sub_ops_run_in_parallel`` to demonstrate
        that the handler issues both secondary calls concurrently. The
        barrier releases when ``len(in_flight) == len(op_ids)``; a
        sequential handler would deadlock on the first call.
        """
        self.barrier = asyncio.Event()
        self._gather_targets = set(op_ids)
        self._in_flight: list[str] = []

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        self.calls.append(
            {"connector_id": connector_id, "op_id": op_id, "params": dict(params), "target": target}
        )
        if self.barrier is not None and op_id in self._gather_targets:
            self._in_flight.append(op_id)
            if len(self._in_flight) >= len(self._gather_targets):
                self.barrier.set()
            await self.barrier.wait()
        payload = self._responses[op_id]
        if isinstance(payload, OperationResult):
            return payload
        return _ok_result(op_id, payload)


def _pr_payload(head_sha: str = "abc123", **overrides: Any) -> dict[str, Any]:
    """Build a minimal GitHub PR payload with the head.sha field populated."""
    payload: dict[str, Any] = {
        "number": 754,
        "head": {"sha": head_sha, "ref": "feat/x"},
        "mergeable": True,
        "mergeable_state": "clean",
    }
    payload.update(overrides)
    return payload


def _checks_payload(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(runs), "check_runs": list(runs)}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_status_summary_dispatches_three_sub_ops_in_order() -> None:
    """PR first, then checks + reviews; head SHA flows into the checks call."""
    pr = _pr_payload(head_sha="76065a0")
    checks = _checks_payload(
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "test", "status": "completed", "conclusion": "success"},
    )
    reviews = [
        {"user": {"login": "reviewer-a"}, "state": "APPROVED"},
    ]
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": pr,
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": checks,
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": reviews,
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        dispatch_child=dispatch,
    )
    # PR call fires first; the two secondaries can land in either order
    # (asyncio.gather is not order-preserving for entry order).
    assert dispatch.calls[0]["op_id"] == "GET:/repos/{owner}/{repo}/pulls/{pull_number}"
    sub_op_ids = {c["op_id"] for c in dispatch.calls[1:]}
    assert sub_op_ids == {
        "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    }
    # head SHA threaded into the checks call's ref param.
    checks_call = next(
        c
        for c in dispatch.calls
        if c["op_id"] == "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs"
    )
    assert checks_call["params"] == {"owner": "evoila", "repo": "meho", "ref": "76065a0"}
    # Aggregated envelope.
    assert out["pr"] == pr
    assert out["checks"] == checks
    assert out["reviews"] == reviews
    assert out["mergeable"] is True
    assert out["mergeable_state"] == "clean"
    assert out["checks_status"] == "all_passed"
    assert out["review_status"] == "approved"
    # All calls carry the connector id.
    assert all(c["connector_id"] == "gh-rest-3" for c in dispatch.calls)


@pytest.mark.asyncio
async def test_pr_call_params_pass_through() -> None:
    """``owner`` / ``repo`` / ``pull_number`` flow into the PR sub-call params."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _checks_payload(),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": [],
        }
    )
    await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "octocat", "repo": "hello-world", "pull_number": 42},
        dispatch_child=dispatch,
    )
    pr_call = dispatch.calls[0]
    assert pr_call["params"] == {"owner": "octocat", "repo": "hello-world", "pull_number": 42}
    reviews_call = next(
        c
        for c in dispatch.calls
        if c["op_id"] == "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    )
    assert reviews_call["params"] == {"owner": "octocat", "repo": "hello-world", "pull_number": 42}


# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secondary_sub_ops_run_in_parallel() -> None:
    """The checks + reviews sub-calls fire concurrently via asyncio.gather.

    Set a barrier that both secondary calls must enter before either may
    return. A sequential handler would deadlock; the gather()-based
    handler unblocks cleanly because both calls land before either
    awaits the barrier.
    """
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _checks_payload(),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": [],
        }
    )
    dispatch.set_gather_barrier(
        {
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        }
    )
    out = await asyncio.wait_for(
        pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            dispatch_child=dispatch,
        ),
        timeout=2.0,
    )
    assert out["pr"] is not None


# ---------------------------------------------------------------------------
# Partial-failure tolerance (acceptance criterion #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checks_sub_op_failure_does_not_bail() -> None:
    """A 404 on the checks call surfaces as ``checks=None`` + ``checks_status='unknown'``."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _err_result(
                "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs", "not_found: 404"
            ),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": [
                {"user": {"login": "r1"}, "state": "APPROVED"},
            ],
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        dispatch_child=dispatch,
    )
    assert out["checks"] is None
    assert out["checks_status"] == "unknown"
    # Reviews + PR still flow through cleanly.
    assert out["reviews"] is not None
    assert out["review_status"] == "approved"
    assert out["pr"] is not None


@pytest.mark.asyncio
async def test_reviews_sub_op_failure_does_not_bail() -> None:
    """Reviews call failure surfaces as ``reviews=None`` + ``review_status='unknown'``."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _checks_payload(
                {"name": "build", "status": "completed", "conclusion": "success"},
            ),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": _err_result(
                "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews", "unauthorized: 401"
            ),
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        dispatch_child=dispatch,
    )
    assert out["reviews"] is None
    assert out["review_status"] == "unknown"
    assert out["checks"] is not None
    assert out["checks_status"] == "all_passed"
    assert out["pr"] is not None


@pytest.mark.asyncio
async def test_both_secondary_sub_ops_failing_still_returns_pr() -> None:
    """If both secondaries fail, the composite still surfaces the PR + 'unknown' states."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _err_result(
                "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs", "rate_limited"
            ),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": _err_result(
                "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews", "rate_limited"
            ),
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        dispatch_child=dispatch,
    )
    assert out["pr"] is not None
    assert out["checks"] is None
    assert out["reviews"] is None
    assert out["checks_status"] == "unknown"
    assert out["review_status"] == "unknown"


# ---------------------------------------------------------------------------
# Primary failure -- composite must NOT swallow this
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_sub_op_failure_raises_runtime_error() -> None:
    """PR sub-call error -> ``RuntimeError`` (dispatcher wraps as connector_error)."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _err_result(
                "GET:/repos/{owner}/{repo}/pulls/{pull_number}", "not_found: 404"
            ),
        }
    )
    with pytest.raises(RuntimeError, match="status='error'"):
        await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            dispatch_child=dispatch,
        )
    # The composite bailed before issuing the secondary calls.
    assert len(dispatch.calls) == 1


@pytest.mark.asyncio
async def test_pr_payload_missing_head_sha_raises_runtime_error() -> None:
    """A PR payload with no head.sha cannot drive the checks call -> error."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": {
                "number": 754,
                # No head -> no head.sha extractable.
            },
        }
    )
    with pytest.raises(RuntimeError, match=r"head\.sha"):
        await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            dispatch_child=dispatch,
        )


# ---------------------------------------------------------------------------
# Mergeable / mergeable_state pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mergeable_null_is_passed_through_verbatim() -> None:
    """``mergeable=None`` (GitHub still computing) flows through as-is."""
    dispatch = _RecordingDispatchChild(
        {
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}": _pr_payload(
                mergeable=None, mergeable_state="unknown"
            ),
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs": _checks_payload(),
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews": [],
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        dispatch_child=dispatch,
    )
    assert out["mergeable"] is None
    assert out["mergeable_state"] == "unknown"


# ---------------------------------------------------------------------------
# Checks summariser
# ---------------------------------------------------------------------------


def test_summarize_checks_all_passed() -> None:
    payload = _checks_payload(
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "skipped"},
        {"status": "completed", "conclusion": "neutral"},
    )
    assert _summarize_checks(payload) == "all_passed"


def test_summarize_checks_any_failed() -> None:
    payload = _checks_payload(
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "failure"},
    )
    assert _summarize_checks(payload) == "any_failed"


def test_summarize_checks_pending() -> None:
    payload = _checks_payload(
        {"status": "completed", "conclusion": "success"},
        {"status": "in_progress", "conclusion": None},
    )
    assert _summarize_checks(payload) == "pending"


def test_summarize_checks_no_checks() -> None:
    assert _summarize_checks(_checks_payload()) == "no_checks"


def test_summarize_checks_unknown_shape() -> None:
    assert _summarize_checks(None) == "unknown"
    assert _summarize_checks({"check_runs": "not-a-list"}) == "unknown"


def test_summarize_checks_treats_unexpected_conclusion_as_pending() -> None:
    """A completed run with a null/unexpected conclusion is conservative -> pending."""
    payload = _checks_payload(
        {"status": "completed", "conclusion": None},
    )
    assert _summarize_checks(payload) == "pending"


# ---------------------------------------------------------------------------
# Reviews summariser
# ---------------------------------------------------------------------------


def test_summarize_reviews_changes_requested_vetoes() -> None:
    """A single CHANGES_REQUESTED outranks any APPROVED."""
    reviews = [
        {"user": {"login": "a"}, "state": "APPROVED"},
        {"user": {"login": "b"}, "state": "CHANGES_REQUESTED"},
    ]
    assert _summarize_reviews(reviews) == "changes_requested"


def test_summarize_reviews_approved_when_no_changes_requested() -> None:
    reviews = [
        {"user": {"login": "a"}, "state": "APPROVED"},
        {"user": {"login": "b"}, "state": "COMMENTED"},
    ]
    assert _summarize_reviews(reviews) == "approved"


def test_summarize_reviews_latest_per_reviewer_wins() -> None:
    """Chronological list: the same reviewer's APPROVED supersedes earlier CHANGES_REQUESTED."""
    reviews = [
        {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "a"}, "state": "APPROVED"},
    ]
    assert _summarize_reviews(reviews) == "approved"


def test_summarize_reviews_dismissed_drops_reviewer() -> None:
    """DISMISSED removes the reviewer's prior verdict from the tally."""
    reviews = [
        {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "a"}, "state": "DISMISSED"},
        {"user": {"login": "b"}, "state": "APPROVED"},
    ]
    assert _summarize_reviews(reviews) == "approved"


def test_summarize_reviews_commented_when_no_verdict() -> None:
    reviews = [
        {"user": {"login": "a"}, "state": "COMMENTED"},
    ]
    assert _summarize_reviews(reviews) == "commented"


def test_summarize_reviews_no_reviews() -> None:
    assert _summarize_reviews([]) == "no_reviews"


def test_summarize_reviews_unknown_shape() -> None:
    assert _summarize_reviews(None) == "unknown"
    assert _summarize_reviews("not-a-list") == "unknown"
