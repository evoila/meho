# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Values on the approval card of ``k8s.apply``, through the real dispatch.

Each call goes through ``dispatch`` to the stored approval request, over the
stand-in Kubernetes API server of ``test_k8s_apply_card_through_dispatch``.

* A date that YAML reads from the manifest is hidden like text, when it is a
  password on a command line or under a secret name.
* A number YAML reads as ``.nan`` or ``.inf`` is stored as its YAML text, so a
  failed apply still waits for approval.
* A redaction rule for the field path ``manifest.*.metadata.name`` hides the
  name in the subject line, in the "stopped at" sentence and in the error of
  the dry run, for a person and for agents.
* The ``binaryData`` of a Secret inside another object is hidden on the card.
* A ServiceAccount that only names a Secret waits for approval without the
  key of the encrypted handoff.
* A manifest that is not valid YAML tells every reader what the parser found,
  with no text of the manifest.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from meho_backplane.operations import _preview
from meho_backplane.redaction.path_glob import path_matches
from meho_backplane.settings import get_settings
from tests.test_k8s_apply_card_through_dispatch import (
    _bff_env,
    _console_card,
    _kubernetes_registry,
    _login,
    _made_up_name,
    _park,
    _setup,
    _StandInServer,
    _views,
)

__all__ = ["_bff_env", "_kubernetes_registry"]


def _everything_shown(row: Any) -> str:
    """The stored card, the console card and each view of it, as one text."""
    return json.dumps([row.proposed_effect, _views(row)]) + _console_card(row)


def test_a_date_used_as_a_secret_is_hidden_on_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer(refused_kinds=frozenset({"Pod"}))
    _setup(monkeypatch, server)
    manifest = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
        "data: {password: 2001-02-03}\n"
        "---\n"
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        "spec: {containers: [{name: app, image: report:1, args: [--password, 2001-02-04]}]}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert set(effect["manifest"][0]["data"]) == {"password"}
    assert effect["run_commands"][0]["args"][0] == "--password"
    shown = _everything_shown(row)
    for value in ("2001-02-03", "2001-02-04"):
        assert value not in shown


def test_a_number_yaml_reads_as_not_finite_still_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"ConfigMap"}))
    _setup(monkeypatch, server)
    manifest = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: limits, namespace: prod}\n"
        "data: {ratio: .nan, top: .inf, floor: -.inf}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert effect["manifest"][0]["data"] == {"ratio": ".nan", "top": ".inf", "floor": "-.inf"}
    # The database accepts only finite numbers in JSON.
    json.dumps(effect, allow_nan=False)
    assert ".nan" in _console_card(row)


_NAME_RULE = ("manifest.*.metadata.name",)
_HIDDEN_BY_RULE = "[name hidden by a rule]"


def _hide_by_path(node: Any, path: str) -> Any:
    """*node* with each text at a path of :data:`_NAME_RULE` hidden, as a field-path rule does."""
    if isinstance(node, dict):
        return {
            key: _hide_by_path(item, f"{path}.{key}" if path else str(key))
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [_hide_by_path(item, f"{path}.{index}") for index, item in enumerate(node)]
    if isinstance(node, str) and path_matches(_NAME_RULE, path):
        return _HIDDEN_BY_RULE
    return node


def test_a_field_path_rule_for_the_name_hides_it_in_both_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"Service"}))
    _setup(monkeypatch, server)
    real = _preview.apply_connector_boundary_redaction

    def _with_the_rule(raw: Any, **labels: Any) -> Any:
        result = real(raw, **labels)
        return result.model_copy(update={"redacted": _hide_by_path(result.redacted, "")})

    monkeypatch.setattr(_preview, "apply_connector_boundary_redaction", _with_the_rule)
    name = _made_up_name()
    manifest = (
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: web}\n"
        "---\n"
        f"apiVersion: v1\nkind: Service\nmetadata: {{name: {name}, namespace: web}}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    assert effect["manifest"][1]["metadata"]["name"] == _HIDDEN_BY_RULE
    assert effect["subject"] == f"Namespace/{_HIDDEN_BY_RULE}, Service/web/{_HIDDEN_BY_RULE}"
    assert effect["dry_run_stop"] == (
        f"The dry run stopped at document 2 of 2 (Service/web/{_HIDDEN_BY_RULE})."
    )
    labels = json.dumps([effect["manifest"], effect["subject"], effect["dry_run_stop"]])
    assert name not in labels
    # The error names the object as the card does, for a person and for agents.
    assert f"v1/Service web/{_HIDDEN_BY_RULE}" in effect["preview_error"]
    for view_name, view in _views(row).items():
        assert view["preview_error"] == effect["preview_error"], view_name
    assert name not in _everything_shown(row)


def test_the_binary_data_of_a_secret_template_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    value = _made_up_name()
    manifest = (
        "apiVersion: example.test/v1\nkind: Widget\nmetadata: {name: w1, namespace: prod}\n"
        "spec:\n  template:\n    kind: Secret\n    metadata: {name: inner}\n"
        f"    binaryData: {{blob: {value}}}\n"
    )

    row = _park(_login("person-token"), manifest)

    # The storage check and the card read the same Secret fields.
    assert row.params is None
    assert row.execution_handle is not None
    template = row.proposed_effect["preview"]["manifest"][0]["spec"]["template"]
    assert set(template["binaryData"]) == {"blob"}
    assert value not in _everything_shown(row)


def test_a_service_account_that_names_a_secret_waits_without_the_handoff_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    manifest = (
        "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: builder, namespace: ci}\n"
        "secrets: [{apiVersion: v1, kind: Secret, name: builder-token}]\n"
    )

    row = _park(_login("person-token"), manifest)

    # A reference holds no Secret value: the params stay on the row, and no
    # encrypted handoff is needed.
    assert row.params == {"manifest": manifest}
    assert row.execution_handle is None


def test_an_unreadable_manifest_tells_every_reader_what_the_parser_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)

    row = _park(_login("person-token"), "a: [b")

    for name, view in _views(row).items():
        assert view["preview_unavailable"] is True, name
        assert "expected ',' or ']'" in view["preview_error"], name
        assert "(line 1, column 6)" in view["preview_error"], name
        assert "a: [b" not in view["preview_error"], name
