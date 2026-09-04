#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render the docs-site connector-inventory and MCP-tool reference pages.

Two committed reference pages are generated from the in-process backend
registries — no database, no live backplane:

* ``docs-site/reference/connectors.md`` — the per-connector inventory,
  from the v2 connector registry
  (:func:`meho_backplane.connectors.registry.all_connectors_v2`) joined
  with the committed connector-spec catalog
  (:func:`meho_backplane.operations.ingest.catalog.load_catalog`).
* ``docs-site/reference/mcp-tools.md`` — the MCP tool surface, from the
  MCP tool registry (:data:`meho_backplane.mcp.registry._TOOLS`, populated
  by :func:`~meho_backplane.mcp.registry.eager_import_mcp_modules`) plus
  the human-only decision verbs
  (:data:`meho_backplane.mcp.human_only.HUMAN_ONLY_MCP_TOOLS`).

Both pages are **committed**, not built on the fly: the docs-site CI job
installs only the ``docs`` dependency group (``uv sync --locked
--only-group docs``, see ``.github/workflows/docs-site.yml``), so an
mkdocs hook importing ``meho_backplane`` would drag the whole backend
dependency tree into every docs build and tag deploy. Instead this script
regenerates the pages inside the backend env, and the freshness gate in
:mod:`tests.test_reference_docs_drift` fails the unit lane whenever a
committed page and its registry disagree — the same committed-derived-
artifact shape as ``docs-site/reference/maturity.md`` /
``backend/scripts/generate_maturity_index.py`` and ``cli/api/openapi.json``.

**Public-safety.** Tool descriptions are the agent-facing contract text
and several carry bare ``#NNNN`` planning-issue references; :func:`_redact`
strips every such token (and any parenthetical that only exists to hold
one) so no internal ticket number reaches the public page. The pages
never restate a maturity or readiness stage beyond what a public,
machine-readable source states — connector *kind* comes only from the
committed catalog (blank where the catalog is silent), and per-connector
readiness is deliberately not asserted (no machine-readable public
per-connector stage exists; the page links the maturity reference and the
CHANGELOG ship-state convention instead).

Run from ``backend/``::

    uv run python scripts/generate_reference_docs.py
