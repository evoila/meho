# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings file refuses YAML anchors and aliases; the export never writes one."""

from __future__ import annotations

from typing import Any, cast

import pytest

from meho_backplane.instance_settings import schema
from meho_backplane.instance_settings.schema import parse_settings_file
from meho_backplane.instance_settings.service import build_plan, dump_settings_yaml

_HEAD = "apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"

#: Each level points ten times at the level below it.
_NESTED_ALIASES = (
    "apiVersion: meho.evoila.com/v1\nkind: Settings\n"
    "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
    "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
    "d: [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
)


@pytest.fixture
def secret_walk_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def _record(*args: Any, **kwargs: Any) -> list[str]:
        calls.append(args)
        return []

    monkeypatch.setattr(schema, "find_secret_shaped_values", _record)
    return calls


@pytest.mark.parametrize(
    ("content", "line"),
    [
        pytest.param(_NESTED_ALIASES, 3, id="nested-aliases"),
        pytest.param(
            _HEAD + "    - targets:\n"
            "        - name: a\n          product: ssh\n          host: h\n"
            "          extras: &shared {site: one}\n"
            "        - name: b\n          product: ssh\n          host: h\n"
            "          extras: *shared\n",
            9,
            id="anchor-then-alias",
        ),
        pytest.param(
            _HEAD + "    - targets:\n"
            "        - name: a\n          product: ssh\n          host: h\n"
            "          extras: {<<: *missing}\n",
            9,
            id="merge-key-alias",
        ),
        pytest.param(
            _HEAD + "    - targets:\n"
            "        - name: a\n          product: ssh\n          host: &h h\n",
            8,
            id="anchor-alone",
        ),
    ],
)
def test_anchors_and_aliases_are_refused_before_the_secret_walk(
    content: str, line: int, secret_walk_calls: list[object]
) -> None:
    parsed = parse_settings_file(content.encode())
    assert parsed.model is None
    assert parsed.mistakes == [
        f"The file is not valid YAML on line {line}: anchors (`&`) and aliases (`*`) "
        "are not accepted; write each value out."
    ]
    assert secret_walk_calls == []


def test_anchor_name_is_not_shown_back() -> None:
    parsed = parse_settings_file((_HEAD + "    - targets: &eyJhbGciOiJIUzI1NiJ9 []\n").encode())
    (mistake,) = parsed.mistakes
    assert "eyJ" not in mistake


async def test_preview_refuses_aliases_before_any_database_read() -> None:
    plan = await build_plan(cast(Any, None), cast(Any, None), _NESTED_ALIASES.encode())
    assert plan.mistakes == [
        "The file is not valid YAML on line 3: anchors (`&`) and aliases (`*`) "
        "are not accepted; write each value out."
    ]


def test_export_writes_shared_values_out_and_imports_again() -> None:
    shared_extras = {"site": "one", "rack": "r1"}
    shared_aliases = ["a.example", "b.example"]
    document = {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {
            "tenants": [
                {
                    "targets": [
                        {
                            "name": name,
                            "product": "ssh",
                            "host": "h",
                            "aliases": shared_aliases,
                            "extras": shared_extras,
                        }
                        for name in ("a", "b")
                    ]
                }
            ]
        },
    }

    exported = dump_settings_yaml(document)

    assert "&id" not in exported
    assert "*id" not in exported
    parsed = parse_settings_file(exported.encode())
    assert parsed.mistakes == []
    assert parsed.model is not None
    targets = parsed.model.spec.tenants[0].targets or []
    assert [t.extras for t in targets] == [shared_extras, shared_extras]
    assert [t.aliases for t in targets] == [shared_aliases, shared_aliases]
