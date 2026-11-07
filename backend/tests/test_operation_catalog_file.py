# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of the operation catalog file that each release publishes.

The release workflow ``.github/workflows/operation-catalog.yml`` writes the
file with ``backend/scripts/operation_catalog.py``. Automation code checks
its calls against the file with a plain JSON Schema 2020-12 validator and no
running MEHO. So the main test here is that such a validator, given the
schema from the file, says the same as ``validate_params`` for the same
params.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from scripts.operation_catalog import (
    build_catalog,
    collect_operations,
    connector_id,
    diff_catalogs,
    main,
    result_shape,
)
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sample_params(schema: dict[str, Any]) -> list[Any]:
    """Params to try on one operation: good ones, and ones that must fail."""
    examples = [e for e in schema.get("examples", []) if isinstance(e, dict)]
    samples: list[Any] = [{}, {"not_a_parameter_of_this_op": "x"}, [], "text", *examples]
    for example in examples:
        for name in example:
            wrong_type = copy.deepcopy(example)
            wrong_type[name] = {"wrong": [1, 2]}
            samples.append(wrong_type)
    return samples


async def _stored_schemas() -> dict[tuple[str, str], dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.owner_package.is_(None),
                    EndpointDescriptor.source_kind.in_(("typed", "composite")),
                )
            )
        ).scalars()
        return {
            (connector_id(r.impl_id, r.version, r.product), r.op_id): r.parameter_schema or {}
            for r in rows
        }


@pytest.mark.asyncio
async def test_catalog_schema_and_validate_params_give_the_same_answer() -> None:
    """For sample params of each op, the file's schema and ``validate_params`` agree."""
    operations = await collect_operations()
    # The file is JSON: check the schema as a reader of the file gets it.
    catalog = json.loads(json.dumps(build_catalog("v0.0.0", operations)))
    stored = await _stored_schemas()
    assert {(op["connector_id"], op["op_id"]) for op in catalog["operations"]} == set(stored)

    answers = {True: 0, False: 0}
    disagree = []
    for op in catalog["operations"]:
        offline = Draft202012Validator(op["parameter_schema"])
        stored_schema = stored[(op["connector_id"], op["op_id"])]
        for params in _sample_params(op["parameter_schema"]):
            file_ok = offline.is_valid(params)
            server_ok = not validate_params(stored_schema, params)
            answers[server_ok] += 1
            if file_ok != server_ok:
                disagree.append(f"{op['op_id']}: {params!r} file={file_ok} server={server_ok}")
    assert not disagree, "\n".join(disagree[:20])
    # Not a test of nothing: some samples pass and many fail.
    assert answers[True] > 100
    assert answers[False] > 1000


@pytest.mark.asyncio
async def test_catalog_entries_carry_the_fields_automation_needs() -> None:
    """Each entry names its connector, risk level, approval flag and version range."""
    operations = await collect_operations()
    assert operations == sorted(operations, key=lambda e: (e["connector_id"], e["op_id"]))
    by_id = {op["op_id"]: op for op in operations}
    pod_list = by_id["k8s.pod.list"]
    assert pod_list["connector_id"] == "k8s-1.x"
    assert pod_list["product"] == "k8s"
    assert pod_list["kind"] == "typed"
    assert pod_list["safety_level"] == "safe"
    assert pod_list["requires_approval"] is False
    assert {"safety_level", "requires_approval", "supported_versions", "result_shape"} <= set(
        pod_list
    )
    assert any(op["supported_versions"] for op in operations)
    for op in operations:
        assert op["safety_level"] in {"safe", "caution", "dangerous", "destructive"}
        assert isinstance(op["requires_approval"], bool)


def test_result_shape_names_the_top_level_fields() -> None:
    assert result_shape(None) is None
    assert result_shape({}) is None
    assert result_shape({"type": "object", "properties": {"b": {}, "a": {}}}) == {
        "type": "object",
        "fields": ["a", "b"],
    }
    assert result_shape({"type": "array", "items": {"properties": {"name": {}}}}) == {
        "type": "array",
        "item_fields": ["name"],
    }
    assert result_shape({"type": "object"}) == {"type": "object"}


