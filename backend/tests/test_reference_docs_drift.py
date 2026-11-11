# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Freshness + public-safety gate for the generated docs-site reference pages.

``docs-site/reference/connectors.md`` and
``docs-site/reference/mcp-tools.md`` are rendered from the in-process
backend registries by ``backend/scripts/generate_reference_docs.py`` and
**committed** (the docs-site CI job installs only the ``docs`` dependency
group and must not import the backend — see
``.github/workflows/docs-site.yml``). This module is the drift guard: a
registry or catalog edit that changes either page without regeneration
turns the unit lane red — the same committed-derived-artifact shape as
``docs-site/reference/maturity.md`` /
``backend/tests/test_maturity_surface_drift.py`` and
``cli/api/openapi.json`` / its ``cli-api-snapshot-freshness`` CI job.

It also asserts the rendered pages carry no internal planning reference
(GitHub issue numbers, Goal/Task identifiers, internal design-doc section
markers, or a private-repo slug) — the redaction contract the public
surface depends on, checked against freshly rendered output so a new tool
description that smuggles a reference in fails here even while the
committed page stays "fresh".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.generate_reference_docs import (
    CONNECTORS_RELPATH,
    MCP_TOOLS_RELPATH,
    OVERVIEW_END,
    OVERVIEW_RELPATH,
    OVERVIEW_START,
    render_connector_index,
    render_mcp_tool_index,
    render_overview_block,
    splice_overview_block,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Tokens that must never appear in a published reference page.
_FORBIDDEN_PATTERNS = (
    r"#\d+",  # GitHub issue / PR numbers
    r"\bG\d[\dA-Za-z.]*-T\d+\b",  # Goal/Task identifiers, e.g. G11.2-T6
    r"§\d+",  # internal design-doc section markers
    r"meho-internal",  # private planning repo slug
    r"evoila-bosnia",  # private org slug
)


@pytest.mark.parametrize(
    ("relpath", "render"),
    [
        (CONNECTORS_RELPATH, render_connector_index),
        (MCP_TOOLS_RELPATH, render_mcp_tool_index),
    ],
)
def test_reference_page_is_fresh(relpath: str, render) -> None:
    """The committed page must equal freshly rendered output."""
    committed = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert committed == render(), (
        f"{relpath} is stale relative to the backend registry. Regenerate "
        "with `uv run python scripts/generate_reference_docs.py` from "
        "backend/ and commit the result."
    )


@pytest.mark.parametrize(
    ("relpath", "render"),
    [
        (CONNECTORS_RELPATH, render_connector_index),
        (MCP_TOOLS_RELPATH, render_mcp_tool_index),
    ],
)
def test_reference_page_carries_no_internal_reference(relpath: str, render) -> None:
    """No internal planning reference may leak into a public reference page."""
    rendered = render()
    leaks = {
        pattern: re.findall(pattern, rendered)
        for pattern in _FORBIDDEN_PATTERNS
        if re.search(pattern, rendered)
    }
    assert not leaks, (
        f"{relpath} would leak internal references {leaks}; extend "
        "`_redact` / `_INTERNAL_REF` in "
        "backend/scripts/generate_reference_docs.py to strip them."
    )


def test_overview_block_is_fresh() -> None:
    """The generated block of the overview page must equal the fresh block.

    The block comes from the live test record, so a change to the record
    without building the pages again fails here.
    """
    committed = (_REPO_ROOT / OVERVIEW_RELPATH).read_text(encoding="utf-8")
    assert committed == splice_overview_block(committed, render_overview_block()), (
        f"The generated block of {OVERVIEW_RELPATH} is stale. Regenerate "
        "with `uv run python scripts/generate_reference_docs.py` from "
        "backend/ and commit the result."
    )


def test_overview_block_carries_no_internal_reference() -> None:
    """The overview block follows the same public-safety rule as the other pages."""
    block = render_overview_block()
    assert block.startswith(OVERVIEW_START)
    assert block.endswith(OVERVIEW_END)
    for pattern in _FORBIDDEN_PATTERNS:
        assert not re.search(pattern, block), pattern


def test_splice_replaces_only_the_text_between_the_markers() -> None:
    page = f"before\n{OVERVIEW_START}\nold\n{OVERVIEW_END}\nafter\n"
    new_block = f"{OVERVIEW_START}\nnew\n{OVERVIEW_END}"
    assert splice_overview_block(page, new_block) == f"before\n{new_block}\nafter\n"
