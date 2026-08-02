# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Maturity drift guard + docs maturity-index freshness gate (#2678).

The #2664 program's enforcement piece: every user-facing surface — MCP
tool, public REST operation, server-advertised CLI command, ``/ui``
area — must resolve to a :data:`meho_backplane.features.FEATURE_MATURITY`
key, so an unlabelled surface is a **red build**, not a silent gap. One
test per surface class; each failure message names the unmapped surface
and the file to edit.

Deliberate infrastructure exemptions are **closed allowlists in this
module** — adding a surface without classifying it means either mapping
it to a registry key or adding it here with a written rationale, in the
same diff the reviewer sees. Stale allowlist entries fail too (the
guard asserts each exempted surface still exists), so exemptions cannot
outlive the surface they excuse. The #2678 decisions that emptied the
previous "deliberately unclassified" set:

* the ``meho_runbook_*`` MCP tools and the ``runbooks`` REST tag →
  ``write_surfaces`` (curated compositions driven through the run
  driver dispatch writes — the same call :mod:`meho_backplane.ui.maturity`
  already made for the ``/ui`` runbooks area);
* the ``conventions`` REST tag → ``memory_knowledge`` (the preamble
  knowledge packer is a face of the memory/knowledge plane, matching
  the ``/ui`` conventions area);
* ``meho_status`` stays unclassified **explicitly**: it mirrors
  ``/api/v1/health`` — infrastructure, the same posture as the
  ``health`` / ``version`` / ``mcp`` REST tags and the
  ``_READY_ENTRY_FEATURE`` ``mcp`` entry.

The ``/ui*`` surfaces are excluded from the REST class by path prefix:
the console's maturity surface is the #2677 badge chips (guarded by
:mod:`tests.test_ui_maturity_badge` plus the UI class below), and the
prefix exclusion tightens automatically once the #2662 public/BFF
OpenAPI split lands.

This module also carries the freshness gate for the **generated docs
maturity-index page** (``docs-site/reference/maturity.md``): the page
is rendered from the registry by
``backend/scripts/generate_maturity_index.py`` and committed, because
the docs-site CI job installs only the ``docs`` dependency group and
must not import the backend (see the script docstring). A registry
edit therefore turns this gate red until the page is regenerated —
same committed-derived-artifact shape as ``cli/api/openapi.json`` and
its ``cli-api-snapshot-freshness`` job.

Pattern precedent: :mod:`tests.test_truncate_list_drift` (registry
introspection + source extraction so drift fails on the PR that
introduces it, not a later unrelated one).
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts.generate_maturity_index import INDEX_RELPATH, render_maturity_index

import meho_backplane.mcp.registry as registry_module
from meho_backplane.api.openapi_maturity import PATH_FEATURE_OVERRIDES, TAG_FEATURE
from meho_backplane.features import FEATURE_MATURITY
from meho_backplane.main import app
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.ui.maturity import SURFACE_FEATURE
from meho_backplane.ui.routes.stubs import _SURFACE_STUBS

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Closed infrastructure allowlists — every entry carries its rationale.
# ---------------------------------------------------------------------------

#: MCP tools that deliberately carry ``feature=None``. ``meho_status``
#: mirrors ``/api/v1/health`` wire-identically — infrastructure, not a
#: classified feature (the ``_READY_ENTRY_FEATURE`` ``mcp`` precedent).
_MCP_INFRA_TOOLS = frozenset({"meho_status"})

#: REST tags that never map to a feature: deploy/runtime visibility
#: (``health``: /healthz, /ready, /api/v1/health; ``version``) and the
#: MCP transport endpoint (``mcp``: /mcp — the tools it carries are
#: labelled per-tool by the MCP class above).
_REST_INFRA_TAGS = frozenset({"health", "version", "mcp"})

#: Untagged public paths that never map to a feature: the root
#: redirect and the Prometheus scrape endpoint.
_REST_UNTAGGED_INFRA_PATHS = frozenset({"/", "/metrics"})

#: ``active_surface`` keys that are console chrome, not feature areas:
#: the account page (``/ui/account``). The dashboard passes no
#: ``active_surface`` at all, so it never reaches the mapping.
_UI_CHROME_SURFACES = frozenset({"account"})