"""

from __future__ import annotations

import re
from pathlib import Path

from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    all_connectors_v2,
)
from meho_backplane.features import FEATURE_MATURITY
from meho_backplane.mcp.human_only import HUMAN_ONLY_MCP_TOOLS
from meho_backplane.mcp.registry import ToolSurface, eager_import_mcp_modules
from meho_backplane.operations.ingest.catalog import load_catalog

#: Where the rendered pages land, relative to the repo root.
CONNECTORS_RELPATH = "docs-site/reference/connectors.md"
MCP_TOOLS_RELPATH = "docs-site/reference/mcp-tools.md"

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Sentence-ending abbreviations the first-sentence extractor must not
#: split on (``"... e.g. this"`` is one sentence, not two).
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "cf.", "vs.")

# ---------------------------------------------------------------------------
# Public-safety text helpers
# ---------------------------------------------------------------------------


#: Internal planning identifiers that must never reach a public page:
#: GitHub issue numbers (``#3153``), Goal/Initiative/Task identifiers
#: (``G11.2-T6``, ``G0.7``), and internal design-doc section markers
#: (``§6``). Tool descriptions are the agent-facing contract text and
#: several embed these; the published reference must strip them.
_INTERNAL_REF = r"(?:#\d+|G\d[\dA-Za-z.]*(?:-T\d+)?|§\d+)"


def _redact(text: str) -> str:
    """Strip internal planning references from public-facing text.

    Removes any parenthetical group that exists only to carry an internal
    reference (``"(Initiative #3153)"``, ``"(G11.2-T6)"``), then any bare
    internal-reference token left behind, then collapses the resulting
    whitespace.
    """
    text = re.sub(rf"\s*\([^()]*{_INTERNAL_REF}[^()]*\)", "", text)
    text = re.sub(rf"\s*{_INTERNAL_REF}", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _first_sentence(text: str) -> str:
    """Return the first sentence of *text*, abbreviation-aware."""
    for match in re.finditer(r"\.\s", text):
        head = text[: match.start() + 1]
        if not any(head.endswith(abbr) for abbr in _ABBREVIATIONS):
            return head
    return text


def _one_liner(description: str) -> str:
    """Reduce a full MCP tool description to a redacted one-line summary."""
    body = re.sub(r"^\[(?:beta|experimental)\]\s*", "", description)
    return _redact(_first_sentence(body.split("\n", 1)[0]))


def _md_escape(text: str) -> str:
    """Escape the pipe character so cell text cannot break a Markdown table."""
    return text.replace("|", r"\|")


def _generated_header(script_note: str, freshness_note: str) -> list[str]:
    """The ``do not edit by hand`` banner every generated page carries."""
    return [
        "<!--",
        "  GENERATED FILE — do not edit by hand.",
        f"  Regenerate from backend/ with: {script_note}",
        f"  {freshness_note}",
        "-->",
        "",
    ]


# ---------------------------------------------------------------------------
# Connector inventory
# ---------------------------------------------------------------------------


def _catalog_kind_by_triple() -> dict[tuple[str, str, str], str]:
    """Map each catalogued connector triple to ``"generic"`` / ``"typed"``.

    The committed connector-spec catalog is the only DB-free public
    statement of a connector's ingest kind: a row that names an
    ``upstream`` spec URL or ships a bundled ``spec_resource`` is a
    generic (spec-ingested) connector; a row with neither is a typed
    (hand-coded) one. The mechanism-fixture row (``product="_fixture"``)
    is not a shippable connector and is skipped.
    """
    kinds: dict[tuple[str, str, str], str] = {}
    for entry in load_catalog().entries:
        if entry.product == "_fixture":
            continue
        generic = bool(getattr(entry, "upstream", None)) or bool(
            getattr(entry, "spec_resource", None)
        )
        kinds[(entry.product, entry.version, entry.impl_id)] = "generic" if generic else "typed"
    return kinds


def render_connector_index() -> str:
    """Render the connector-inventory page from the v2 registry + catalog."""
    _eager_import_connectors()
    kinds = _catalog_kind_by_triple()

    rows: list[tuple[str, str, str, str]] = []
    for (product, version, impl_id), cls in all_connectors_v2().items():
        # Skip the wildcard / v1-compat padding rows (empty version /
        # impl_id): they are resolver-internal, never operator-addressable.
        if not version or not impl_id:
            continue
        connector_id = f"{impl_id}-{version}"
        supported = cls.supported_version_range or "—"
        kind = kinds.get((product, version, impl_id), "—")
        rows.append((product, connector_id, supported, kind))
    rows.sort(key=lambda row: (row[0], row[1]))

    lines = _generated_header(
        "uv run python scripts/generate_reference_docs.py",
        "The freshness gate in backend/tests/test_reference_docs_drift.py "
        "fails CI when this page and the registry disagree.",
    )
    lines += [
        "# Connector inventory",
        "",
        "Every connector MEHO can resolve a target to, generated from the "
        "in-process connector registry. Each row is one registered "
        "implementation: agents and operators drive them all through the "
        "same governed surface — pick a connector, list its operation "
        "groups, search operations, then call — so the vendor never leaks "
        "into the tool names.",
        "",
        "MEHO has two kinds of connector, both first-class and "
        "indistinguishable to an agent. **Generic** connectors are built by "
        "ingesting a vendor's protocol spec (OpenAPI, GraphQL, WSDL, proto); "
        "**typed** connectors are hand-coded against a vendor SDK or "
        "transport where no usable spec exists. The **Kind** column is "
        "populated only where MEHO's published connector-spec catalog states "
        "it, and is blank otherwise — it is a property of how a connector's "
        "operations are sourced, not something this table infers.",
        "",
        "| Connector | Product | Supported versions | Kind |",
        "| --- | --- | --- | --- |",
    ]
    for product, connector_id, supported, kind in rows:
        lines.append(
            f"| `{_md_escape(connector_id)}` | `{_md_escape(product)}` "
            f"| {_md_escape(supported)} | {kind} |"
        )
    lines += [
        "",
        "## Versions and how a connector is chosen",
        "",
        "**Supported versions** is the product-version range an "
        "implementation advertises. Where a product has more than one "
        "implementation — a modern and a legacy one, say — both are "
        "registered and MEHO resolves the right one per target from the "
        "target's fingerprint, so one estate spanning old and new versions "
        "just works.",
        "",
        "## Maturity and readiness",
        "",
        "This inventory lists what is *registered*; it does not restate a "
        "ship-state. Feature-level maturity — GA, beta, or experimental — is "
        "published in the [feature maturity index](maturity.md). "
        "Per-release, per-connector ship-state (dispatch + catalog, "
        "loader-wired, or production-ready) is stated in the "
        "[changelog](https://github.com/evoila/meho/blob/main/CHANGELOG.md) "
        "under the connector release-notes convention. The two-connector "
        "model is described in the "
        "[architecture overview](../architecture.md).",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


def _tool_maturity(feature: str | None) -> str:
    """Resolve a tool's maturity tier from its owning feature key."""
    if feature is None:
        return "—"
    return FEATURE_MATURITY[feature]["maturity"]


