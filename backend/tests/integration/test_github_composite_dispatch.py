# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Live composite dispatch acceptance for ``gh.composite.pr_status_summary``.

G3.11-T4 #1224 shipped the composite registration surface + handler
body; G3.11-T7 #1241 unblocked live ingest of the GitHub OpenAPI spec;
G3.11-T9 #1252 flipped this test from ``xfail`` to a real live-dispatch
acceptance; #2255 migrated the composite to **direct connector-session
dispatch** — the handler declares a ``connector`` parameter and issues
its three reads through the connector's own session
(``connector._get_json`` against ``connector.mount_op_path``) with no
``endpoint_descriptor`` lookup. This test plugs an HTTPX-backed session
stub into the handler so the composite's three reads hit the real
upstream (PR get / head-commit check runs / PR reviews).

No catalog ingest and no ``endpoint_descriptor`` table is required — the
whole point of the migration is that the composite works on a fresh
deploy with no gh catalog ingest (the #2050 defect is gone).

What this verifies
==================

1. The composite handler issues three HTTP calls against the real
   GitHub REST API in the documented order (PR first, then checks +
   reviews in parallel via :func:`asyncio.gather`).
2. The head SHA extracted from the PR sub-call threads correctly into
   the check-runs sub-call's ``{ref}`` path segment.
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
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._read import (
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

    The stubbed session ignores the operator identity (auth is handled by
    the HTTPX client's headers, not a Vault credential read); the handler
    only forwards ``operator`` into ``_get_json`` / ``mount_op_path``.
    """
    return Operator(
        sub="op-gh-composite-live",
        name="GH Composite Live Dispatch Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


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


class _HttpxSessionConnector:
    """Session stub matching the ``GitHubRestConnector`` surface the handler uses.

    Satisfies the #2255 direct-session contract: ``mount_op_path`` is
    identity (github.com mounts descriptor paths verbatim) and
    ``_get_json`` GETs the real GitHub REST API and calls
    ``raise_for_status`` so the handler's partial-failure-tolerance
    branches run against real 4xx / 5xx shapes -- exactly what the
    connector's own ``_get_json`` does.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.calls: list[str] = []

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
        response = await self._client.get(f"{_GITHUB_API_BASE}{path}", params=params)
        response.raise_for_status()
        return response.json()


@pytest.mark.skipif(
    not _gates_satisfied(),
    reason=(
        "GitHub live composite dispatch gated by MEHO_GH_INGEST_LIVE=1 "
        "+ MEHO_GH_LIVE_PR=<owner/repo#number>. Unit tests cover the "
        "composite handler with a mocked connector session; this verifies "
        "the handler against real GitHub REST API responses."
    ),
)
@pytest.mark.asyncio
async def test_pr_status_summary_dispatches_against_live_pr() -> None:
    """Composite end-to-end dispatch against a real GitHub PR (direct-session).

    Acceptance:

    1. The handler issues three HTTP requests in the documented order
       (PR sub-call first; checks + reviews in parallel afterwards).
    2. The head SHA from the PR payload threads into the check-runs
       call's ``{ref}`` path segment.
    3. The envelope carries all seven schema-defined keys.
    4. ``checks_status`` / ``review_status`` are within their enum
       ranges (the exact value depends on live PR state and is not
       asserted; the test pins only structural contracts).

    Negative cases (PR sub-call 404 -> propagates; checks 404 ->
    graceful degradation) are not exercised here because they cannot
    be triggered deterministically against a live PR. They are covered
    by the unit-test suite in
    ``tests/test_connectors_github_composites_read.py``.
    """
    spec = _resolve_live_pr()
    assert spec is not None  # _gates_satisfied() guarded
    owner, repo, pull_number = spec

    pr_path = f"/repos/{owner}/{repo}/pulls/{pull_number}"
    reviews_path = f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews"

    async with httpx.AsyncClient(
        headers=_auth_headers(),
        timeout=httpx.Timeout(30.0),
    ) as client:
        connector = _HttpxSessionConnector(client)
        envelope = await pr_status_summary_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": owner, "repo": repo, "pull_number": pull_number},
            connector=connector,  # type: ignore[arg-type]
        )

    # ----- Sub-call ordering + head-SHA path threading -----
    assert connector.calls, "composite must issue at least the PR sub-call"
    assert connector.calls[0] == pr_path, (
        "PR sub-call must fire first (drives the head SHA used by checks)"
    )
    secondary_paths = connector.calls[1:]
    assert reviews_path in secondary_paths, "reviews sub-call must fire"
    checks_paths = [
        p
        for p in secondary_paths
        if p.startswith(f"/repos/{owner}/{repo}/commits/") and p.endswith("/check-runs")
    ]
    assert len(checks_paths) == 1, f"expected exactly one check-runs call, got {secondary_paths!r}"
    # Head SHA threading is provable from the PR payload vs the checks path.
    pr_head_sha = envelope["pr"].get("head", {}).get("sha")
    assert pr_head_sha, "PR payload must carry a head SHA"
    assert checks_paths[0] == f"/repos/{owner}/{repo}/commits/{pr_head_sha}/check-runs", (
        f"head SHA in PR payload ({pr_head_sha!r}) must match the SHA "
        f"threaded into the check-runs path ({checks_paths[0]!r})"
    )

    # ----- Envelope shape -----
    assert set(envelope.keys()) == _ENVELOPE_KEYS, (
        f"envelope keys drift from schema: got {set(envelope.keys())!r}, want {_ENVELOPE_KEYS!r}"
    )
    assert envelope["pr"] is not None, "PR payload is required"
    assert isinstance(envelope["pr"], dict)

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
