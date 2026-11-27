# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the agent and the person read about the dry run of ``k8s.apply``.

* With ``K8S_APPLY_PREVIEW_DRY_RUN`` on, the full text, the short text and
  the hosted agent's text of ``preview_operation`` say that the preview of
  ``k8s.apply`` sends a dry run, and none says that nothing is sent. Off,
  each text is the registered one.
* With the switch on, the output schema says that ``proposed_effect`` can
  come from a dry run on the target and that ``plan_shown`` can be false next
  to it. Off, the schema is the registered one.
* The web console shows a plan that checked only part of the manifest
  together with its ``plan_note``. (The CLI test is
  ``TestPrintPreviewResultShowsAPartPlanAndItsNote`` in
  ``cli/internal/cmd/operation/operation_test.go``.)
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.agent import toolset
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import ToolDefinition, tool_for_settings
from meho_backplane.mcp.tools._preview_typed_writes_text import (
    K8S_APPLY_DRY_RUN_SCHEMA_TEXTS,
    K8S_APPLY_DRY_RUN_SHORT_SWAP,
    K8S_APPLY_DRY_RUN_TEXT_SWAPS,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.templating import get_templates
from tests.mcp_test_fixtures import build_operator
from tests.test_k8s_apply_card_through_dispatch import _kubernetes_registry
from tests.test_k8s_apply_preview_dry_run import (
    _THREE_OBJECTS,
    _preview,
    _setup,
    _StandInCluster,
)
from tests.test_ui_approvals import _bff_env

__all__ = ["_bff_env", "_kubernetes_registry"]

_SWITCH = "K8S_APPLY_PREVIEW_DRY_RUN"
_SENDS_NOTHING = ("Nothing is sent", "it never sends the request")


@pytest.fixture(params=[False, True], ids=["switch-off", "switch-on"])
def on(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    monkeypatch.setenv(_SWITCH, "true" if request.param else "false")
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


def _registered() -> ToolDefinition:
    importlib.import_module("meho_backplane.mcp.tools.operations")  # registers the tool
    definition: ToolDefinition = mcp_registry._TOOLS["preview_operation"][0]
    return definition


def _shown(monkeypatch: pytest.MonkeyPatch, *, short: bool) -> dict[str, Any]:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if short else "false")
    get_settings.cache_clear()
    wire: dict[str, Any] = tool_for_settings(_registered(), build_operator()).to_wire()
    return wire


def test_the_full_text_names_the_dry_run_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    registered = _registered().description
    for old, _new in K8S_APPLY_DRY_RUN_TEXT_SWAPS:
        assert old in registered

    description = _shown(monkeypatch, short=False)["description"]

    if on:
        assert all(new in description for _old, new in K8S_APPLY_DRY_RUN_TEXT_SWAPS)
        assert not any(text in description for text in _SENDS_NOTHING)
    else:
        assert description.endswith(registered)


def test_the_short_text_names_the_dry_run_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    old, new = K8S_APPLY_DRY_RUN_SHORT_SWAP

    description = _shown(monkeypatch, short=True)["description"]

    if on:
        assert new in description
        assert not any(text in description for text in _SENDS_NOTHING)
    else:
        assert old in description


def test_the_hosted_agent_text_names_the_dry_run_only_with_the_switch_on(on: bool) -> None:
    hosted = {spec.name: spec for spec in toolset._active_catalog()}["preview_operation"]

    if on:
        assert all(new in hosted.description for _old, new in K8S_APPLY_DRY_RUN_TEXT_SWAPS)
        assert not any(text in hosted.description for text in _SENDS_NOTHING)
    else:
        assert hosted.description == _registered().description


def test_the_output_schema_names_the_dry_run_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    registered = _registered().outputSchema
    assert registered is not None

    shown = _shown(monkeypatch, short=False)["outputSchema"]["properties"]

    for name, text in K8S_APPLY_DRY_RUN_SCHEMA_TEXTS.items():
        expected = text if on else registered["properties"][name]["description"]
        assert shown[name]["description"] == expected


def test_the_console_shows_a_part_plan_with_its_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SWITCH, "true")
    get_settings.cache_clear()
    cluster = _StandInCluster()

    async def _discover(*, api_version: str, kind: str) -> Any:
        if kind == "Widget":
            raise LookupError("no kind Widget is known")
        return SimpleNamespace(kind=kind, namespaced=True)

    cluster.resources = SimpleNamespace(get=_discover)
    _setup(monkeypatch, cluster)
    manifest = _THREE_OBJECTS.replace("kind: Ingress", "kind: Widget")
    envelope = _preview(manifest)
    assert envelope["plan_shown"] is False
    assert envelope["proposed_effect"]["preview"]["not_checked"] == [3]

    html = (
        get_templates()
        .env.get_template("operations/_preview.html")
        .render(envelope=envelope, error_message=None, connector_id="k8s-1.x", op_id="k8s.apply")
    )

    assert "data-preview-effect" in html
    assert "not_checked" in html
    assert "data-preview-plan-note" in html
    assert "could not check every part" in html
