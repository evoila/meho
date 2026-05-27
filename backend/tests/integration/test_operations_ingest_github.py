# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :func:`parse_openapi` against the live GitHub REST spec.

Skipped in CI unless ``MEHO_GH_INGEST_LIVE=1`` is set (G3.11-T3 #1223
acceptance criterion line 3 — "skip in CI unless env-var set,
document how to run locally"). The unit-test fixtures cover the parser
contract; this test only asserts the parser scales to the real
GitHub OpenAPI spec corpus per the Initiative's acceptance criterion
("~700 endpoint_descriptor rows landed").

G3.11-T7 #1241 lifted the parser ref-bucket gap: the parser now
inlines ``#/components/responses/*`` and
``#/components/requestBodies/*`` refs alongside the existing schemas
and parameters paths. This test was previously ``xfail(strict=True)``
gated on that limitation; once the parser fix landed, the xfail was
removed and the test runs cleanly under the env-var gate.

**Note on downstream dispatch (out of scope here).** The T1↔T3
version-string drift (catalog ``version: v3`` vs registry
``version: "3"``) means that a full
``meho connector ingest --catalog gh/v3 --dry-run`` end-to-end run
will succeed at parsing but currently misses the connector-class
lookup. That's tracked under G3.11-T8 (#1242). This integration
test verifies the parsing layer only — row count + spot-checks on
the parsed descriptors — so it is independent of that drift.

**How to run locally.** The GitHub OpenAPI spec is public and direct-
resolvable (no auth required for the spec itself):

.. code-block:: shell

    MEHO_GH_INGEST_LIVE=1 \\
      uv run --package meho-backplane pytest -q \\
      backend/tests/integration/test_operations_ingest_github.py

Optional: ``MEHO_GH_OPENAPI`` overrides the upstream URL (useful for
canary-against-a-pinned-SHA or testing against a fork). When unset, the
test fetches the same URL the catalog ships
(``raw.githubusercontent.com/github/rest-api-description/main/...``).

The full ingest round-trip against a running backplane (``POST
/api/v1/connectors/ingest`` with ``catalog_entry: gh/v3``, asserting
``staged_count >= 700`` and ``review_status=staged``) is exercised by
the operator runbook in ``docs/cross-repo/github-connector.md``
(G3.11-T6) -- it requires a backplane + DB + GitHub App credential
chain end-to-end and is out of scope for the unit/integration test
boundary.
"""

from __future__ import annotations

import os

import pytest

from meho_backplane.operations.ingest import parse_openapi

# Default to the catalog-shipped upstream. Override via env var for
# pinned-SHA / fork canaries.
_DEFAULT_GH_OPENAPI = (
    "https://raw.githubusercontent.com/github/rest-api-description/"
    "main/descriptions/api.github.com/api.github.com.json"
)


def _resolve_gh_spec() -> str | None:
    """Return the spec URL when live ingest is opted in, else ``None``.

    Opt-in is the env var ``MEHO_GH_INGEST_LIVE=1`` (G3.11-T3 #1223
    acceptance line 3). The spec URL itself can be overridden via
    ``MEHO_GH_OPENAPI`` for pinned-SHA / fork canaries; otherwise the
    catalog-shipped upstream is used.
    """
    if os.getenv("MEHO_GH_INGEST_LIVE") != "1":
        return None
    return os.getenv("MEHO_GH_OPENAPI") or _DEFAULT_GH_OPENAPI


@pytest.mark.skipif(
    _resolve_gh_spec() is None,
    reason=(
        "GitHub spec live ingest gated by MEHO_GH_INGEST_LIVE=1 "
        "(G3.11-T3 #1223 AC line 3). Unit tests cover the parser "
        "contract; this only verifies the parser scales to the live "
        "~10 MB GitHub REST spec corpus."
    ),
)
def test_parse_github_rest_spec_lands_700_plus_rows() -> None:
    """Live ingest acceptance: ~700 paths land as endpoint descriptors.

    Asserts the parser scales to the GitHub REST API v3 spec and that
    the spec's path coverage matches the catalog entry's "~700 paths
    grouped into ~40 tags" claim. Spot-checks per the G0.7 safety-level
    convention: GET -> safe, POST -> caution, DELETE -> dangerous.

    The 700 lower bound tracks the AC's "~700 paths" wording (the live
    spec carries ~784 paths as of 2026-05-27; the bound has ~10%
    headroom for upstream churn).
    """
    spec_url = _resolve_gh_spec()
    assert spec_url is not None  # guarded by skipif above
    rows = parse_openapi(spec_url, spec_source="spec:gh/v3")
    distinct_paths = {row.path for row in rows}
    assert len(rows) >= 700, f"got {len(rows)} rows; acceptance threshold is 700"
    assert len(distinct_paths) >= 600, (
        f"got {len(distinct_paths)} distinct paths; acceptance threshold is 600"
    )
    # Spot-check method -> safety_level mapping is wired (G0.7 contract).
    safe = [r for r in rows if r.method == "GET"]
    caution = [r for r in rows if r.method == "POST"]
    dangerous = [r for r in rows if r.method == "DELETE"]
    assert safe, "GitHub REST spec must have at least one GET"
    assert caution, "GitHub REST spec must have at least one POST"
    assert dangerous, "GitHub REST spec must have at least one DELETE"
    assert all(r.safety_level == "safe" for r in safe[:5])
    assert all(r.safety_level == "caution" for r in caution[:5])
    assert all(r.safety_level == "dangerous" for r in dangerous[:5])
    # spec_source threading.
    assert all("spec:gh/v3" in row.tags for row in rows)
