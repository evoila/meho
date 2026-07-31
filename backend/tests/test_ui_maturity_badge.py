# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Area-header maturity badge chips (#2677).

Every expectation here is **derived from the feature-maturity registry**
(:data:`meho_backplane.features.FEATURE_MATURITY`, #2674) rather than
hardcoded per surface, so a registry retier (the v0.28 post-eval round)
never requires a test edit — the same no-drift property the surface
itself promises. What is pinned structurally:

* the ``SURFACE_FEATURE`` mapping covers exactly ``base.html``'s
  sidebar registry and points only at real registry keys;
* the shared ``_maturity_badge.html`` include renders a chip for every
  non-GA surface (correct tier label, DaisyUI 5 class, maturity-index
  link) and renders nothing for GA / unmapped surfaces;
* a registry retier propagates through the include with zero template
  edits (acceptance criterion 3, proven by monkeypatching an entry);
* every area landing template pulls the shared include — no per-page
  hardcoding anywhere.
"""

from __future__ import annotations

import re

from pytest import MonkeyPatch

from meho_backplane.features import FEATURE_MATURITY
from meho_backplane.ui.maturity import (
    MATURITY_INDEX_URL,
    SURFACE_FEATURE,
    surface_maturity,
)
from meho_backplane.ui.paths import templates_dir
from meho_backplane.ui.templating import get_jinja_env

#: The area landing templates (one full-page header per sidebar area;
#: topology serves both its graph and table views from ``/ui/topology``,
#: so both carry the header). Each must pull the shared include — the
#: chip's presence is decided by the registry, never by the template.
_AREA_LANDING_TEMPLATES = (
    "broadcast/feed.html",
    "kb/index.html",
    "corpus/index.html",
    "retrieval/index.html",
    "topology/graph.html",
    "topology/table.html",
    "connectors/list.html",
    "connectors/registry.html",
    "memory/index.html",
    "conventions/index.html",
    "agents/index.html",
    "runbooks/index.html",
    "scheduler/list.html",
    "checks/list.html",
    "sensors/list.html",
    "runners/list.html",
    "operations/index.html",
    "keycloak/index.html",
    "vault/index.html",
    "approvals/index.html",
    "audit/index.html",
)

_INCLUDE_TAG = '{% include "_maturity_badge.html" %}'


def _sidebar_surface_keys() -> set[str]:
    """Extract the surface keys from ``base.html``'s ``nav_surfaces``."""
    src = (templates_dir() / "base.html").read_text()
    match = re.search(r"nav_surfaces = \[(.*?)\] %}", src, re.S)
    assert match is not None, "nav_surfaces registry not found in base.html"
    keys = re.findall(r'\(\s*"([a-z-]+)"', match.group(1))
    assert keys, "no surface keys parsed from nav_surfaces"
    return set(keys)


def _render_badge(active_surface: str) -> str:
    return (
        get_jinja_env().get_template("_maturity_badge.html").render(active_surface=active_surface)
    )


def test_mapping_covers_exactly_the_sidebar_surfaces() -> None:
    """``SURFACE_FEATURE`` and ``base.html``'s sidebar cannot drift.

    A new sidebar surface without a maturity mapping would silently
    render chip-less (implicitly claiming GA); a stale mapping key
    would rot unnoticed. Pin set equality in both directions.
    """
    assert set(SURFACE_FEATURE) == _sidebar_surface_keys()


def test_mapping_targets_are_registry_keys() -> None:
    unknown = set(SURFACE_FEATURE.values()) - set(FEATURE_MATURITY)
    assert not unknown, f"mapped to nonexistent registry keys: {sorted(unknown)}"


def test_resolver_derives_tier_from_registry() -> None:
    """Chip iff the mapped feature is non-GA; fields come from the entry."""
    for surface, feature in SURFACE_FEATURE.items():
        info = FEATURE_MATURITY[feature]
        badge = surface_maturity(surface)
        if info["maturity"] == "ga":
            assert badge is None, f"{surface}: GA area must render no chip"
        else:
            assert badge is not None, f"{surface}: non-GA area must chip"
            assert badge["tier"] == info["maturity"]
            assert badge["label"] == info["maturity"].capitalize()
            assert badge["target_ga"] == info.get("target_ga")
            assert badge["tracking"] == info.get("tracking")
            assert badge["href"] == MATURITY_INDEX_URL


def test_resolver_renders_nothing_for_unmapped_surfaces() -> None:
    """Console chrome (dashboard, account) and the bare default: no chip."""
    for surface in ("", "account", "no-such-surface"):
        assert surface_maturity(surface) is None


def test_badge_include_renders_from_registry() -> None:
    """The shared include's output matches the registry, per surface.

    Expected chips are derived from ``FEATURE_MATURITY`` — beta chips
    carry ``badge-info``, experimental chips ``badge-warning`` (DaisyUI
    5 ``badge-soft`` style; classes verified against the vendored
    ``static/src/vendor/daisyui.js`` plugin), and every chip links the
    maturity-index docs page.
    """
    for surface, feature in SURFACE_FEATURE.items():
        html = _render_badge(surface)
        tier = FEATURE_MATURITY[feature]["maturity"]
        if tier == "ga":
            assert html.strip() == "", f"{surface}: GA area rendered a chip"
            continue
        assert f">{tier.capitalize()}</a>" in html, f"{surface}: label missing"
        expected_class = "badge-info" if tier == "beta" else "badge-warning"
        assert expected_class in html, f"{surface}: wrong tier class"
        assert "badge-soft" in html
        assert MATURITY_INDEX_URL in html, f"{surface}: index link missing"


def test_retier_propagates_without_template_edits(
    monkeypatch: MonkeyPatch,
) -> None:
    """Acceptance criterion 3: a registry edit alone changes the chip.

    Retier a GA feature to beta in the registry and re-render the
    include — the chip appears with no template (or mapping) change.
    """
    ga_surfaces = [s for s, f in SURFACE_FEATURE.items() if FEATURE_MATURITY[f]["maturity"] == "ga"]
    assert ga_surfaces, "registry has no GA-mapped surface to retier"
    surface = ga_surfaces[0]
    monkeypatch.setitem(
        FEATURE_MATURITY,
        SURFACE_FEATURE[surface],
        {"maturity": "beta", "target_ga": "v9.9.9"},
    )
    html = _render_badge(surface)
    assert ">Beta</a>" in html
    assert "target GA v9.9.9" in html


def test_area_landing_headers_pull_the_shared_include() -> None:
    """Every area landing header includes the one shared chip include."""
    tdir = templates_dir()
    for name in _AREA_LANDING_TEMPLATES:
        src = (tdir / name).read_text()
        assert _INCLUDE_TAG in src, f"{name}: maturity badge include missing"