_OPERATION_METHODS = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace"),
)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture
def production_tools() -> Iterator[dict[str, ToolDefinition]]:
    """Yield every production tool definition, freshly registered.

    Same isolation shape as
    :func:`tests.test_maturity_propagation.production_tools` (see its
    docstring for why import-then-reload is needed); replicated here so
    the two guard modules stay independently collectable.
    """
    tools_pkg = importlib.import_module("meho_backplane.mcp.tools")
    modules = [
        importlib.import_module(f"meho_backplane.mcp.tools.{name}")
        for _finder, name, _ispkg in pkgutil.iter_modules(tools_pkg.__path__)
    ]
    saved_tools = dict(registry_module._TOOLS)
    saved_resources = dict(registry_module._RESOURCES)
    registry_module._TOOLS.clear()
    for module in modules:
        importlib.reload(module)
    try:
        yield {name: defn for name, (defn, _handler) in registry_module._TOOLS.items()}
    finally:
        registry_module._TOOLS.clear()
        registry_module._TOOLS.update(saved_tools)
        registry_module._RESOURCES.clear()
        registry_module._RESOURCES.update(saved_resources)


def test_every_mcp_tool_resolves_to_a_feature(
    production_tools: dict[str, ToolDefinition],
) -> None:
    """Every registered MCP tool names a registry key or is exempt infra."""
    assert production_tools, "no production tools registered — fixture broken"
    unmapped = sorted(
        name
        for name, defn in production_tools.items()
        if defn.feature is None and name not in _MCP_INFRA_TOOLS
    )
    assert not unmapped, (
        f"MCP tools without a maturity classification: {unmapped}. Set "
        "feature=<FEATURE_MATURITY key> on each ToolDefinition (the file "
        "registering the tool under backend/src/meho_backplane/mcp/tools/), "
        "or — only for genuine infrastructure — add the tool name to "
        "_MCP_INFRA_TOOLS in this module with a rationale."
    )
    stale = sorted(_MCP_INFRA_TOOLS - production_tools.keys())
    assert not stale, (
        f"_MCP_INFRA_TOOLS entries no longer registered: {stale}. "
        "Remove them from this module's allowlist."
    )


# ---------------------------------------------------------------------------
# Public REST operations
# ---------------------------------------------------------------------------


@pytest.fixture
def openapi_schema() -> dict[str, Any]:
    """Bust FastAPI's cache and generate a fresh document."""
    app.openapi_schema = None
    return app.openapi()


