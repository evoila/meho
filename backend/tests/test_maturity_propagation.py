# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Maturity propagation to the agent-facing surfaces (#2675).

Three surfaces, one source of truth
(:data:`~meho_backplane.features.FEATURE_MATURITY`):

* **MCP tool descriptions** — non-GA tools carry a ``[beta]`` /
  ``[experimental]`` prefix applied by
  :func:`~meho_backplane.mcp.registry.register_mcp_tool`; GA and
  deliberately-unclassified (``feature=None``) tools carry none.
* **``initialize.instructions``** — the static
  :data:`~meho_backplane.mcp.maturity.FEATURE_MATURITY_BAND` appended
  after the assembled preamble (wire-level coverage lives in
  :mod:`tests.test_mcp_initialize_instructions`; this module pins the
  band's *content* contract).
* **OpenAPI ``x-maturity``** — top-level tag entries plus the
  spans-tiers per-operation overrides injected by
  :func:`~meho_backplane.api.openapi_maturity.inject_maturity_extensions`.

Every expectation here is **derived from the registry** — no
hand-maintained expected-list (the #2675 acceptance criterion). A
registry retier must flip these assertions' expectations automatically,
never require an edit here.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

import meho_backplane.mcp.registry as registry_module
from meho_backplane.api.openapi_maturity import (
    PATH_FEATURE_OVERRIDES,
    TAG_FEATURE,
)
from meho_backplane.features import FEATURE_MATURITY
from meho_backplane.main import app
from meho_backplane.mcp.maturity import (
    FEATURE_MATURITY_BAND,
    MATURITY_BLOCK_END,
    MATURITY_BLOCK_START,
    _build_band,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool

_NON_GA_PREFIXES = ("[beta] ", "[experimental] ")


async def _noop_handler(_operator: object, _args: dict[str, object]) -> dict[str, object]:
    return {}


# ---------------------------------------------------------------------------
# MCP tool-description prefixes
# ---------------------------------------------------------------------------


@pytest.fixture
def production_tools() -> Iterator[dict[str, ToolDefinition]]:
    """Yield every production tool definition, freshly registered.

    Python's import cache makes registration side effects one-shot per
    process, so a prior test's ``clear_registries()`` leaves the tool
    modules imported but the registry empty. Import-then-reload forces
    every module body to re-execute against a cleared registry — the
    same pattern as ``tests.mcp_test_fixtures.isolated_registry``, but
    discovered via ``pkgutil`` so a future tool module is covered
    without joining a hand-maintained list. Prior registry state is
    restored on teardown.
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


def test_every_tool_description_prefix_derives_from_the_registry(
    production_tools: dict[str, ToolDefinition],
) -> None:
    """Non-GA tools carry exactly their tier's prefix; others carry none."""
    assert production_tools, "no production tools registered — fixture broken"
    for name, defn in production_tools.items():
        expected_tier = None if defn.feature is None else FEATURE_MATURITY[defn.feature]["maturity"]
        if expected_tier in (None, "ga"):
            assert not defn.description.startswith(_NON_GA_PREFIXES), (
                f"{name}: GA/unclassified tool must carry no maturity prefix"
            )
        else:
            expected_prefix = f"[{expected_tier}] "
            assert defn.description.startswith(expected_prefix), (
                f"{name}: expected description prefix {expected_prefix!r} "
                f"(feature {defn.feature!r})"
            )
            rest = defn.description.removeprefix(expected_prefix)
            assert not rest.startswith(_NON_GA_PREFIXES), f"{name}: description is double-prefixed"


def test_prefix_is_applied_at_registration_and_stays_off_the_wire_fields() -> None:
    """Registration owns the label; ``feature`` never reaches the wire."""
    non_ga_feature = next(key for key, info in FEATURE_MATURITY.items() if info["maturity"] != "ga")
    ga_feature = next(key for key, info in FEATURE_MATURITY.items() if info["maturity"] == "ga")
    tier = FEATURE_MATURITY[non_ga_feature]["maturity"]
    saved = dict(registry_module._TOOLS)
    registry_module._TOOLS.clear()
    try:
        for feature, tool_name in (
            (non_ga_feature, "maturity.test.non_ga"),
            (ga_feature, "maturity.test.ga"),
            (None, "maturity.test.unclassified"),
        ):
            register_mcp_tool(
                ToolDefinition(
                    feature=feature,
                    name=tool_name,
                    description="Does a thing.",
                    inputSchema={"type": "object"},
                ),
                _noop_handler,
            )
        non_ga_defn = registry_module._TOOLS["maturity.test.non_ga"][0]
        assert non_ga_defn.description == f"[{tier}] Does a thing."
        assert registry_module._TOOLS["maturity.test.ga"][0].description == "Does a thing."
        assert (
            registry_module._TOOLS["maturity.test.unclassified"][0].description == "Does a thing."
        )
        wire = non_ga_defn.to_wire()
        assert wire["description"] == f"[{tier}] Does a thing."
        assert "feature" not in wire
    finally:
        registry_module._TOOLS.clear()
        registry_module._TOOLS.update(saved)


def test_unknown_feature_key_is_rejected_at_construction() -> None:
    """A typo'd feature key fails loudly instead of silently un-labelling."""
    with pytest.raises(ValidationError, match="unknown feature key"):
        ToolDefinition(
            feature="not-a-feature",
            name="maturity.test.bogus",
            description="Does a thing.",
            inputSchema={"type": "object"},
        )


# ---------------------------------------------------------------------------
# initialize.instructions maturity band
# ---------------------------------------------------------------------------


def test_band_lists_exactly_the_registrys_non_ga_features() -> None:
    """Every non-GA key appears in the band; every GA key does not."""
    assert FEATURE_MATURITY_BAND.startswith(MATURITY_BLOCK_START)
    assert FEATURE_MATURITY_BAND.endswith(MATURITY_BLOCK_END)
    for key, info in FEATURE_MATURITY.items():
        if info["maturity"] == "ga":
            assert key not in FEATURE_MATURITY_BAND, (
                f"GA feature {key!r} must not be listed in the maturity band"
            )
        else:
            assert key in FEATURE_MATURITY_BAND, (
                f"non-GA feature {key!r} missing from the maturity band"
            )


def test_band_is_empty_when_every_feature_is_ga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-GA registry -> empty band, so the initialize join drops it."""
    monkeypatch.setattr(
        "meho_backplane.mcp.maturity.FEATURE_MATURITY",
        {"everything": {"maturity": "ga"}},
    )
    assert _build_band() == ""


# ---------------------------------------------------------------------------
# OpenAPI x-maturity
# ---------------------------------------------------------------------------


@pytest.fixture
def openapi_schema() -> dict[str, object]:
    """Bust FastAPI's cache and generate a fresh document."""
    app.openapi_schema = None
    return app.openapi()


def test_openapi_tags_carry_registry_derived_x_maturity(
    openapi_schema: dict[str, object],
) -> None:
    """Each mapped, in-use tag advertises its feature's current tier."""
    tags = openapi_schema["tags"]
    assert isinstance(tags, list) and tags
    by_name = {entry["name"]: entry for entry in tags}
    for name, entry in by_name.items():
        assert entry["x-maturity"] == FEATURE_MATURITY[TAG_FEATURE[name]]["maturity"]
    # The BFF console tags are #2677's surface, not the public
    # document's — never labelled here.
    assert not [name for name in by_name if name.startswith("ui")]


def test_openapi_spans_tiers_paths_carry_operation_level_x_maturity(
    openapi_schema: dict[str, object],
) -> None:
    """Override paths exist and each operation carries its own tier."""
    paths = openapi_schema["paths"]
    assert isinstance(paths, dict)
    for path, feature in PATH_FEATURE_OVERRIDES.items():
        assert path in paths, f"override path {path!r} missing from the document"
        expected = FEATURE_MATURITY[feature]["maturity"]
        operations = [
            op
            for method, op in paths[path].items()
            if method in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
        ]
        assert operations
        for op in operations:
            assert op["x-maturity"] == expected
