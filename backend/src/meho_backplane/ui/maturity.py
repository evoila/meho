# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Area-header maturity-badge resolution for the operator console (#2677).

Maps each ``/ui`` sidebar surface onto its canonical
:data:`~meho_backplane.features.FEATURE_MATURITY` key and resolves the
badge chip the area header renders. The resolution happens server-side
at template render time (a Jinja global bound in
:mod:`meho_backplane.ui.templating`) — no REST round-trip, exactly the
"resolved during template render" shape the task specifies. The chip
itself is one shared include, ``templates/_maturity_badge.html``; no
template hardcodes a tier, so a registry retier (the v0.28 post-eval
round, Goal #2661) propagates to every area header with zero template
edits.

Mapping rationale (the judgment calls, so a reviewer doesn't have to
re-derive them):

* ``connectors`` is the **targets** list (``connectors/list_view.py``
  renders ``targets`` rows) → ``targets``. The **registry** surface
  (``connectors-registry``) curates ingested connectors → the
  ``connector_ingest`` experimental line.
* ``checks`` dashboards compose Sensor results — the #2664
  classification names the beta line "sensors/dashboards", one
  feature → both ``checks`` and ``sensors`` map to ``sensors``.
* ``operations`` (launcher: browse + preview + **dispatch**, incl.
  write verbs), ``vault`` (KV browser + confirm-gated writes via
  ``call_operation``), and ``runbooks`` (curated compositions driven
  through the run driver) all dispatch writes; labelling them by the
  GA read plane would overstate exactly the way this program exists
  to prevent → ``write_surfaces``.
* ``retrieval`` (retrieval diagnostics over kb/memory/operations) and
  ``conventions`` (the preamble knowledge packer) are faces of the
  memory/knowledge plane → ``memory_knowledge``.
* ``keycloak`` (realm/user admin) and the account page's parent
  surface belong to ``auth_tenancy``.
* The dashboard (``/ui/``) and ``/ui/account`` are console chrome,
  not feature areas — deliberately unmapped, no chip. The console
  chassis's own tier (``ui_console``) is a deploy-wide property, not
  an area property; stamping "Beta" on every page would drown the
  per-area signal this surface exists to carry.
"""

from __future__ import annotations

from typing import TypedDict

from meho_backplane.features import FEATURE_MATURITY

__all__ = ["MATURITY_INDEX_URL", "SURFACE_FEATURE", "surface_maturity"]

#: The docs maturity-index page the chip links to. #2678 generates the
#: page (``docs-site/reference/maturity.md``, rendered by
#: ``backend/scripts/generate_maturity_index.py``) into the MkDocs
#: site's Reference section; each chip deep-links to its feature's
#: anchor — the page's per-feature headings are the raw registry keys,
#: which the toc slugifier preserves verbatim (underscores included).
#: ``latest`` is the mike alias every tagged deploy re-points.
MATURITY_INDEX_URL = "https://evoila.github.io/meho/latest/reference/maturity/"

#: Sidebar surface key (``active_surface``, as registered in
#: ``base.html``'s ``nav_surfaces``) → canonical
#: :data:`FEATURE_MATURITY` key. Every sidebar surface is mapped —
#: including GA ones, so a future retier needs no edit here — and the
#: test suite pins this dict against ``base.html``'s registry so the
#: two cannot drift silently.
SURFACE_FEATURE: dict[str, str] = {
    "pairing": "addon_pairing",
    # Paired-surface activation (#3029): the automation panel is part of the
    # add-on pairing contract (Initiative #2900) and shares its maturity.
    "automation": "addon_pairing",
    "broadcast": "broadcast",
    "knowledge": "memory_knowledge",
    "corpus": "doc_collections",
    "retrieval": "memory_knowledge",
    "topology": "topology",
    "connectors": "targets",
    "connectors-registry": "connector_ingest",
    "memory": "memory_knowledge",
    "conventions": "memory_knowledge",
    "agents": "agent_runtime",
    "runbooks": "write_surfaces",
    "scheduler": "scheduler",
    "checks": "sensors",
    "sensors": "sensors",
    "runners": "satellite_gateway",
    # event_source registry (#2880): the inbound producer side of the
    # event -> kind=event ScheduledTrigger substrate (#2877).
    "event-source": "scheduler",
    "operations": "write_surfaces",
    "keycloak": "auth_tenancy",
    "vault": "write_surfaces",
    "approvals": "approvals",
    "audit": "audit",
}


class MaturityBadge(TypedDict):
    """The context the ``_maturity_badge.html`` include renders from."""

    tier: str
    label: str
    target_ga: str | None
    tracking: str | None
    href: str


def surface_maturity(surface: str) -> MaturityBadge | None:
    """Resolve the badge chip for a sidebar surface key.

    Returns ``None`` — render nothing — for GA areas and for unmapped
    keys (the dashboard, the account page, the empty-string default a
    bare ``base.html`` render carries). Non-GA areas get the fields the
    chip needs; the tier and its metadata come straight from
    :data:`FEATURE_MATURITY`, read at render time so a registry edit is
    live on the next request with no restart-ordering caveats beyond
    the process reload any code change already implies.
    """
    feature = SURFACE_FEATURE.get(surface)
    if feature is None:
        return None
    info = FEATURE_MATURITY[feature]
    if info["maturity"] == "ga":
        return None
    return {
        "tier": info["maturity"],
        "label": info["maturity"].capitalize(),
        "target_ga": info.get("target_ga"),
        "tracking": info.get("tracking"),
        "href": f"{MATURITY_INDEX_URL}#{feature}",
    }
