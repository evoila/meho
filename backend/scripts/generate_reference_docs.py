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
#: (``§6``). A ``#`` right after a word (``gh:org/repo#7``, a
#: change-ticket reference format) is not one. The same pattern guards
#: the text MEHO sends to a model (``tests/test_model_text_no_internal_refs.py``).
_INTERNAL_REF = r"(?<!\w)(?:#\d+|G\d[\dA-Za-z.]*(?:-T\d+)?|§\d+)"


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


#: Where a connector's contributor docs page lives on GitHub. The docs site
#: does not build ``docs/codebase/``, so the table uses the full GitHub address.
_DOCS_PAGE_URL = "https://github.com/evoila/meho/blob/main/docs/codebase/{name}"

#: Products whose docs page name differs from ``connectors-<product>.md``.
_DOCS_PAGE_BY_PRODUCT = {
    "gh": "connectors-github.md",
    "hetzner": "connectors-hetzner-robot.md",
    "k8s": "connectors-kubernetes.md",
    "windns": "connectors-windows-dns.md",
}

#: The connectors of evoila's enterprise pack, a commercial add-on image on
#: top of the public one. They are not in this repository's registry, so the
#: page lists them from this fixed list: product, connector ids, the product
#: versions they cover, and what they do. Names and one-liners only.
_ENTERPRISE_PACK: tuple[tuple[str, str, str, str], ...] = (
    (
        "VMware vCenter and vSphere",
        "vmware-rest-9.0, vmware-rest-8.0",
        "8.0, 9.x",
        "Reads and manages virtual machines, hosts, clusters, storage and networks.",
    ),
    (
        "VMware NSX",
        "nsx-rest-9.0, nsx-rest-4.2",
        "4.x, 9.x",
        "Reads and changes segments, gateways, firewall rules, load balancers and VPNs.",
    ),
    (
        "VCF SDDC Manager",
        "sddc-rest-9.0, sddc-vcf5-5.0",
        "5.x, 9.x",
        "Reads workload domains, clusters and hosts, and runs VCF lifecycle tasks.",
    ),
    (
        "VCF Installer and Cloud Builder",
        "installer-rest-9.1, installer-cb-5.0",
        "5.x, 9.x",
        "Checks a deployment plan and deploys a new VCF instance.",
    ),
    (
        "VCF fleet management and Aria Suite Lifecycle",
        "fleet-lcm-9.0, fleet-rest-9.0",
        "8.x, 9.x",
        "Installs, updates and scales the VCF management components.",
    ),
    (
        "VCF Automation and Aria Automation",
        "vcfa-rest-9.0, vcfa-vra8-8.0",
        "8.x, 9.x",
        "Reads and manages projects, catalog items, templates and deployments.",
    ),
    (
        "VCF Operations and Aria Operations",
        "vrops-rest-9.0, vrops-vrops8-8.0",
        "8.x, 9.x",
        "Reads resources, alerts, metrics and health.",
    ),
    (
        "VCF Operations for logs and Aria Operations for Logs",
        "vrli-rest-9.0, vrli-vrli8-8.0",
        "8.x, 9.x",
        "Searches logs and reads log alerts.",
    ),
    (
        "VCF Operations orchestrator",
        "vro-rest-9.0",
        "8.x, 9.x",
        "Reads workflows and their runs, and starts or cancels a workflow run.",
    ),
    (
        "VCF Identity Broker and Workspace ONE Access",
        "idb-rest-9.0, idb-wsa-3.3",
        "3.3, 9.x",
        "Reads users, groups and identity settings.",
    ),
    (
        "VMware Cloud Director",
        "vcd-rest-10.6",
        "10.x",
        "Reads and manages tenants, vApps, virtual machines, networks and edge gateways.",
    ),
    (
        "VMware HCX",
        "hcx-rest-9.0",
        "4.7 to 9.x",
        "Reads site pairings, service meshes and migrations, and changes service meshes.",
    ),
    (
        "VMware Site Recovery Manager",
        "srm-rest-9.1",
        "8.8 to 9.x",
        "Reads protection groups and recovery plans, and runs recovery tasks.",
    ),
    (
        "VMware vSphere Replication",
        "vr-rest-9.1",
        "8.8 to 9.x",
        "Reads and manages the replication of virtual machines.",
    ),
    (
        "VMware Aria Automation Config (Salt)",
        "salt-raas-8.18",
        "8.x, 9.x",
        "Reads minions and jobs, accepts minion keys and starts jobs.",
    ),
    (
        "VMware Holodeck",
        "holodeck-ssh-9.0",
        "9.0",
        "Builds and manages nested VCF lab environments.",
    ),
    (
        "Fortinet FortiGate",
        "fortigate-rest-7.x",
        "7.2 to 7.x",
        "Reads firewalls and changes interfaces, routes, addresses, policies and IPsec VPNs.",
    ),
    (
        "Fortinet FortiManager",
        "fortimanager-jsonrpc-7.x",
        "7.2 to 7.x",
        "Changes addresses, groups and policies, then installs the policy package to devices.",
    ),
    (
        "Fortinet FortiAnalyzer",
        "fortianalyzer-jsonrpc-7.x",
        "7.2 to 7.x",
        "Searches the logs and events that FortiAnalyzer keeps for many devices.",
    ),
    (
        "Fortinet FortiMail",
        "fortimail-rest-7.x",
        "7.2 to 7.x",
        "Reads status, quarantine and mail queues, and changes block lists, safe lists "
        "and policies.",
    ),
    (
        "Fortinet FortiSIEM",
        "fortisiem-rest-7.5, fortisiem-rest-7.4",
        "7.4 to 7.x",
        "Reads the health of the FortiSIEM nodes and the organizations.",
    ),
    (
        "CrowdStrike Falcon",
        "falcon-rest-2026.11",
        "cloud service",
        "Reads hosts and detections, and cuts a host off the network or gives it back.",
    ),
)


