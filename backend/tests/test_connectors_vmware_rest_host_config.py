# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the host settings read and the NTP, DNS and syslog changes.

* ``vmware.host.config.show``: every part, a part vSphere cannot read, the host
  lookup refusals, and that no SNMP community string reaches the answer.
* ``vmware.composite.host.ntp.set`` / ``.dns.set`` / ``.syslog.set``: the exact
  vim bodies, the calls sent only when something changes, the read-back, a
  step that waits for approval, a vendor fault, a read-back that does not
  match, and the approval preview.
* ``vmware.composite.host.service_control``: the three new services and the
  read-back of the service.
* The contract of each operation: risk level, approval, examples, patterns.

The transport arms (real HTTP on the vCenter 8 and 9 paths) and the flow
through the dispatcher are in ``test_connectors_vmware_rest_host_config_flow.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._host import service_control_composite
from meho_backplane.connectors.vmware_rest.composites._host_config_ops import (
    HOST_CONFIG_COMPOSITES,
    HOST_CONFIG_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.composites._host_config_writes import (
    host_dns_set_composite,
    host_ntp_set_composite,
    host_syslog_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_config import (
    VMWARE_HOST_CONFIG_SHOW_OP,
    host_config_show_impl,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vmware_host_config_fake import HOST, SECRET_COMMUNITY, HostConfigVim
from tests._vmware_teardown_fake import (
    GateRecorder,
    http_status_error,
    operator,
    parked,
    preview_ctx,
)


class _Target:
    name = "vc-test"
    fingerprint = None


_TARGET = _Target()
_FAULT_BODY = json.dumps(
    {"_typeName": "InvalidArgument", "faultstring": "A specified parameter was not correct"}
)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _show(vim: HostConfigVim, host: str | None = "esxi-a") -> dict[str, Any]:
    params = {} if host is None else {"host": host}
    return await host_config_show_impl(vim, operator(), _TARGET, params)  # type: ignore[arg-type]


async def _call(handler: Any, vim: HostConfigVim, **params: Any) -> Any:
    return await handler(operator=operator(), target=_TARGET, params=params, connector=vim)


# ---------------------------------------------------------------------------
# host.config.show
# ---------------------------------------------------------------------------


async def test_show_reads_every_part() -> None:
    vim = HostConfigVim()
    out = await _show(vim)
    assert out["status"] == "ok"
    assert out["host"] == HOST
    assert out["host_name"] == "esxi-a"
    assert out["ntp"] == {
        "servers": ["old-ntp.example.invalid"],
        "time_zone": "UTC",
        "protocol": "ntp",
        "service_policy": "off",
        "service_running": True,
    }
    assert out["dns"] == {
        "dhcp": False,
        "host_name": "esxi-a",
        "domain_name": "example.invalid",
        "servers": ["old-dns.example.invalid"],
        "search_domains": ["example.invalid"],
    }
    assert out["syslog"] == {
        "log_host": "udp://old-log.example.invalid:514",
        "targets": ["udp://old-log.example.invalid:514"],
    }
    assert out["lockdown_mode"] == "lockdownDisabled"
    assert out["snmp"] == {
        "enabled": True,
        "port": 161,
        "read_only_community_count": 1,
        "trap_targets": [{"host_name": "trap.example.invalid", "port": 162}],
    }
    assert out["core_dump"] == {
        "partition_active": True,
        "disk_name": "disk-a",
        "partition": 7,
        "storage_type": "directAttached",
        "slots": 1,
    }
    assert out["read_notes"] == []
    assert vim.writes() == []


async def test_show_never_returns_an_snmp_community() -> None:
    out = await _show(HostConfigVim())
    assert SECRET_COMMUNITY not in json.dumps(out)


async def test_show_keeps_the_other_parts_when_one_cannot_be_read() -> None:
    vim = HostConfigVim()
    vim.unreadable.add("dnsConfig")
    vim.fail["QueryOptions"] = http_status_error(500, _FAULT_BODY)
    out = await _show(vim)
    assert out["status"] == "ok"
    assert out["dns"] is None
    assert out["syslog"] is None
    assert out["host_name"] is None
    assert out["ntp"]["servers"] == ["old-ntp.example.invalid"]
    assert len(out["read_notes"]) == 2
    assert "HostNetworkSystem.dnsConfig could not be read (NoPermission)" in out["read_notes"][0]
    assert out["read_notes"][1].startswith("syslog: read failed with HTTPStatusError")


async def test_show_notes_a_manager_the_host_does_not_have() -> None:
    vim = HostConfigVim()
    del vim.managers["snmpSystem"]
    out = await _show(vim)
    assert out["snmp"] is None
    assert out["read_notes"] == ["snmp: the host has no configManager.snmpSystem"]


@pytest.mark.parametrize(
    ("hosts", "host", "status"),
    [
        ([], "ghost", "host_not_found"),
        (
            [{"host": "host-1", "name": "twin"}, {"host": "host-2", "name": "twin"}],
            "twin",
            "ambiguous_host",
        ),
        ([], None, "host_required"),
    ],
)
async def test_show_refuses_an_unknown_host(
    hosts: list[dict[str, str]], host: str | None, status: str
) -> None:
    vim = HostConfigVim()
    vim.hosts = hosts
    out = await _show(vim, host)
    assert out["status"] == status
    assert out["ntp"] is None
    assert vim.calls == []


async def test_show_by_moid() -> None:
    out = await _show(HostConfigVim(), HOST)
    assert out["status"] == "ok"
    assert out["host"] == HOST


async def test_show_with_an_unreadable_config_manager() -> None:
    vim = HostConfigVim()
    vim.fail["RetrievePropertiesEx"] = httpx.ConnectError("down")
    out = await _show(vim)
    assert out["status"] == "config_manager_unreadable"
    assert out["host"] == HOST
    assert out["read_notes"][0].startswith("configManager: read failed with ConnectError")


# ---------------------------------------------------------------------------
# host.ntp.set
# ---------------------------------------------------------------------------

_NEW_NTP = ["ntp-1.example.invalid", "ntp-2.example.invalid"]


async def test_ntp_set_sends_config_policy_and_restart(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out["status"] == "applied"
    assert vim.writes() == ["UpdateDateTimeConfig", "UpdateServicePolicy", "RestartService"]
    assert vim.body_of("UpdateDateTimeConfig") == {
        "config": {
            "_typeName": "HostDateTimeConfig",
            "ntpConfig": {"_typeName": "HostNtpConfig", "server": _NEW_NTP},
        }
    }
    assert vim.body_of("UpdateServicePolicy") == {"id": "ntpd", "policy": "on"}
    assert vim.body_of("RestartService") == {"id": "ntpd"}
    assert out["before"]["servers"] == ["old-ntp.example.invalid"]
    assert out["after"]["servers"] == _NEW_NTP
    assert out["after"]["service_policy"] == "on"
    assert out["applied_steps"] == ["UpdateDateTimeConfig", "UpdateServicePolicy", "RestartService"]
    assert [c["op_id"] for c in gate.calls] == [
        "POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig",
        "POST:/HostServiceSystem/{moId}/UpdateServicePolicy",
        "POST:/HostServiceSystem/{moId}/RestartService",
    ]
    assert gate.calls[0]["params"] == {"host": HOST, "ntp_servers": _NEW_NTP}


async def test_ntp_set_starts_a_stopped_ntpd(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.services["ntpd"]["running"] = False
    vim.services["ntpd"]["policy"] = "automatic"
    out = await _call(
        host_ntp_set_composite,
        vim,
        host="esxi-a",
        servers=["old-ntp.example.invalid"],
        policy="automatic",
    )
    assert out["status"] == "applied"
    assert vim.writes() == ["StartService"]
    assert out["after"]["service_running"] is True


async def test_ntp_set_sends_nothing_when_the_host_already_matches(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.ntp_servers = list(_NEW_NTP)
    vim.services["ntpd"]["policy"] = "on"
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out["status"] == "unchanged"
    assert out["after"] == out["before"]
    assert vim.writes() == []
    assert gate.calls == []


async def test_ntp_set_stops_when_a_step_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = parked("POST:/HostDateTimeSystem/{moId}/UpdateDateTimeConfig")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(waiting))
    vim = HostConfigVim()
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out is waiting
    assert vim.writes() == []


async def test_ntp_set_reports_a_vendor_fault(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.fail["UpdateServicePolicy"] = http_status_error(500, _FAULT_BODY)
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out["status"] == "failed"
    assert "InvalidArgument" in out["fault"]
    assert "A specified parameter was not correct" in out["fault"]
    assert out["applied_steps"] == ["UpdateDateTimeConfig"]
    assert out["issues"][0]["severity"] == "error"
    assert "RestartService" not in vim.methods()


async def test_ntp_set_lets_a_non_fault_error_through(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.fail["UpdateDateTimeConfig"] = http_status_error(401)
    with pytest.raises(httpx.HTTPStatusError):
        await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)


async def test_ntp_set_not_verified_when_the_read_back_differs(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.apply = False
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out["status"] == "not_verified"
    assert out["after"]["servers"] == ["old-ntp.example.invalid"]
    assert out["issues"][0]["severity"] == "error"


async def test_ntp_set_refuses_an_unknown_host(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.hosts = []
    out = await _call(host_ntp_set_composite, vim, host="ghost", servers=_NEW_NTP)
    assert out["status"] == "host_not_found"
    assert out["requested"] == {"servers": _NEW_NTP, "policy": "on"}
    assert vim.calls == []


async def test_ntp_set_refuses_a_missing_config_manager(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    del vim.managers["serviceSystem"]
    out = await _call(host_ntp_set_composite, vim, host="esxi-a", servers=_NEW_NTP)
    assert out["status"] == "config_manager_unreadable"
    assert out["issues"][0]["severity"] == "error"
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# host.dns.set
# ---------------------------------------------------------------------------


async def test_dns_set_keeps_the_fields_left_out(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(host_dns_set_composite, vim, host="esxi-a", servers=["dns-1.example.invalid"])
    assert out["status"] == "applied"
    assert vim.writes() == ["UpdateDnsConfig"]
    assert vim.body_of("UpdateDnsConfig") == {
        "config": {
            "_typeName": "HostDnsConfig",
            "dhcp": False,
            "hostName": "esxi-a",
            "domainName": "example.invalid",
            "address": ["dns-1.example.invalid"],
            "searchDomain": ["example.invalid"],
        }
    }
    assert out["after"]["servers"] == ["dns-1.example.invalid"]
    assert out["after"]["host_name"] == "esxi-a"


async def test_dns_set_renames_the_host(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(
        host_dns_set_composite, vim, host="esxi-a", host_name="esxi-b", domain_name="lab.invalid"
    )
    assert out["status"] == "applied"
    assert out["after"]["host_name"] == "esxi-b"
    assert out["after"]["domain_name"] == "lab.invalid"


async def test_dns_set_treats_a_missing_domain_as_empty(gate: GateRecorder) -> None:
    """A host without a domain reads back ``""`` after the change; that is a match."""
    vim = HostConfigVim()
    del vim.dns["domainName"]
    out = await _call(host_dns_set_composite, vim, host="esxi-a", host_name="esxi-b")
    assert out["status"] == "applied", out
    assert vim.body_of("UpdateDnsConfig")["config"]["domainName"] == ""
    assert out["after"]["host_name"] == "esxi-b"


async def test_dns_set_refuses_dhcp_dns(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.dns["dhcp"] = True
    out = await _call(host_dns_set_composite, vim, host="esxi-a", host_name="esxi-b")
    assert out["status"] == "dhcp_in_use"
    assert vim.writes() == []
    assert gate.calls == []


async def test_dns_set_unchanged(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(host_dns_set_composite, vim, host="esxi-a", host_name="esxi-a")
    assert out["status"] == "unchanged"
    assert vim.writes() == []


async def test_dns_set_reports_a_vendor_fault(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.fail["UpdateDnsConfig"] = http_status_error(500, _FAULT_BODY)
    out = await _call(host_dns_set_composite, vim, host="esxi-a", host_name="esxi-b")
    assert out["status"] == "failed"
    assert "InvalidArgument" in out["fault"]
    assert out["applied_steps"] == []
    assert out["issues"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# host.syslog.set
# ---------------------------------------------------------------------------


async def test_syslog_set_sends_the_option_with_a_boxed_string(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    targets = ["udp://log-1.example.invalid:514", "ssl://log-2.example.invalid:1514"]
    out = await _call(host_syslog_set_composite, vim, host="esxi-a", targets=targets)
    assert out["status"] == "applied"
    assert vim.writes() == ["UpdateOptions"]
    assert vim.body_of("UpdateOptions") == {
        "changedValue": [
            {
                "_typeName": "OptionValue",
                "key": "Syslog.global.logHost",
                "value": {"_typeName": "string", "_value": ",".join(targets)},
            }
        ]
    }
    assert vim.body_of("QueryOptions") == {"name": "Syslog.global.logHost"}
    assert out["after"]["targets"] == targets


async def test_syslog_set_with_an_empty_list_removes_all_targets(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(host_syslog_set_composite, vim, host="esxi-a", targets=[])
    assert out["status"] == "applied"
    assert out["after"] == {"log_host": "", "targets": []}


async def test_syslog_set_unchanged(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    out = await _call(
        host_syslog_set_composite, vim, host="esxi-a", targets=["udp://old-log.example.invalid:514"]
    )
    assert out["status"] == "unchanged"
    assert vim.writes() == []


async def test_syslog_set_reports_a_vendor_fault(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    vim.fail["UpdateOptions"] = http_status_error(500, _FAULT_BODY)
    out = await _call(host_syslog_set_composite, vim, host="esxi-a", targets=[])
    assert out["status"] == "failed"
    assert "InvalidArgument" in out["fault"]
    assert "A specified parameter was not correct" in out["fault"]


async def test_syslog_set_not_verified_when_the_read_back_fails(gate: GateRecorder) -> None:
    vim = HostConfigVim()
    calls = 0
    original = vim._post_vmomi_json

    async def flaky(target: Any, path: str, **kwargs: Any) -> Any:
        nonlocal calls
        if path.endswith("/QueryOptions"):
            calls += 1
            if calls == 2:
                raise httpx.ConnectError("gone")
        return await original(target, path, **kwargs)

    vim._post_vmomi_json = flaky  # type: ignore[method-assign]
    out = await _call(host_syslog_set_composite, vim, host="esxi-a", targets=[])
    assert out["status"] == "not_verified"
    assert out["after"] is None
    assert out["issues"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------


async def test_ntp_preview_shows_the_current_and_the_asked_settings() -> None:
    vim = HostConfigVim()
    builder = _PREVIEW_BUILDERS["vmware.composite.host.ntp.set"]
    out = await builder(preview_ctx({"host": "esxi-a", "servers": _NEW_NTP}, vim))
    assert out is not None
    assert out["host"] == HOST
    assert out["current"]["servers"] == ["old-ntp.example.invalid"]
    assert out["current"]["service_policy"] == "off"
    assert out["current"]["service_running"] is True
    assert out["requested"] == {"servers": _NEW_NTP}
    assert vim.writes() == []


@pytest.mark.parametrize(
    ("op_id", "params", "key"),
    [
        ("vmware.composite.host.dns.set", {"host": "esxi-a", "host_name": "x"}, "host_name"),
        ("vmware.composite.host.syslog.set", {"host": "esxi-a", "targets": []}, "targets"),
    ],
)
async def test_dns_and_syslog_previews_read_the_host(
    op_id: str, params: dict[str, Any], key: str
) -> None:
    out = await _PREVIEW_BUILDERS[op_id](preview_ctx(params, HostConfigVim()))
    assert out is not None
    assert key in out["current"]


async def test_preview_says_when_the_host_cannot_be_read() -> None:
    vim = HostConfigVim()
    vim.fail["RetrievePropertiesEx"] = httpx.ConnectError("down")
    builder = _PREVIEW_BUILDERS["vmware.composite.host.syslog.set"]
    out = await builder(preview_ctx({"host": "esxi-a", "targets": []}, vim))
    assert out is not None
    assert "read_error" in out


# ---------------------------------------------------------------------------
# host.service_control: the wider list and the read-back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["snmpd", "sfcbd-watchdog", "slpd"])
async def test_service_control_allows_the_new_services(gate: GateRecorder, service: str) -> None:
    vim = HostConfigVim()
    vim.services[service] = {"key": service, "running": False, "policy": "off"}
    out = await service_control_composite(
        operator=operator(),
        target=_TARGET,
        params={"host": "esxi-a", "service": service, "action": "start", "policy": "on"},
        connector=vim,  # type: ignore[arg-type]
    )
    assert out["status"] == "applied"  # type: ignore[index]
    assert vim.writes() == ["StartService", "UpdateServicePolicy"]
    assert out["service_state"] == {  # type: ignore[index]
        "key": service,
        "label": None,
        "running": True,
        "policy": "on",
    }


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

_CHANGES = {
    "vmware.composite.host.ntp.set",
    "vmware.composite.host.dns.set",
    "vmware.composite.host.syslog.set",
}


def test_each_operation_has_its_risk_level_and_approval() -> None:
    assert VMWARE_HOST_CONFIG_SHOW_OP in VMWARE_TYPED_OPS
    assert VMWARE_HOST_CONFIG_SHOW_OP.safety_level == "safe"
    assert VMWARE_HOST_CONFIG_SHOW_OP.requires_approval is False
    rows = {spec.op_id: spec for spec in _COMPOSITES}
    for op_id in [*_CHANGES, "vmware.composite.host.service_control"]:
        assert rows[op_id].safety_level == "dangerous", op_id
        assert rows[op_id].requires_approval is True, op_id
    assert {row["op_id"] for row in HOST_CONFIG_COMPOSITES} == _CHANGES
    assert set(HOST_CONFIG_GOVERNED_SUBOPS) == _CHANGES
    assert set(_PREVIEW_BUILDERS) >= _CHANGES


def _schemas() -> list[tuple[str, dict[str, Any]]]:
    rows = [(row["op_id"], row["parameter_schema"]) for row in HOST_CONFIG_COMPOSITES]
    return [*rows, (VMWARE_HOST_CONFIG_SHOW_OP.op_id, VMWARE_HOST_CONFIG_SHOW_OP.parameter_schema)]


@pytest.mark.parametrize(("op_id", "schema"), _schemas())
def test_examples_are_valid(op_id: str, schema: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    for example in schema["examples"]:
        assert not list(validator.iter_errors(example)), (op_id, example)


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("vmware.composite.host.ntp.set", {"servers": ["ntp.example.invalid iburst"]}),
        ("vmware.composite.host.ntp.set", {"servers": ["ntp.example.invalid\n"]}),
        ("vmware.composite.host.ntp.set", {"servers": []}),
        ("vmware.composite.host.ntp.set", {"servers": ["a"], "policy": "off"}),
        ("vmware.composite.host.dns.set", {}),
        ("vmware.composite.host.dns.set", {"host_name": "esxi-a.example.invalid"}),
        ("vmware.composite.host.dns.set", {"servers": ["dns.example.invalid"]}),
        ("vmware.composite.host.syslog.set", {"targets": ["udp://a:514,udp://b:514"]}),
        ("vmware.composite.host.syslog.set", {"targets": ["http://log.example.invalid"]}),
    ],
)
def test_bad_input_is_refused_by_the_schema(op_id: str, params: dict[str, Any]) -> None:
    schema = next(
        row["parameter_schema"] for row in HOST_CONFIG_COMPOSITES if row["op_id"] == op_id
    )
    errors = list(Draft202012Validator(schema).iter_errors({"host": "esxi-a", **params}))
    assert errors, params


def test_every_change_answer_names_its_main_fields() -> None:
    for row in HOST_CONFIG_COMPOSITES:
        props = row["response_schema"]["properties"]
        assert {"status", "host", "before", "after", "fault"} <= set(props), row["op_id"]
