#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render the docs-site feature-maturity index from the registry (#2678).

Emits ``docs-site/reference/maturity.md`` — the public per-feature
road-to-prod-ready roadmap the #2664 program requires — entirely from
:data:`meho_backplane.features.FEATURE_MATURITY`. The page is
**committed**, not built on the fly: the docs-site CI job installs only
the ``docs`` dependency group (``uv sync --locked --only-group docs``,
see ``.github/workflows/docs-site.yml``), so an mkdocs hook importing
``meho_backplane`` would drag the full backend dependency tree into
every docs build and tag deploy. Instead this script regenerates the
page inside the backend env, and the freshness gate in
:mod:`tests.test_maturity_surface_drift` fails the unit lane whenever
the committed page and the registry disagree — the same
committed-derived-artifact shape as ``cli/api/openapi.json`` and its
``cli-api-snapshot-freshness`` CI job.

Per-feature headings are the **raw registry keys** (the Python-Markdown
toc slugifier preserves them verbatim, underscores included), so the
#2677 ``/ui`` badge chips can deep-link
``…/reference/maturity/#<feature-key>`` — pinned by the anchor test in
the same drift-guard module.

Run from ``backend/``::

    uv run python scripts/generate_maturity_index.py
"""

from __future__ import annotations

from pathlib import Path

from meho_backplane.features import FEATURE_MATURITY, MaturityInfo

#: Where the rendered page lands, relative to the repo root.
INDEX_RELPATH = "docs-site/reference/maturity.md"

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Tier semantics + promotion entry criteria, verbatim from the #2664
#: initiative. Static prose — the per-feature data around it is what
#: derives from the registry.
_TIER_CRITERIA: dict[str, str] = {
    "ga": (
        "Carries the 1.0 stability promise. Entry criteria: clean-room "
        "eval score ≥ 4 for both usefulness and correctness; works on "
        "both credential backends where applicable; contract surfaces "
        "under the [#2662](https://github.com/evoila/meho/issues/2662) "
        "stability gates; a docs task-guide page; no open P1s."
    ),
    "beta": (
        "Works end-to-end somewhere real; gaps are known and tracked "
        "on the feature's tracking issue (its promotion gate); "
        "surfaces may change with a deprecation notice."
    ),
    "experimental": (
        "May change or vanish without notice; sits outside the 1.0 "
        "stability promise. No committed GA milestone unless stated."
    ),
}


def _issue_ref(tracking_url: str) -> str:
    """Render a tracking-issue URL as a ``[#N](url)`` markdown link."""
    return f"[#{tracking_url.rstrip('/').rsplit('/', 1)[-1]}]({tracking_url})"


def _tier_features(tier: str) -> list[tuple[str, MaturityInfo]]:
    """The registry's entries for *tier*, sorted for deterministic output."""
    return sorted((key, info) for key, info in FEATURE_MATURITY.items() if info["maturity"] == tier)


def _non_ga_section(title: str, tier: str) -> list[str]:
    """Render one non-GA tier: summary table + per-feature anchor sections."""
    features = _tier_features(tier)
    lines = [
        f"## {title}",
        "",
        _TIER_CRITERIA[tier],
        "",
        "| Feature | Target GA | Gaps & promotion gate |",
        "| --- | --- | --- |",
    ]
    for key, info in features:
        target = info.get("target_ga") or "_none committed_"
        lines.append(f"| [{key}](#{key}) | {target} | {_issue_ref(info['tracking'])} |")
    for key, info in features:
        target = info.get("target_ga")
        lines += [
            "",
            f"### {key}",
            "",
            f"- **Tier:** {tier}",
            "- **Target GA milestone:** "
            + (target if target else "none committed — outside the 1.0 promise"),
            "- **Gaps & promotion gate:** known gaps and the road to "
            f"promotion are tracked in {_issue_ref(info['tracking'])}.",
        ]
    lines.append("")
    return lines


def render_maturity_index() -> str:
    """Render the full maturity-index page from the registry."""
    ga_features = _tier_features("ga")
    lines = [
        "<!--",
        "  GENERATED FILE — do not edit by hand (#2678).",
        "  Source of truth: backend/src/meho_backplane/features.py",
        "  (FEATURE_MATURITY). Regenerate from backend/ with:",
        "      uv run python scripts/generate_maturity_index.py",
        "  The freshness gate in",
        "  backend/tests/test_maturity_surface_drift.py fails CI when",
        "  this page and the registry disagree.",
        "-->",
        "",
        "# Feature maturity index",
        "",
        "Every MEHO feature carries an explicit maturity tier — **GA**, "
        "**Beta**, or **Experimental** — declared once in the "
        "feature-maturity registry ([`backend/src/meho_backplane/"
        "features.py`](https://github.com/evoila/meho/blob/main/"
        "backend/src/meho_backplane/features.py)) "
        "and propagated to every surface you touch: `[beta]` / "
        "`[experimental]` prefixes on MCP tool descriptions, "
        "`x-maturity` in the REST OpenAPI document, CLI help labels, "
        "and the `/ui` console's area badges (which link to this page). "
        "This page is the road-to-prod-ready roadmap for every non-GA "
        "feature: what tier it is in, the milestone it targets, and the "
        "issue where its gaps and promotion gate are tracked.",
        "",
        "The classification is the provisional "
        "[#2664](https://github.com/evoila/meho/issues/2664) table, "
        "pending clean-room eval round 1 "
        "([#2665](https://github.com/evoila/meho/issues/2665)).",
        "",
        "## GA features",
        "",
        _TIER_CRITERIA["ga"],
        "",
    ]
    lines += [f"- `{key}`" for key, _info in ga_features]
    lines.append("")
    lines += _non_ga_section("Beta features", "beta")
    lines += _non_ga_section("Experimental features", "experimental")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    """Write the rendered page to :data:`INDEX_RELPATH`."""
    target = _REPO_ROOT / INDEX_RELPATH
    target.write_text(render_maturity_index(), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