def _op(op_id: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "connector_id": "demo-1.x",
        "op_id": op_id,
        "safety_level": "safe",
        "requires_approval": False,
        "parameter_schema": {"type": "object"},
        "summary": "A demo op.",
    }
    entry.update(fields)
    return entry


def test_changed_risk_level_shows_under_changed() -> None:
    old = build_catalog("v1.0.0", [_op("demo.vm.get"), _op("demo.vm.list")])
    new = build_catalog(
        "v1.1.0",
        [_op("demo.vm.get", safety_level="caution", requires_approval=True), _op("demo.vm.list")],
    )
    changes = diff_catalogs(old, new)
    assert changes["release"] == "v1.1.0"
    assert changes["previous_release"] == "v1.0.0"
    assert changes["added"] == []
    assert changes["removed"] == []
    assert changes["changed"] == [
        {
            "connector_id": "demo-1.x",
            "op_id": "demo.vm.get",
            "changes": [
                {"field": "safety_level", "before": "safe", "after": "caution"},
                {"field": "requires_approval", "before": False, "after": True},
            ],
        }
    ]


def test_added_removed_and_schema_changes_are_listed() -> None:
    old = build_catalog("v1.0.0", [_op("demo.vm.get"), _op("demo.vm.old")])
    new = build_catalog(
        "v1.1.0",
        [
            _op("demo.vm.get", parameter_schema={"type": "object", "required": ["id"]}),
            _op("demo.vm.new"),
        ],
    )
    changes = diff_catalogs(old, new)
    assert changes["added"] == [{"connector_id": "demo-1.x", "op_id": "demo.vm.new"}]
    assert changes["removed"] == [{"connector_id": "demo-1.x", "op_id": "demo.vm.old"}]
    assert changes["changed"] == [
        {
            "connector_id": "demo-1.x",
            "op_id": "demo.vm.get",
            "changes": [{"field": "parameter_schema"}],
        }
    ]


def test_a_summary_change_alone_is_not_a_change() -> None:
    old = build_catalog("v1.0.0", [_op("demo.vm.get")])
    new = build_catalog("v1.1.0", [_op("demo.vm.get", summary="New words.")])
    assert diff_catalogs(old, new)["changed"] == []


def test_first_catalog_lists_every_operation_as_added() -> None:
    new = build_catalog("v1.0.0", [_op("demo.vm.list"), _op("demo.vm.get")])
    changes = diff_catalogs(None, new)
    assert changes["previous_release"] is None
    assert [a["op_id"] for a in changes["added"]] == ["demo.vm.get", "demo.vm.list"]
    assert changes["removed"] == []
    assert changes["changed"] == []


def test_the_release_commands_write_both_files(tmp_path: Path) -> None:
    """The two commands the release workflow runs write the two files."""
    catalog_path = tmp_path / "operation-catalog.json"
    changes_path = tmp_path / "operation-catalog-changes.json"
    assert main(["export", "--release", "v9.9.9", "--out", str(catalog_path)]) == 0
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["format_version"] == 1
    assert catalog["release"] == "v9.9.9"
    assert len(catalog["operations"]) > 100

    previous = copy.deepcopy(catalog)
    previous["release"] = "v9.9.8"
    first = previous["operations"][0]
    first["safety_level"] = "destructive" if first["safety_level"] != "destructive" else "safe"
    old_path = tmp_path / "previous.json"
    old_path.write_text(json.dumps(previous), encoding="utf-8")
    assert (
        main(
            [
                "diff",
                "--new",
                str(catalog_path),
                "--old",
                str(old_path),
                "--out",
                str(changes_path),
            ]
        )
        == 0
    )
    changes = json.loads(changes_path.read_text(encoding="utf-8"))
    assert changes["previous_release"] == "v9.9.8"
    assert [c["op_id"] for c in changes["changed"]] == [first["op_id"]]
    assert changes["changed"][0]["changes"][0]["field"] == "safety_level"
