# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Live composite dispatch acceptance for ``gh.composite.pr_status_summary``.

G3.11-T4 #1224 shipped the composite registration surface + handler
body; G3.11-T7 #1241 unblocked live ingest of the GitHub OpenAPI spec;
G3.11-T8 #1242 reconciled the catalog/registry ``version`` chain;
G3.11-T9 #1252 (this file's last revision) flipped this test from
``xfail`` to a real live-dispatch acceptance against the public GitHub
REST API.

The test exercises :func:`pr_status_summary_composite` end-to-end
against a real GitHub PR. It plugs an HTTPX-backed ``dispatch_child``
into the handler so the composite's three sub-calls hit the real
upstream (PR get / head-commit check runs / PR reviews). The L2 pre-
flight is primed in-process so the test does not depend on a running
backplane or a populated ``endpoint_descriptor`` table — the cost of a
full backplane bring-up far exceeds what an in-process live test should
require, and the parser-level acceptance for the catalog round-trip is
already covered by ``tests/integration/test_operations_ingest_github.py``
(G3.11-T3 #1228) + the operator runbook in
``docs/cross-repo/github-connector.md`` (G3.11-T6).

What this verifies
==================

1. The composite handler issues three HTTP calls against the real
   GitHub REST API in the documented order (PR first, then checks +
   reviews in parallel via :func:`asyncio.gather`).
2. The head SHA extracted from the PR sub-call threads correctly into
   the check-runs sub-call's ``ref`` param.
3. The aggregated envelope carries the seven documented keys (``pr``,
   ``checks``, ``reviews``, ``mergeable``, ``mergeable_state``,
   ``checks_status``, ``review_status``).
4. ``checks_status`` / ``review_status`` evaluate to one of the
   schema-allowed enum values against real upstream payload shape (not
   the unit-test mock shape).
5. Partial-failure tolerance survives a real upstream — the test does
   not assert a specific check-runs / reviews disposition (live PR
   state drifts) but does assert the keys are present and the summaries
   are in their enum range.

Gates
=====

The test runs only when **both** of:

* ``MEHO_GH_INGEST_LIVE=1`` -- the project-wide opt-in for live-GitHub
  tests (same gate as ``test_operations_ingest_github.py``).
* ``MEHO_GH_LIVE_PR`` -- ``owner/repo#number`` for a public PR the
  unauthenticated client can read (e.g. ``evoila/meho#754``). Live PR
  state drifts (checks turn green, reviews land), so the upstream is
  pinned via env var rather than hardcoded; the operator running the
  smoke chooses a PR appropriate for the moment.

Optional:

* ``GITHUB_TOKEN`` -- a personal access token or fine-grained token.
  When set, every sub-call carries ``Authorization: token <value>`` so
  the test runs under the authenticated rate-limit (5000/hr) instead
  of the IP-shared 60/hr. Unauthenticated runs work fine for a single
  PR; the token is only needed when running the smoke in a loop or on
  shared CI infrastructure.

How to run locally
==================

.. code-block:: shell

    # Unauthenticated, against a known public PR:
    MEHO_GH_INGEST_LIVE=1 MEHO_GH_LIVE_PR=evoila/meho#754 \\
      uv run --package meho-backplane pytest -q \\
      backend/tests/integration/test_github_composite_dispatch.py

    # Authenticated (avoids the 60/hr unauthenticated cap):
    MEHO_GH_INGEST_LIVE=1 MEHO_GH_LIVE_PR=evoila/meho#754 \\
      GITHUB_TOKEN=ghp_xxx \\
      uv run --package meho-backplane pytest -q \\
      backend/tests/integration/test_github_composite_dispatch.py

The full ingest + dispatch round-trip via the REST API (``POST
/api/v1/connectors/ingest`` with ``catalog_entry: gh/3`` followed by
``POST /api/v1/operations/dispatch`` with the composite op_id) is
exercised by the operator runbook in
``docs/cross-repo/github-connector.md`` (G3.11-T6) -- that path
requires a backplane process + DB + GitHub App credential chain end-
to-end and is out of scope for the integration-test boundary.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.github.composites import _preflight
from meho_backplane.connectors.github.composites._read import (
    _OP_GET_CHECK_RUNS,
    _OP_GET_PULL,
    _OP_LIST_REVIEWS,
    pr_status_summary_composite,
)

# GitHub REST API base. Pinned by convention; no env override on
# purpose -- if you need to talk to a different host (GHES), file a
# follow-up task to thread a base-url config rather than letting tests
# drift via env var.
_GITHUB_API_BASE = "https://api.github.com"

# Composite envelope keys -- duplicated from the response schema rather
# than imported to keep this test's "envelope contract" assertion
# self-contained (a schema-edit that drops a key must update the test
# explicitly).
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "pr",
        "checks",
        "reviews",
        "mergeable",
        "mergeable_state",
        "checks_status",
        "review_status",
    }
)