def _docs_link(product: str) -> str:
    """Return a Markdown link to *product*'s docs page, or "" when it has none."""
    name = _DOCS_PAGE_BY_PRODUCT.get(product, f"connectors-{product}.md")
    if not (_REPO_ROOT / "docs" / "codebase" / name).is_file():
        return ""
    return f"[docs]({_DOCS_PAGE_URL.format(name=name)})"


def render_connector_index() -> str:
    """Render the connector-inventory page from the v2 registry + catalog."""
    _eager_import_connectors()
    kinds = _catalog_kind_by_triple()

    rows: list[tuple[str, str, str, str, str]] = []
    for (product, version, impl_id), cls in all_connectors_v2().items():
        # Skip the wildcard / v1-compat padding rows (empty version /
        # impl_id): they are resolver-internal, never operator-addressable.
        if not version or not impl_id:
            continue
        connector_id = f"{impl_id}-{version}"
        supported = cls.supported_version_range or "—"
        kind = kinds.get((product, version, impl_id), "—")
        summary = cls.summary or "—"
        rows.append((product, connector_id, summary, supported, kind))
    rows.sort(key=lambda row: (row[0], row[1]))

    lines = _generated_header(
        "uv run python scripts/generate_reference_docs.py",
        "The freshness gate in backend/tests/test_reference_docs_drift.py "
        "fails CI when this page and the registry disagree.",
    )
    lines += [
        "# Connector inventory",
        "",
        "This page lists every MEHO connector in two tables. The first table "
        "lists the open source connectors of the public image. The second "
        "table lists the connectors of evoila's enterprise pack, a commercial "
        "add-on with the VMware and VCF connectors. Agents and "
        "operators drive all of them through the same governed surface — "
        "pick a connector, list its operation groups, search operations, "
        "then call — so no tool name carries a vendor name.",
        "",
        "You can add your own connectors the same way as the enterprise "
        "pack: see [Write a connector package](../extend/connector-kit.md).",
        "",
        "## Open source connectors (public image)",
        "",
        "These connectors are in the public image, generated from the "
        "in-process connector registry. Each row is one registered "
        "implementation. The **Docs** column links to the connector's page "
        "in the source repository.",
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
        "| Connector | Product | What it does | Supported versions | Kind | Docs |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for product, connector_id, summary, supported, kind in rows:
        lines.append(
            f"| `{_md_escape(connector_id)}` | `{_md_escape(product)}` "
            f"| {_md_escape(summary)} | {_md_escape(supported)} | {kind} "
            f"| {_docs_link(product)} |"
        )
    lines += [
        "",
        "## evoila's enterprise pack",
        "",
        "evoila's enterprise pack is a commercial add-on. It adds the VMware "
        "and VCF connectors, and connectors for Fortinet and CrowdStrike "
        "products, as a signed package on top of the public image. "
        "Their setup guides and operation lists come with the pack. To get "
        "the pack, contact evoila.",
        "",
        "| Product | Connectors | Product versions | What it does |",
        "| --- | --- | --- | --- |",
    ]
    for product, connectors, versions, summary in _ENTERPRISE_PACK:
        ids = ", ".join(f"`{_md_escape(c.strip())}`" for c in connectors.split(","))
        lines.append(
            f"| {_md_escape(product)} | {ids} | {_md_escape(versions)} | {_md_escape(summary)} |"
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
