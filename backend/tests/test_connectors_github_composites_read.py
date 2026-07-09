# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``gh.composite.pr_status_summary`` (direct-session, #2255).

The composite migrated from ``dispatch_child``-through-ingested-rows to
direct :class:`GitHubRestConnector` session calls (#2255): the handler
declares a ``connector`` parameter and issues its three reads through
``connector._get_json`` against ``connector.mount_op_path``, with no
``endpoint_descriptor`` lookup. These tests drive the handler with a
recording session stub.

Coverage matrix (envelope semantics unchanged from the pre-migration
behaviour -- parity is the acceptance gate):

* Happy path -- three reads fire in the expected order with the right
  wire paths; head SHA flows from the PR read into the check-runs path;
  the aggregated envelope matches the documented shape.
* Parallelism -- the two secondary reads fire concurrently
  (``asyncio.gather``) rather than sequentially.
* Partial-failure tolerance:
  * Checks read raises -> ``checks=None`` + ``checks_status="unknown"``;
    composite still returns the PR + reviews cleanly.
  * Reviews read raises -> ``reviews=None`` + ``review_status="unknown"``;
    composite still returns the PR + checks cleanly.
  * Both secondaries raise -> composite still returns the PR with the
    two None / "unknown" payloads.
* Primary failure -- the PR read raising propagates (the dispatcher's
  outer branch maps it to ``connector_error``); no secondary read runs.
* Malformed PR payload (no head.sha) raises ``RuntimeError``.
* Status summarisers (``_summarize_checks`` / ``_summarize_reviews``)
  collapse the raw arrays into the agent-actionable enum values.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._read import (
    _summarize_checks,
    _summarize_reviews,
    pr_status_summary_composite,
)


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


def _pr_path(owner: str = "evoila", repo: str = "meho", pull_number: int = 754) -> str:
    return f"/repos/{owner}/{repo}/pulls/{pull_number}"


def _checks_path(ref: str, owner: str = "evoila", repo: str = "meho") -> str:
    return f"/repos/{owner}/{repo}/commits/{ref}/check-runs"


def _reviews_path(owner: str = "evoila", repo: str = "meho", pull_number: int = 754) -> str:
    return f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews"


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` the way ``raise_for_status`` would."""
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"http_{status_code}", request=request, response=response)


class _RecordingConnector:
    """Session stub matching the subset of ``GitHubRestConnector`` the handler uses.

    Maps a wire path -> canned JSON payload (or an ``Exception`` to
    raise). Records every ``_get_json`` path so the test can assert
    call-shape and parallelism. ``mount_op_path`` is identity, mirroring
    the github connector's inherited default.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self.barrier: asyncio.Event | None = None
        self._gather_targets: set[str] = set()
        self._in_flight: list[str] = []

    def set_gather_barrier(self, paths: set[str]) -> None:
        """Hold every ``_get_json`` for these paths until all have entered.

        A sequential handler would deadlock on the first call; the
        gather()-based handler unblocks because both secondary reads land
        before either awaits the barrier.
        """
        self.barrier = asyncio.Event()
        self._gather_targets = set(paths)
        self._in_flight = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(path)
        if self.barrier is not None and path in self._gather_targets:
            self._in_flight.append(path)
            if len(self._in_flight) >= len(self._gather_targets):
                self.barrier.set()
            await self.barrier.wait()
        payload = self._responses[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


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
async def test_pr_status_summary_reads_three_sub_ops_in_order() -> None:
    """PR first, then checks + reviews; head SHA flows into the checks path."""
    pr = _pr_payload(head_sha="76065a0")
    checks = _checks_payload(
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "test", "status": "completed", "conclusion": "success"},
    )
    reviews = [
        {"user": {"login": "reviewer-a"}, "state": "APPROVED"},
    ]
    connector = _RecordingConnector(
        {
            _pr_path(): pr,
            _checks_path("76065a0"): checks,
            _reviews_path(): reviews,
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        connector=connector,  # type: ignore[arg-type]
    )
    # PR read fires first; the two secondaries can land in either order
    # (asyncio.gather is not entry-order-preserving).
    assert connector.calls[0] == _pr_path()
    assert set(connector.calls[1:]) == {_checks_path("76065a0"), _reviews_path()}
    # head SHA threaded into the checks path's {ref} slot.
    assert _checks_path("76065a0") in connector.calls
    # Aggregated envelope.
    assert out["pr"] == pr
    assert out["checks"] == checks
    assert out["reviews"] == reviews
    assert out["mergeable"] is True
    assert out["mergeable_state"] == "clean"
    assert out["checks_status"] == "all_passed"
    assert out["review_status"] == "approved"


@pytest.mark.asyncio
async def test_pr_read_path_passes_through() -> None:
    """``owner`` / ``repo`` / ``pull_number`` flow into the PR + reviews paths."""
    connector = _RecordingConnector(
        {
            _pr_path("octocat", "hello-world", 42): _pr_payload(),
            _checks_path("abc123", "octocat", "hello-world"): _checks_payload(),
            _reviews_path("octocat", "hello-world", 42): [],
        }
    )
    await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "octocat", "repo": "hello-world", "pull_number": 42},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.calls[0] == _pr_path("octocat", "hello-world", 42)
    assert _reviews_path("octocat", "hello-world", 42) in connector.calls


# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secondary_reads_run_in_parallel() -> None:
    """The checks + reviews reads fire concurrently via asyncio.gather.

    Set a barrier both secondary reads must enter before either may
    return. A sequential handler would deadlock; the gather()-based
    handler unblocks cleanly because both reads land before either awaits
    the barrier.
    """
    connector = _RecordingConnector(
        {
            _pr_path(): _pr_payload(head_sha="76065a0"),
            _checks_path("76065a0"): _checks_payload(),
            _reviews_path(): [],
        }
    )
    connector.set_gather_barrier({_checks_path("76065a0"), _reviews_path()})
    out = await asyncio.wait_for(
        pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            connector=connector,  # type: ignore[arg-type]
        ),
        timeout=2.0,
    )
    assert out["pr"] is not None


# ---------------------------------------------------------------------------
# Partial-failure tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checks_read_failure_does_not_bail() -> None:
    """A 404 on the checks read surfaces as ``checks=None`` + ``checks_status='unknown'``."""
    connector = _RecordingConnector(
        {
            _pr_path(): _pr_payload(head_sha="76065a0"),
            _checks_path("76065a0"): _http_error(404),
            _reviews_path(): [
                {"user": {"login": "r1"}, "state": "APPROVED"},
            ],
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        connector=connector,  # type: ignore[arg-type]
    )
    assert out["checks"] is None
    assert out["checks_status"] == "unknown"
    # Reviews + PR still flow through cleanly.
    assert out["reviews"] is not None
    assert out["review_status"] == "approved"
    assert out["pr"] is not None


@pytest.mark.asyncio
async def test_reviews_read_failure_does_not_bail() -> None:
    """Reviews read failure surfaces as ``reviews=None`` + ``review_status='unknown'``."""
    connector = _RecordingConnector(
        {
            _pr_path(): _pr_payload(head_sha="76065a0"),
            _checks_path("76065a0"): _checks_payload(
                {"name": "build", "status": "completed", "conclusion": "success"},
            ),
            _reviews_path(): _http_error(401),
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        connector=connector,  # type: ignore[arg-type]
    )
    assert out["reviews"] is None
    assert out["review_status"] == "unknown"
    assert out["checks"] is not None
    assert out["checks_status"] == "all_passed"
    assert out["pr"] is not None


@pytest.mark.asyncio
async def test_both_secondary_reads_failing_still_returns_pr() -> None:
    """If both secondaries fail, the composite still surfaces the PR + 'unknown' states."""
    connector = _RecordingConnector(
        {
            _pr_path(): _pr_payload(head_sha="76065a0"),
            _checks_path("76065a0"): _http_error(429),
            _reviews_path(): _http_error(429),
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        connector=connector,  # type: ignore[arg-type]
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
async def test_pr_read_failure_propagates() -> None:
    """PR read error -> propagates (dispatcher maps it to connector_error)."""
    connector = _RecordingConnector(
        {
            _pr_path(): _http_error(404),
        }
    )
    with pytest.raises(httpx.HTTPStatusError):
        await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            connector=connector,  # type: ignore[arg-type]
        )
    # The composite bailed before issuing the secondary reads.
    assert connector.calls == [_pr_path()]


@pytest.mark.asyncio
async def test_pr_payload_missing_head_sha_raises_runtime_error() -> None:
    """A PR payload with no head.sha cannot drive the checks read -> error."""
    connector = _RecordingConnector(
        {
            _pr_path(): {"number": 754},  # No head -> no head.sha extractable.
        }
    )
    with pytest.raises(RuntimeError, match=r"head\.sha"):
        await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "evoila", "repo": "meho", "pull_number": 754},
            connector=connector,  # type: ignore[arg-type]
        )
    # Only the PR read fired before the malformed-payload bail.
    assert connector.calls == [_pr_path()]


# ---------------------------------------------------------------------------
# Mergeable / mergeable_state pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mergeable_null_is_passed_through_verbatim() -> None:
    """``mergeable=None`` (GitHub still computing) flows through as-is."""
    connector = _RecordingConnector(
        {
            _pr_path(): _pr_payload(head_sha="76065a0", mergeable=None, mergeable_state="unknown"),
            _checks_path("76065a0"): _checks_payload(),
            _reviews_path(): [],
        }
    )
    out = await pr_status_summary_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "evoila", "repo": "meho", "pull_number": 754},
        connector=connector,  # type: ignore[arg-type]
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
