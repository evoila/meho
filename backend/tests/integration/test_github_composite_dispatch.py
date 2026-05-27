# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Live-ingest dispatch acceptance for ``gh.composite.pr_status_summary`` (G3.11-T4 #1224).

Gated on ``MEHO_GH_INGEST_LIVE=1`` (same env var as
``tests/integration/test_operations_ingest_github.py`` from G3.11-T3
#1228). When the env var is unset the test is skipped; when it is set,
the test is currently :func:`pytest.xfail`-marked with ``strict=True``
because the composite-dispatch body itself is not yet wired against a
live backplane.

**STATUS.**

* G3.11-T7 #1241 lifted the upstream parser limitation — the parser
  now inlines ``#/components/responses/*`` and
  ``#/components/requestBodies/*`` refs, so catalog ingest against
  the live GitHub spec succeeds end-to-end.
* What is still xfailed here is T4's own composite-dispatch body:
  the round-trip from ``gh.composite.pr_status_summary`` through
  L2 child ops against a real running backplane + a real GitHub PR
  requires a backplane process up, a GitHub App credential
  provisioned in Vault, and the catalog ingested, none of which the
  unit-test sandbox has. The body raises ``NotImplementedError`` to
  keep ``strict=True`` honest until the dispatch sketch is filled in.

The composite *registration* itself is unblocked -- the unit tests in
``tests/test_connectors_github_composites_register.py`` exercise the
register path without ingest, and the unit tests in
``tests/test_connectors_github_composites_read.py`` cover the handler's
dispatch logic with mocked ``dispatch_child``.

Once the dispatch body is wired (separate follow-up Task on Initiative
G3.11 #1220), this test's ``xfail(strict=True)`` flips to ``xpass`` and
CI fails loudly -- prompting the maintainer to remove the xfail mark
and re-qualify the docstring.

**How to run locally.** With a backplane up, a GitHub App credential
provisioned in Vault, and the catalog ingested::

    MEHO_GH_INGEST_LIVE=1 \\
      uv run --package meho-backplane pytest -q \\
      backend/tests/integration/test_github_composite_dispatch.py

The full ingest round-trip (``POST /api/v1/connectors/ingest`` with
``catalog_entry: gh/v3``) is exercised by the operator runbook in
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
        "Composite-dispatch body not yet wired against a live backplane "
        "(needs backplane up + GitHub App credential in Vault + catalog "
        "ingested). The upstream parser ref-bucket gap that previously "
        "blocked this test was lifted by G3.11-T7 #1241; what remains "
        "is T4's own dispatch sketch. xfail flips to xpass once the "
        "body is filled in -- remove the mark + this rationale then."
    ),
)
def test_pr_status_summary_dispatches_against_live_pr() -> None:
    """Composite end-to-end dispatch against a real GitHub PR.

    The intended shape, once the dispatch body is wired:

    1. Ingest the catalog entry ``gh/v3`` (registers ~700 L2 ops).
    2. Dispatch ``gh.composite.pr_status_summary`` with ``owner=evoila,
       repo=meho, pull_number=754`` (or another live PR on a repo the
       App can read).
    3. Assert the result envelope carries ``pr``, ``checks``, ``reviews``,
       ``mergeable``, ``mergeable_state``, ``checks_status``, and
       ``review_status`` keys.
    4. Assert ``checks_status`` is one of the enum values from the
       response schema (not "unknown" -- the live PR has CI configured).

    Until the dispatch body is filled in, this body short-circuits via
    the ``xfail(strict=True)`` mark above so the test documents the
    dependency without claiming to pass.
    """
    # When the dispatch body lands, this body fills in with the
    # dispatch + assertions sketched above. For now, the xfail mark
    # short-circuits the test; we still raise here to make
    # strict=True meaningful.
    raise NotImplementedError(
        "gh.composite.pr_status_summary live dispatch body is not yet "
        "wired; see the module docstring for the dependency + run "
        "instructions."
    )
