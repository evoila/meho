# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Generic vCenter stop, restart, disconnect, remediate and access calls wait for approval (#4328).

As generic (ingested) writes these operations were ``caution`` without
approval, so a person's call ran at once. The VMware floor
(``ingest_safety.py``, ``VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES``) raises
them to ``dangerous`` with approval on the 8.0 and 9.0 catalogs.

The pinned-spec lane checks every rule against the vendor files on the spec
shelf; it skips when the shelf is not configured (see ``tests/_spec_shelf.py``)
and runs in CI.
"""

from __future__ import annotations

import re

import pytest

from meho_backplane.connectors.vmware_rest.ingest_safety import (
    VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES,
    is_vmware_stop_restart_or_remediate_write,
    vmware_rest_safety_floor,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

#: One entry per family of the task, every operation of it in the pinned files.
_GATED = frozenset(
    {
        # Guest shutdown, reboot and standby.
        "POST:/vcenter/vm/{vm}/guest/power?action=shutdown",
        "POST:/vcenter/vm/{vm}/guest/power?action=reboot",
        "POST:/vcenter/vm/{vm}/guest/power?action=standby",
        # Appliance power off and reboot.
        "POST:/appliance/shutdown?action=poweroff",
        "POST:/appliance/shutdown?action=reboot",
        # Service stop and restart.
        "POST:/appliance/services/{service}?action=stop",
        "POST:/appliance/services/{service}?action=restart",
        "POST:/vcenter/services/{service}?action=stop",
        "POST:/vcenter/services/{service}?action=restart",
        # SSH, shell, console and DCUI access.
        "PUT:/appliance/access/ssh",
        "PUT:/appliance/access/shell",
        "PUT:/appliance/access/consolecli",
        "PUT:/appliance/access/dcui",
        # Host disconnect.
        "POST:/vcenter/host/{host}?action=disconnect",
        # Turn off a Supervisor.
        "POST:/vcenter/namespace-management/clusters/{cluster}?action=disable",
        # Stage or apply a software image to a cluster or a host.
        "POST:/esx/settings/clusters/{cluster}/software?action=apply&vmw-task=true",
        "POST:/esx/settings/clusters/{cluster}/software?action=stage&vmw-task=true",
        "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true",
        "POST:/esx/settings/hosts/{host}/software?action=stage&vmw-task=true",
        # Install an appliance update.
        "POST:/appliance/update/pending/{version}?action=install",
    }
)

#: Other actions on the same paths, and reads sent as POST, keep their level.
_UNCHANGED = frozenset(
    {
        "POST:/appliance/shutdown?action=cancel",
        "POST:/appliance/services/{service}?action=start",
        "POST:/vcenter/services/{service}?action=start",
        "PATCH:/vcenter/services/{service}",
        "POST:/vcenter/host/{host}?action=connect",
        "POST:/vcenter/namespace-management/clusters/{cluster}?action=enable",
        "POST:/esx/settings/clusters/{cluster}/software?action=check&vmw-task=true",
        "POST:/esx/settings/hosts/{host}/software?action=scan&vmw-task=true",
        "POST:/esx/settings/hosts/{host}/software?action=export",
        "POST:/appliance/update/pending/{version}?action=precheck",
        "POST:/vcenter/vm/{vm}/guest/filesystem?action=list",
        "GET:/appliance/access/ssh",
    }
)


def _hand_made_spec() -> str:
    """One small OpenAPI file with every gated op and the neighbours that stay."""
    paths: dict[str, list[str]] = {}
    for op_id in sorted(_GATED | _UNCHANGED):
        method, path = op_id.split(":", 1)
        paths.setdefault(path, []).append(method.lower())
    lines = [
        "openapi: 3.0.3",
        "info: {title: vcenter stop and restart fixture, version: '1.0'}",
        "paths:",
    ]
    for path, methods in paths.items():
        lines.append(f"  '{path}':")
        for method in methods:
            lines.append(f"    {method}:")
            lines.append("      responses: {'200': {description: ok}}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _floored(
    protos: list[EndpointDescriptorProto], version: str
) -> dict[str, EndpointDescriptorProto]:
    return {
        p.op_id: apply_safety_floor(
            product="vmware", version=version, impl_id="vmware-rest", proto=p
        )
        for p in protos
    }


def _parsed() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///stop-restart.yaml", content=_hand_made_spec())
    return {p.op_id: p for p in rows}


def _ingested(version: str) -> dict[str, EndpointDescriptorProto]:
    return _floored(list(_parsed().values()), version)


# ---------------------------------------------------------------------------
# Ingest of a hand-made spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_every_family_lands_dangerous_with_approval(version: str) -> None:
    parsed = _parsed()
    ops = _ingested(version)

    assert set(ops) == _GATED | _UNCHANGED
    for op_id in _GATED:
        assert parsed[op_id].safety_level == "caution", op_id
        assert ops[op_id].safety_level == "dangerous", op_id
        assert ops[op_id].requires_approval is True, op_id


@pytest.mark.parametrize("version", ["8.0", "9.0"])
@pytest.mark.parametrize("op_id", sorted(_UNCHANGED))
def test_other_actions_and_reads_keep_their_level(version: str, op_id: str) -> None:
    parsed = _parsed()
    ops = _ingested(version)

    assert ops[op_id].safety_level == parsed[op_id].safety_level
    assert ops[op_id].requires_approval is False


def test_rules_do_not_apply_to_an_unlisted_catalog_version() -> None:
    ops = _ingested("7.0")

    for op_id in _GATED:
        assert ops[op_id].safety_level == "caution", op_id
        assert ops[op_id].requires_approval is False, op_id


@pytest.mark.parametrize("op_id", sorted(_GATED))
def test_no_rule_lowers_a_level(op_id: str) -> None:
    """A row that is already ``destructive`` stays ``destructive`` with approval."""
    method, path = op_id.split(":", 1)
    proto = EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


#: ``(method, path, the rule accepts it)``.
_RULE_CASES: list[tuple[str, str, bool]] = [
    ("POST", "/vcenter/vm/{vm}/guest/power?action=reboot", True),
    ("POST", "/api/vcenter/vm/{vm_id}/guest/power?action=reboot", True),
    ("post", "/appliance/shutdown?action=poweroff", True),
    ("PUT", "/api/appliance/access/ssh", True),
    ("POST", "/vcenter/host/{host_id}?action=disconnect", True),
    ("GET", "/appliance/access/ssh", False),
    ("PATCH", "/appliance/access/ssh", False),
    ("POST", "/appliance/shutdown?action=cancel", False),
    ("POST", "/vcenter/services/{service}?action=start", False),
    ("POST", "/vcenter/host/{host}?action=connect", False),
    ("POST", "/esx/settings/hosts/{host}/software?action=apply", False),
    ("POST", "/esx/settings/hosts/{host}/software?action=check&vmw-task=true", False),
    ("POST", "/appliance/update/pending/{version}?action=stage", False),
]


@pytest.mark.parametrize(("method", "path", "expected"), _RULE_CASES)
def test_is_vmware_stop_restart_or_remediate_write(method: str, path: str, expected: bool) -> None:
    assert is_vmware_stop_restart_or_remediate_write(method, path) is expected


def test_every_floor_rule_is_in_the_test_table() -> None:
    """The floor list and this test's list are the same operations."""
    listed = set()
    for op_id in _GATED:
        method, path = op_id.split(":", 1)
        listed.add((method, re.sub(r"\{[^/{}]+\}", "{}", path)))
    assert listed == VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES


# ---------------------------------------------------------------------------
# The pinned vendor files serve the rules (shelf-gated, runs in CI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product_dir", "version"), [("vcenter-9.0", "9.0"), ("vcenter-8.0", "8.0")]
)
def test_pinned_vendor_spec_serves_the_rules(product_dir: str, version: str) -> None:
    """Every rule matches an operation in the pinned file, and lands waiting for approval."""
    spec_path = require_shelf_spec(product_dir, "vcenter.yaml")
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served(_GATED, set(ops), spec_label=f"{product_dir}/vcenter.yaml")
    not_gated = sorted(
        op_id
        for op_id in _GATED
        if ops[op_id].safety_level != "dangerous" or ops[op_id].requires_approval is not True
    )
    assert not_gated == []
