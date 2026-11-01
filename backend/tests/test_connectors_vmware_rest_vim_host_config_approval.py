# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Older-API host time, name, logging and service changes wait for approval (#4351).

The typed ``host.ntp.set``, ``host.dns.set``, ``host.syslog.set`` and
``host.service_control`` are ``dangerous`` and wait for approval. The generic
vCenter calls they use (VI-JSON, a ``POST`` to ``/<Type>/{moId}/<Method>``)
got ``caution`` without approval from the parser. The VMware floor
(``ingest_safety.py``) now raises them to ``dangerous`` with approval on the
8.0 and 9.0 catalogs. Reads on the same managers keep their level.

The pinned-spec lane checks every rule against the vendor file on the spec
shelf; it skips when the shelf is not configured (see ``tests/_spec_shelf.py``)
and runs in CI.
"""

from __future__ import annotations

import re

import pytest

from meho_backplane.connectors.vmware_rest.ingest_safety import (
    VMWARE_VIM_HOST_CONFIG_WRITES,
    is_vmware_vim_host_config_write,
    vmware_rest_safety_floor,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_UPDATE_DNS = "POST:/HostNetworkSystem/{moId}/UpdateDnsConfig"

#: Raised to ``dangerous`` with approval.
_GATED = frozenset(
    {
        "POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig",
        _UPDATE_DNS,
        "POST:/OptionManager/{moId}/UpdateOptions",
        "POST:/HostServiceSystem/{moId}/UpdateServicePolicy",
        "POST:/HostServiceSystem/{moId}/StartService",
        "POST:/HostServiceSystem/{moId}/StopService",
        "POST:/HostServiceSystem/{moId}/RestartService",
    }
)

#: Reads on the same managers keep their parsed level.
_UNCHANGED = frozenset(
    {
        "GET:/HostDateTimeSystem/{moId}/dateTimeInfo",
        "GET:/HostNetworkSystem/{moId}/dnsConfig",
        "GET:/HostServiceSystem/{moId}/serviceInfo",
        "GET:/OptionManager/{moId}/setting",
        "POST:/OptionManager/{moId}/QueryOptions",
    }
)


def _hand_made_spec() -> str:
    """One small OpenAPI file with every gated op and the reads that stay."""
    lines = [
        "openapi: 3.0.3",
        "info: {title: vim host config fixture, version: '1.0'}",
        "paths:",
    ]
    by_path: dict[str, list[str]] = {}
    for op_id in sorted(_GATED | _UNCHANGED):
        method, path = op_id.split(":", 1)
        by_path.setdefault(path, []).append(method)
    for path, methods in sorted(by_path.items()):
        lines.append(f"  '{path}':")
        for method in methods:
            lines.append(f"    {method.lower()}:")
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


def _parsed() -> list[EndpointDescriptorProto]:
    return parse_openapi("file:///vim-host-config.yaml", content=_hand_made_spec())


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_every_host_change_waits_for_approval(version: str) -> None:
    ops = _floored(_parsed(), version)

    assert set(ops) == _GATED | _UNCHANGED
    for op_id in _GATED:
        assert ops[op_id].safety_level == "dangerous", op_id
        assert ops[op_id].requires_approval is True, op_id


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_reads_on_the_same_managers_keep_their_level(version: str) -> None:
    rows = {p.op_id: p for p in _parsed()}
    ops = _floored(_parsed(), version)

    for op_id in _UNCHANGED:
        assert ops[op_id].safety_level == rows[op_id].safety_level, op_id
        assert ops[op_id].requires_approval is False, op_id


def test_rules_do_not_apply_to_an_unlisted_catalog_version() -> None:
    ops = _floored(_parsed(), "7.0")

    assert ops[_UPDATE_DNS].safety_level == "caution"
    assert ops[_UPDATE_DNS].requires_approval is False


def test_no_rule_lowers_a_level() -> None:
    """A row that is already ``destructive`` stays ``destructive`` with approval."""
    method, path = _UPDATE_DNS.split(":", 1)
    proto = EndpointDescriptorProto(
        op_id=_UPDATE_DNS,
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


_RULE_CASES: list[tuple[str, str, bool]] = [
    ("POST", "/HostNetworkSystem/{moId}/UpdateDnsConfig", True),
    ("post", "/api/HostServiceSystem/{id}/RestartService", True),
    ("POST", "/OptionManager/{moId}/UpdateOptions", True),
    ("GET", "/HostNetworkSystem/{moId}/UpdateDnsConfig", False),
    ("POST", "/HostNetworkSystem/{moId}/UpdateDnsConfig?x=1", False),
    ("POST", "/OptionManager/{moId}/QueryOptions", False),
    ("POST", "/HostServiceSystem/{moId}/RefreshServices", False),
]


@pytest.mark.parametrize(("method", "path", "expected"), _RULE_CASES)
def test_rule_matching(method: str, path: str, expected: bool) -> None:
    assert is_vmware_vim_host_config_write(method, path) is expected


def test_every_rule_is_in_the_test_table() -> None:
    """The floor list and this test's list are the same operations."""
    keys = set()
    for op_id in _GATED:
        method, path = op_id.split(":", 1)
        keys.add((method, re.sub(r"\{[^/{}]+\}", "{}", path)))

    assert keys == VMWARE_VIM_HOST_CONFIG_WRITES


@pytest.mark.parametrize(("label", "version"), [("vcenter-9.0", "9.0")])
def test_pinned_vi_json_serves_the_rules(label: str, version: str) -> None:
    """Every rule matches an operation in the pinned file, and lands waiting for approval."""
    spec_path = require_shelf_spec(label, "vi-json.yaml")
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served(_GATED, set(ops), spec_label=f"{label}/vi-json.yaml")
    for op_id in _GATED:
        assert ops[op_id].safety_level == "dangerous", op_id
        assert ops[op_id].requires_approval is True, op_id