def _public_operations(schema: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(path, method, operation)`` for every non-BFF operation."""
    for path, path_item in schema["paths"].items():
        if path.startswith("/ui"):
            # BFF console routes — #2677's surface; prefix exclusion
            # tightens automatically after the #2662 public/BFF split.
            continue
        for method, operation in path_item.items():
            if method in _OPERATION_METHODS and isinstance(operation, dict):
                yield path, method, operation


def test_every_public_rest_operation_resolves_to_a_feature(
    openapi_schema: dict[str, Any],
) -> None:
    """Every public operation maps via tag or path override, or is infra."""
    unmapped: list[str] = []
    for path, method, operation in _public_operations(openapi_schema):
        if path in PATH_FEATURE_OVERRIDES:
            continue
        tags = operation.get("tags", [])
        if not tags:
            if path not in _REST_UNTAGGED_INFRA_PATHS:
                unmapped.append(f"{method.upper()} {path} (untagged)")
        elif not all(tag in TAG_FEATURE or tag in _REST_INFRA_TAGS for tag in tags):
            unmapped.append(f"{method.upper()} {path} (tags={tags})")
    assert not unmapped, (
        f"public REST operations without a maturity classification: {unmapped}. "
        "Map each operation's tag in TAG_FEATURE (or, where its tag spans "
        "tiers, its path in PATH_FEATURE_OVERRIDES) in "
        "backend/src/meho_backplane/api/openapi_maturity.py, or — only for "
        "genuine infrastructure — extend _REST_INFRA_TAGS / "
        "_REST_UNTAGGED_INFRA_PATHS in this module with a rationale."
    )


def test_rest_infra_allowlists_carry_no_stale_entries(
    openapi_schema: dict[str, Any],
) -> None:
    """Exemptions cannot outlive the surfaces they excuse."""
    used_tags = {
        tag
        for _path, _method, operation in _public_operations(openapi_schema)
        for tag in operation.get("tags", [])
    }
    stale_tags = sorted(_REST_INFRA_TAGS - used_tags)
    assert not stale_tags, f"_REST_INFRA_TAGS entries on no operation: {stale_tags}"
    stale_paths = sorted(_REST_UNTAGGED_INFRA_PATHS - openapi_schema["paths"].keys())
    assert not stale_paths, f"_REST_UNTAGGED_INFRA_PATHS not in the document: {stale_paths}"


# ---------------------------------------------------------------------------
# Server-advertised CLI commands
# ---------------------------------------------------------------------------


def test_cli_command_manifest_endpoint_is_still_unshipped(
    openapi_schema: dict[str, Any],
) -> None:
    """Tripwire: the manifest endpoint must land together with its guard.

    ``GET /api/v1/commands`` (the CLI's server-driven discovery
    endpoint, ``cli/internal/discovery/discovery.go``) is an unshipped
    Goal #11 §5 coordination point — every backplane 404s it today and
    the CLI falls back to its local command set, so there are no
    server-advertised commands to classify yet (see
    ``docs/codebase/feature-maturity.md``). This tripwire turns red on
    the PR that ships the endpoint: extend this module with a manifest
    class asserting every advertised command resolves its owning
    feature from FEATURE_MATURITY (the ``maturity`` field the CLI
    already renders, #2676), then retire this test.

    Detection goes through the generated OpenAPI document, not a scan
    of ``app.routes``: on the locked FastAPI, ``include_router`` is
    lazy — the included routes never surface in ``app.routes`` — so a
    route-scan tripwire stays green on the realistic
    ``app.include_router`` landing, while the OpenAPI paths always
    carry the new endpoint.
    """
    assert "/api/v1/commands" not in openapi_schema["paths"], (
        "/api/v1/commands has landed: replace this tripwire with a drift "
        "guard asserting every command the manifest advertises resolves to "
        "a FEATURE_MATURITY key (see this test's docstring)."
    )


# ---------------------------------------------------------------------------
# /ui areas
# ---------------------------------------------------------------------------

_UI_ROUTES_DIR = _REPO_ROOT / "backend" / "src" / "meho_backplane" / "ui" / "routes"

#: Matches the ``active_surface`` context entries every /ui route
#: passes for the sidebar highlight, in both in-tree idioms: the dict
#: literal (``"active_surface": "kb"``) and the item assignment
#: (``context["active_surface"] = "audit"``). Literal string values
#: only — the single dynamic site (``stubs.py``'s ``stub.slug``) draws
#: from ``_SURFACE_STUBS``, whose emptiness the test below asserts.
_ACTIVE_SURFACE_RE = re.compile(r'"active_surface"\s*(?::|\]\s*=)\s*"([^"]+)"')


def test_every_ui_area_surface_resolves_to_a_feature() -> None:
    """Every ``active_surface`` a /ui route passes is mapped or chrome.

    Complements :mod:`tests.test_ui_maturity_badge` (which pins
    ``SURFACE_FEATURE`` ↔ ``base.html``'s sidebar exactly): this class
    catches a new area *route* whose surface key joins neither the
    mapping nor the sidebar.
    """
    assert not _SURFACE_STUBS, (
        "stubs.py re-grew dynamic surfaces — extend this guard to cover "
        "their slugs (they render active_surface=stub.slug, which the "
        "literal-matching regex cannot see)."
    )
    surfaces: dict[str, list[str]] = {}
    for source in sorted(_UI_ROUTES_DIR.rglob("*.py")):
        for key in _ACTIVE_SURFACE_RE.findall(source.read_text(encoding="utf-8")):
            surfaces.setdefault(key, []).append(str(source.relative_to(_REPO_ROOT)))
    assert surfaces, "no active_surface literals found — extraction regex broken?"
    unmapped = {
        key: files
        for key, files in surfaces.items()
        if key not in SURFACE_FEATURE and key not in _UI_CHROME_SURFACES
    }
    assert not unmapped, (
        f"/ui surfaces without a maturity classification: {unmapped}. Map "
        "each key in SURFACE_FEATURE (backend/src/meho_backplane/ui/"
        "maturity.py), or — only for console chrome — add it to "
        "_UI_CHROME_SURFACES in this module with a rationale."
    )
    stale = sorted(_UI_CHROME_SURFACES - surfaces.keys())
    assert not stale, f"_UI_CHROME_SURFACES entries on no route: {stale}"


# ---------------------------------------------------------------------------
# Generated docs maturity-index page
# ---------------------------------------------------------------------------


def test_maturity_index_page_is_fresh() -> None:
    """The committed page equals the registry-rendered content."""
    committed = (_REPO_ROOT / INDEX_RELPATH).read_text(encoding="utf-8")
    assert committed == render_maturity_index(), (
        f"{INDEX_RELPATH} is stale relative to FEATURE_MATURITY. "
        "Regenerate it from backend/ with "
        "`uv run python scripts/generate_maturity_index.py` and commit "
        "the result."
    )


def test_maturity_index_page_anchors_every_non_ga_feature() -> None:
    """The #2677 badge chips deep-link ``#<feature-key>``; the anchors
    must exist. Per-feature headings are the raw registry keys, which
    the Python-Markdown toc slugifier preserves verbatim (underscores
    included), so a ``### <key>`` heading guarantees the anchor.
    """
    page = render_maturity_index()
    missing = sorted(
        key
        for key, info in FEATURE_MATURITY.items()
        if info["maturity"] != "ga" and f"\n### {key}\n" not in page
    )
    assert not missing, (
        f"maturity-index page lacks anchor headings for: {missing} — "
        "the /ui badge deep-links would 404. Fix "
        "backend/scripts/generate_maturity_index.py."
    )
