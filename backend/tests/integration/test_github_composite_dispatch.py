# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Live-ingest dispatch acceptance for ``gh.composite.pr_status_summary`` (G3.11-T4 #1224).

Gated on ``MEHO_GH_INGEST_LIVE=1`` (same env var as
``tests/integration/test_operations_ingest_github.py`` from G3.11-T3
#1228). When the env var is unset the test is skipped; when it is set,
the test is currently :func:`pytest.xfail`-marked with ``strict=True``
because of an upstream parser limitation documented below.

**KNOWN PARSER-LIMITATION DEPENDENCY.**

The G0.7 OpenAPI parser at
``backend/src/meho_backplane/operations/ingest/refs.py`` only inlines
``#/components/schemas/*`` and ``#/components/parameters/*`` ``$ref``
shapes. The GitHub REST spec uses ``#/components/responses/*`` refs
extensively (e.g. ``#/components/responses/accepted``), and the parser
raises ``UnsupportedSpecError`` on the first one. This blocks T4's
acceptance criterion #1 ("``gh.composite.pr_status_summary`` registered
+ dispatchable") from running end-to-end against the live spec until a
sibling parser-scope follow-up Task lifts the ref-bucket coverage.

The composite *registration* itself is not blocked -- the unit tests in
``tests/test_connectors_github_composites_register.py`` exercise the
register path without ingest, and the unit tests in
``tests/test_connectors_github_composites_read.py`` cover the handler's
dispatch logic with mocked ``dispatch_child``. What is xfailed here is
the *L1-on-top-of-L2 dispatch* round-trip against a real running
backplane + a real GitHub PR.

Once the parser-scope follow-up lands and the catalog ingests cleanly,
this test's ``xfail(strict=True)`` flips to ``xpass`` and CI fails
loudly -- prompting the maintainer to remove the xfail mark and re-
qualify the docstring. That's the desired hand-off shape: the test
self-advertises when its blocker has lifted.

**How to run locally.** Once the parser fix lands, with a backplane up,
a GitHub App credential provisioned in Vault, and the catalog ingested::

    MEHO_GH_INGEST_LIVE=1 \\
      uv run --package meho-backplane pytest -q \\
      backend/tests/integration/test_github_composite_dispatch.py

The full ingest round-trip (``POST /api/v1/connectors/ingest`` with
``catalog_entry: gh/3``) is exercised by the operator runbook in
``docs/cross-repo/github-connector.md`` (G3.11-T6); this test only
covers the composite-dispatch leg downstream of that.
"""

from __future__ import annotations

import os

import pytest


def _live_ingest_opted_in() -> bool:
    """Return ``True`` when ``MEHO_GH_INGEST_LIVE=1`` is set."""
    return os.getenv("MEHO_GH_INGEST_LIVE") == "1"


@pytest.mark.skipif(
    not _live_ingest_opted_in(),
    reason=(
        "GitHub live composite dispatch gated by MEHO_GH_INGEST_LIVE=1 "
        "(G3.11-T4 #1224 acceptance criterion #2). Unit tests cover the "
        "composite handler + register paths; this test verifies "
        "end-to-end L1+L2 dispatch against a live spec ingest."
    ),
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "G0.7 parser only inlines #/components/schemas/* and "
        "#/components/parameters/* refs; the GitHub spec uses "
        "#/components/responses/* refs and the parser raises "
        "UnsupportedSpecError on ingest. Out of scope for T4 (composite "
        "implementation); a sibling parser-scope follow-up lifts the "
        "ref-bucket coverage. xfail flips to xpass once that lands -- "
        "remove the mark + this rationale block then."
    ),
)
def test_pr_status_summary_dispatches_against_live_pr() -> None:
    """Composite end-to-end dispatch against a real GitHub PR.

    The intended shape, once the parser fix lands:

    1. Ingest the catalog entry ``gh/3`` (registers ~700 L2 ops).
    2. Dispatch ``gh.composite.pr_status_summary`` with ``owner=evoila,
       repo=meho, pull_number=754`` (or another live PR on a repo the
       App can read).
    3. Assert the result envelope carries ``pr``, ``checks``, ``reviews``,
       ``mergeable``, ``mergeable_state``, ``checks_status``, and
       ``review_status`` keys.
    4. Assert ``checks_status`` is one of the enum values from the
       response schema (not "unknown" -- the live PR has CI configured).

    Until the parser limitation is lifted, this body short-circuits via
    the ``xfail(strict=True)`` mark above so the test documents the
    dependency without claiming to pass.
    """
    # When the parser fix lands and the catalog ingests cleanly, this
    # body fills in with the dispatch + assertions sketched above. For
    # now, the xfail mark short-circuits the test; we still raise here
    # to make strict=True meaningful (the body fails until the parser
    # is fixed).
    raise NotImplementedError(
        "gh.composite.pr_status_summary live dispatch is xfailed pending "
        "G0.7 parser support for #/components/responses/* refs; see the "
        "module docstring for the dependency + run instructions."
    )