def _tool_rows(definitions: list) -> list[tuple[str, str, str, str]]:
    """Build ``(name, maturity, gating, summary)`` rows, sorted by name."""
    rows: list[tuple[str, str, str, str]] = []
    for defn in sorted(definitions, key=lambda d: d.name):
        gating: list[str] = []
        if defn.required_capability is not None:
            gating.append(f"capability `{defn.required_capability}`")
        if defn.required_addon_family is not None:
            gating.append(f"add-on `{defn.required_addon_family}`")
        rows.append(
            (
                defn.name,
                _tool_maturity(defn.feature),
                ", ".join(gating) or "—",
                _one_liner(defn.description),
            )
        )
    return rows


def _tool_table(rows: list[tuple[str, str, str, str]]) -> list[str]:
    """Render tool rows as a Markdown table."""
    lines = [
        "| Tool | Maturity | Extra gate | Summary |",
        "| --- | --- | --- | --- |",
    ]
    for name, maturity, gating, summary in rows:
        lines.append(f"| `{name}` | {maturity} | {gating} | {_md_escape(summary)} |")
    return lines


def render_mcp_tool_index() -> str:
    """Render the MCP tool-surface page from the in-process tool registry."""
    eager_import_mcp_modules()
    from meho_backplane.mcp.registry import _TOOLS

    definitions = [defn for defn, _handler in _TOOLS.values()]
    working_default = [
        d
        for d in definitions
        if d.surface is ToolSurface.WORKING and d.required_addon_family is None
    ]
    working_addon = [
        d
        for d in definitions
        if d.surface is ToolSurface.WORKING and d.required_addon_family is not None
    ]
    operator = [d for d in definitions if d.surface is ToolSurface.OPERATOR]

    lines = _generated_header(
        "uv run python scripts/generate_reference_docs.py",
        "The freshness gate in backend/tests/test_reference_docs_drift.py "
        "fails CI when this page and the registry disagree.",
    )
    lines += [
        "# MCP tool surface",
        "",
        "MEHO exposes one narrow, stable surface of meta-tools over MCP — "
        "the same tools across every connector and product version, so an "
        "agent never has to route through a vendor's thousands of "
        "operations. The surface has three tiers, generated here from the "
        "in-process tool registry.",
        "",
        "- **Working surface** — what every MCP session lists by default.",
        "- **Operator plane** — governance tools a session lists only after "
        "requesting the `mcp:admin` OAuth scope (request-only; the realm "
        "grants it on ask).",
        "- **Human-only** — decision verbs with no MCP path under any claim "
        "set; a human makes these at the console or CLI.",
        "",
        "Each tool also exists as a `meho` CLI command against the same "
        "dispatch path — see the [CLI reference](cli.md). Maturity labels "
        "(`beta` / `experimental`) come from the "
        "[feature maturity registry](maturity.md); a `—` maturity is "
        "deliberately unclassified infrastructure.",
        "",
        "## Working surface",
        "",
        "The default agent surface. No elevation required.",
        "",
    ]
    lines += _tool_table(_tool_rows(working_default))
    if working_addon:
        lines += [
            "",
            "### Add-on working tools",
            "",
            "Listed on the working surface only while a paired add-on "
            "advertising the named family is active for the tenant; a "
            "backplane with no such add-on paired never lists them.",
            "",
        ]
        lines += _tool_table(_tool_rows(working_addon))
    lines += [
        "",
        "## Operator plane (`mcp:admin`)",
        "",
        "Connector lifecycle, principals and grants, scheduler, sensors, "
        "topology mutations, audit admin, and the other governance planes. "
        "A session lists and can call these only when it holds the "
        "`mcp:admin` scope.",
        "",
    ]
    lines += _tool_table(_tool_rows(operator))
    lines += [
        "",
        "## Human-only (no MCP path)",
        "",
        "Approving or rejecting a parked operation, and granting an agent a "
        "privilege elevation, are human decisions with no agent-facing path "
        "under any claim set. An agent that reaches for one is told where "
        "the human makes the decision instead.",
        "",
        "| Tool | Where the decision is made |",
        "| --- | --- |",
    ]
    for name in sorted(HUMAN_ONLY_MCP_TOOLS):
        lines.append(f"| `{name}` | operator console approvals queue, or the `meho` CLI |")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    """Write both rendered reference pages."""
    for relpath, render in (
        (CONNECTORS_RELPATH, render_connector_index),
        (MCP_TOOLS_RELPATH, render_mcp_tool_index),
    ):
        target = _REPO_ROOT / relpath
        target.write_text(render(), encoding="utf-8")
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
