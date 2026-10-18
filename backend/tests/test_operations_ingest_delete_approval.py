# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested deletes wait for approval (#3827).

An ingested ``DELETE`` used to land ``dangerous`` with
``requires_approval=False``. A human operator seat ran it at once, with no
approval. The parser now sets ``requires_approval=True`` on every ``DELETE``,
and the VMware floor does the same for the vCenter tag detach actions, which
are ``POST`` calls.

The guard tests check that no ``DELETE`` on a spec MEHO ships, or on a pinned
vendor spec, lands without approval. The pinned-spec lanes skip when the spec
shelf is not configured (see ``tests/_spec_shelf.py``).
"""

from __future__ import annotations

from importlib import resources

import pytest

from meho_backplane.connectors.vmware_rest.ingest_safety import vmware_rest_safety_floor
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import SPEC_RESOURCE_PACKAGE, load_catalog
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_GATED_LEVELS = frozenset({"dangerous", "destructive"})

_TAG_DETACH_OP_IDS = frozenset(
    {
        "POST:/cis/tagging/tag-association/{tagId}?action=detach",
        "POST:/cis/tagging/tag-association?action=detach-multiple-tags-from-object",
        "POST:/cis/tagging/tag-association/{tagId}?action=detach-tag-from-multiple-objects",
    }
)


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _ungated(protos: list[EndpointDescriptorProto], op_ids: set[str]) -> list[str]:
    """Return the op_ids in *op_ids* that would run without approval."""
    return sorted(
        f"{p.op_id} ({p.safety_level}, requires_approval={p.requires_approval})"
        for p in protos
        if p.op_id in op_ids and not (p.safety_level in _GATED_LEVELS and p.requires_approval)
    )


def _floored(
    protos: list[EndpointDescriptorProto], *, product: str, version: str, impl_id: str
) -> list[EndpointDescriptorProto]:
    return [
        apply_safety_floor(product=product, version=version, impl_id=impl_id, proto=p)
        for p in protos
    ]


def test_parser_makes_every_delete_wait_for_approval() -> None:
    rows = parse_openapi(
        "file:///delete-approval.yaml",
        content="""
openapi: 3.0.3
info: {title: delete approval fixture, version: '1.0'}
paths:
  /cis/tagging/category/{categoryId}:
    get:
      responses: {'200': {description: ok}}
    patch:
      responses: {'204': {description: ok}}
    delete:
      responses: {'204': {description: deleted}}
  /cis/tagging/tag/{tagId}:
    delete:
      operationId: deleteTag
      responses: {'204': {description: deleted}}
""",
    )
    ops = {row.op_id: row for row in rows}

    for op_id in ("DELETE:/cis/tagging/category/{categoryId}", "DELETE:/cis/tagging/tag/{tagId}"):
        assert ops[op_id].safety_level == "dangerous"
        assert ops[op_id].requires_approval is True
    assert ops["GET:/cis/tagging/category/{categoryId}"].requires_approval is False
    assert ops["PATCH:/cis/tagging/category/{categoryId}"].requires_approval is False


@pytest.mark.parametrize("version", ["8.0", "9.0"])
@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("op_id", sorted(_TAG_DETACH_OP_IDS))
def test_vmware_floor_makes_tag_detach_wait_for_approval(
    op_id: str, prefix: str, version: str
) -> None:
    method, path = op_id.split(":", 1)
    proto = EndpointDescriptorProto(
        op_id=f"{method}:{prefix}{path}",
        method=method,
        path=f"{prefix}{path}",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="caution",
    )

    floored = apply_safety_floor(
        product="vmware", version=version, impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "dangerous"
    assert floored.requires_approval is True


@pytest.mark.parametrize(
    "path",
    [
        "/cis/tagging/tag-association/{tagId}?action=attach",
        "/cis/tagging/tag-association?action=list-attached-tags",
        "/cis/tagging/tag-association/{tagId}?action=list-attached-objects",
        "/cis/tagging/tag-associations?action=detach",
    ],
)
def test_vmware_floor_leaves_other_tag_association_actions_alone(path: str) -> None:
    proto = EndpointDescriptorProto(
        op_id=f"POST:{path}",
        method="POST",
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level="caution",
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored is proto


def _shipped_specs() -> list[tuple[str, tuple[str, str, str] | None]]:
    """Every spec file MEHO ships, with its catalog triple when a row names it."""
    triples = {
        entry.spec_resource: (entry.product, entry.version, entry.impl_id)
        for entry in load_catalog().entries
        if entry.spec_resource
    }
    names = sorted(
        item.name
        for item in resources.files(SPEC_RESOURCE_PACKAGE).iterdir()
        if item.name.endswith((".yaml", ".yml", ".json"))
    )
    return [(name, triples.get(name)) for name in names]


@pytest.mark.parametrize(("spec_resource", "triple"), _shipped_specs())
def test_no_shipped_spec_delete_runs_without_approval(
    spec_resource: str, triple: tuple[str, str, str] | None
) -> None:
    text = resources.files(SPEC_RESOURCE_PACKAGE).joinpath(spec_resource).read_text("utf-8")
    protos = parse_openapi(f"file:///{spec_resource}", content=text)
    if triple is not None:
        product, version, impl_id = triple
        protos = _floored(protos, product=product, version=version, impl_id=impl_id)

    deletes = {p.op_id for p in protos if p.method == "DELETE"}

    assert _ungated(protos, deletes) == []


def _pinned_protos(product_dir: str, filename: str) -> list[EndpointDescriptorProto]:
    spec_path = require_shelf_spec(product_dir, filename)
    return parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))


def test_pinned_vcenter_spec_deletes_and_tag_detach_wait_for_approval() -> None:
    protos = _floored(
        _pinned_protos("vcenter-9.0", "vcenter.yaml"),
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )
    served = {p.op_id for p in protos}
    assert_op_ids_served(set(_TAG_DETACH_OP_IDS), served, spec_label="vcenter-9.0/vcenter.yaml")

    gated = {p.op_id for p in protos if p.method == "DELETE"} | set(_TAG_DETACH_OP_IDS)

    assert _ungated(protos, gated) == []


def test_pinned_nsx_spec_deletes_wait_for_approval() -> None:
    protos = _pinned_protos("nsx-9.0", "nsx-openapi.json")
    deletes = {p.op_id for p in protos if p.method == "DELETE"}

    assert deletes
    assert _ungated(protos, deletes) == []