# Enum ranges -- pinned from the response schema's enum constraints.
# When the schema changes, this assertion's blast-radius is one file.
_CHECKS_STATUS_VALUES: frozenset[str] = frozenset(
    {"all_passed", "any_failed", "pending", "no_checks", "unknown"}
)
_REVIEW_STATUS_VALUES: frozenset[str] = frozenset(
    {"approved", "changes_requested", "commented", "pending", "no_reviews", "unknown"}
)


def _resolve_live_pr() -> tuple[str, str, int] | None:
    """Parse ``MEHO_GH_LIVE_PR`` into ``(owner, repo, pull_number)``.

    Returns ``None`` when the env var is unset (the test skips). Raises
    :class:`ValueError` when set but malformed -- bad config is louder
    than silently-skipped tests.
    """
    raw = os.getenv("MEHO_GH_LIVE_PR")
    if not raw:
        return None
    # Expected form: ``owner/repo#number`` (matches the GitHub UI's
    # short-link form, e.g. ``evoila/meho#754``).
    repo_part, _, number_part = raw.partition("#")
    if not number_part:
        raise ValueError(f"MEHO_GH_LIVE_PR={raw!r}: expected 'owner/repo#number' form")
    owner, _, repo = repo_part.partition("/")
    if not owner or not repo:
        raise ValueError(f"MEHO_GH_LIVE_PR={raw!r}: missing owner/repo prefix")
    try:
        pull_number = int(number_part)
    except ValueError as exc:
        raise ValueError(f"MEHO_GH_LIVE_PR={raw!r}: pull number is not an integer") from exc
    if pull_number < 1:
        raise ValueError(f"MEHO_GH_LIVE_PR={raw!r}: pull number must be >= 1")
    return owner, repo, pull_number


def _live_ingest_opted_in() -> bool:
    """Return ``True`` when ``MEHO_GH_INGEST_LIVE=1`` is set."""
    return os.getenv("MEHO_GH_INGEST_LIVE") == "1"


def _gates_satisfied() -> bool:
    """Both opt-in env vars must be set for the test to run."""
    if not _live_ingest_opted_in():
        return False
    try:
        return _resolve_live_pr() is not None
    except ValueError:
        # Malformed MEHO_GH_LIVE_PR -- let the test body run so it
        # raises explicitly rather than masking the typo as a skip.
        return True


def _make_operator() -> Operator:
    """Synthetic operator for the in-process live composite dispatch.

    The composite handler reads only ``operator.tenant_id`` (passed to
    the pre-flight cache); the raw JWT is not exercised because the
    test wires a custom ``dispatch_child`` that bypasses the
    dispatcher's auth/policy chain.
    """
    return Operator(
        sub="op-gh-composite-live",
        name="GH Composite Live Dispatch Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _build_url(op_id: str, params: dict[str, Any]) -> str:
    """Render a GitHub REST URL from a path-template op_id + params.

    The op_id is the parser's METHOD:/path form -- the same shape the
    composite passes to ``dispatch_child``. The path template carries
    ``{owner}`` / ``{repo}`` / ``{pull_number}`` / ``{ref}`` placeholders
    which expand from the param dict.
    """
    _, _, path_template = op_id.partition(":")
    path = path_template.format(**params)
    return f"{_GITHUB_API_BASE}{path}"


def _auth_headers() -> dict[str, str]:
    """Return GitHub auth headers when ``GITHUB_TOKEN`` is set.

    Unauthenticated is fine for a single public PR (the 60/hr IP rate
    limit covers one test run easily); a token avoids the cap when
    running the smoke in a loop or against a private PR.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "meho-backplane-integration-test",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


class _HttpxDispatchChild:
    """``dispatch_child`` implementation that hits the real GitHub REST API.

    Bypasses the dispatcher's auth/policy chain (no JWT validation, no
    audit row, no broadcast) -- this is an integration test for the
    composite handler's logic against real upstream payloads, not a
    test of the dispatcher itself (covered separately by the unit-test
    suite). The handler's contract with ``dispatch_child`` is the
    :class:`DispatchChild` Protocol; this implementation satisfies it.

    Failures from upstream surface as ``OperationResult(status="error",
    error=...)`` so the composite's partial-failure-tolerance branches
    run against real 4xx / 5xx shapes.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        self.calls.append({"connector_id": connector_id, "op_id": op_id, "params": dict(params)})
        url = _build_url(op_id, params)
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            return OperationResult(
                status="error",
                op_id=op_id,
                error=f"transport_error: {exc!r}",
                duration_ms=0.0,
            )
        if response.status_code >= 400:
            return OperationResult(
                status="error",
                op_id=op_id,
                error=f"http_{response.status_code}: {response.text[:200]}",
                duration_ms=0.0,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            return OperationResult(
                status="error",
                op_id=op_id,
                error=f"non_json_body: {exc!r}",
                duration_ms=0.0,
            )
        return OperationResult(
            status="ok",
            op_id=op_id,
            result=payload,
            duration_ms=0.0,
        )


@pytest.fixture
def _prime_preflight_cache() -> Any:
    """Prime the L2 pre-flight cache so dispatch skips the DB walk.

    The composite's pre-flight resolves L2 sub-op presence via a DB
    lookup at first dispatch and caches the result process-wide. This
    test runs in-process without a DB, so we prime the cache directly
    -- the dispatch leg is what the test exercises, not the pre-flight
    leg (the pre-flight is covered by
    ``test_connectors_github_composites_l2_preflight.py``).
    """
    _preflight.reset_preflight_cache()
    _preflight._PREFLIGHT_CACHE.add("gh.composite.pr_status_summary")
    yield
    _preflight.reset_preflight_cache()


@pytest.mark.skipif(
    not _gates_satisfied(),
    reason=(
        "GitHub live composite dispatch gated by MEHO_GH_INGEST_LIVE=1 "
        "+ MEHO_GH_LIVE_PR=<owner/repo#number>. Unit tests cover the "
        "composite handler with mocked dispatch_child; this verifies "
        "the handler against real GitHub REST API responses."
    ),
)
@pytest.mark.asyncio
async def test_pr_status_summary_dispatches_against_live_pr(
    _prime_preflight_cache: None,
) -> None:
    """Composite end-to-end dispatch against a real GitHub PR.

    Acceptance, mirroring G3.11-T9 #1252:

    1. The handler issues three HTTP requests in the documented order
       (PR sub-call first; checks + reviews in parallel afterwards).
    2. The head SHA from the PR payload threads into the check-runs
       call's ``ref`` param.
    3. The envelope carries all seven schema-defined keys.
    4. ``checks_status`` / ``review_status`` are within their enum
       ranges (the exact value depends on live PR state and is not
       asserted; the test pins only structural contracts).

    Negative cases (PR sub-call 404 -> RuntimeError; checks 404 ->
    graceful degradation) are not exercised here because they cannot
    be triggered deterministically against a live PR. They are covered
    by the unit-test suite in
    ``tests/test_connectors_github_composites_read.py``.
    """
    spec = _resolve_live_pr()
    assert spec is not None  # _gates_satisfied() guarded
    owner, repo, pull_number = spec

    async with httpx.AsyncClient(
        headers=_auth_headers(),
        timeout=httpx.Timeout(30.0),
    ) as client:
        dispatch = _HttpxDispatchChild(client)
        envelope = await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": owner, "repo": repo, "pull_number": pull_number},
            dispatch_child=dispatch,
        )

    # ----- Sub-call ordering + param threading -----
    assert dispatch.calls, "composite must issue at least the PR sub-call"
    assert dispatch.calls[0]["op_id"] == _OP_GET_PULL, (
        "PR sub-call must fire first (drives the head SHA used by checks)"
    )
    secondary_op_ids = {c["op_id"] for c in dispatch.calls[1:]}
    assert secondary_op_ids == {_OP_GET_CHECK_RUNS, _OP_LIST_REVIEWS}, (
        f"expected the two secondary sub-calls, got {secondary_op_ids!r}"
    )
    checks_call = next(c for c in dispatch.calls if c["op_id"] == _OP_GET_CHECK_RUNS)
    assert "ref" in checks_call["params"], "check-runs call must carry a 'ref' param"
    assert checks_call["params"]["ref"], "head SHA must be non-empty"
    assert checks_call["params"]["owner"] == owner
    assert checks_call["params"]["repo"] == repo

    # ----- Envelope shape -----
    assert set(envelope.keys()) == _ENVELOPE_KEYS, (
        f"envelope keys drift from schema: got {set(envelope.keys())!r}, want {_ENVELOPE_KEYS!r}"
    )
    assert envelope["pr"] is not None, "PR payload is required"
    assert isinstance(envelope["pr"], dict)
    # Head SHA threading is provable from the PR payload too:
    pr_head_sha = envelope["pr"].get("head", {}).get("sha")
    assert pr_head_sha == checks_call["params"]["ref"], (
        f"head SHA in PR payload ({pr_head_sha!r}) must match the SHA "
        f"threaded into the check-runs call ({checks_call['params']['ref']!r})"
    )

    # ----- Summary enums in range -----
    assert envelope["checks_status"] in _CHECKS_STATUS_VALUES, (
        f"checks_status={envelope['checks_status']!r} is outside the "
        f"schema-allowed enum {_CHECKS_STATUS_VALUES!r}"
    )
    assert envelope["review_status"] in _REVIEW_STATUS_VALUES, (
        f"review_status={envelope['review_status']!r} is outside the "
        f"schema-allowed enum {_REVIEW_STATUS_VALUES!r}"
    )

    # ----- mergeable / mergeable_state pass-through is tri-state -----
    # GitHub computes ``mergeable`` asynchronously; right after a push
    # the response carries ``mergeable=None`` until the background job
    # runs. We assert the type contract (bool or None) without pinning
    # a value.
    assert envelope["mergeable"] is None or isinstance(envelope["mergeable"], bool)
    assert envelope["mergeable_state"] is None or isinstance(envelope["mergeable_state"], str)
