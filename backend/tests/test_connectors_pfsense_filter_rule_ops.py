# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense filter-rule ops (#3850).

``pfsense.filter.rule.add`` (``dangerous`` + approval) and
``pfsense.filter.rule.delete`` (governed destructive delete):

* **Unit** (mocked ``_run_command``): the parser, input checks, the
  management-path guard, the PHP fragments, and each handler outcome.
* **Metadata**: tier, tags, response schemas, the ``*.delete`` classifier.
* **Governed flow** (real dispatch against a recording connector): the add
  waits for approval; the delete needs a preview hash and a blast radius,
  then runs after a second person approves.

All config.xml fixtures are synthetic (RFC 5737 / RFC 1918 addresses only).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.ops_delete import (
    pfsense_alias_delete,
    pfsense_alias_member_remove,
    pfsense_gateway_delete,
    pfsense_nat_delete,
    pfsense_route_static_delete,
)
from meho_backplane.connectors.pfsense.ops_filter_rule import (
    _build_rule_add_playback,
    _build_rule_delete_playback,
    _filter_rule_delete_preview,
    _validate_rule_params,
    parse_filter_rules_xml,
    parse_ssh_connection,
    rule_matches_mgmt_flow,
)
from meho_backplane.connectors.pfsense.ops_read import parse_gateways_xml
from meho_backplane.connectors.pfsense.ops_write import (
    pfsense_gateway_add,
    pfsense_route_static_add,
    serialized_config_write,
)
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grants import _delete_shaped_reason_by_pattern
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003850")
_ADD_OP = "pfsense.filter.rule.add"
_DELETE_OP = "pfsense.filter.rule.delete"
_RID = "100000001"
_SSH_FLOW = "192.0.2.50 51000 192.0.2.1 22\n"

# ---------------------------------------------------------------------------
# Synthetic config.xml fixtures
# ---------------------------------------------------------------------------

_INTERFACES = (
    "<interfaces><wan><ipaddr>192.0.2.1</ipaddr></wan>"
    "<lan><ipaddr>10.0.0.1</ipaddr></lan><opt1><ipaddr>10.1.0.1</ipaddr></opt1></interfaces>"
)
_LAN_PASS = (
    "<rule><tracker>1600000001</tracker><type>pass</type><interface>lan</interface>"
    "<ipprotocol>inet</ipprotocol><source><network>lan</network></source>"
    "<destination><any></any></destination><descr>default allow lan</descr></rule>"
)
_FENCE = (
    "<rule><tracker>100000001</tracker><type>block</type><interface>opt1</interface>"
    "<ipprotocol>inet</ipprotocol><source><address>10.1.0.0/24</address></source>"
    "<destination><address>10.2.0.0/24</address></destination>"
    "<descr>meho: filter rule 100000001</descr></rule>"
)
_MEHO_PASS_SSH = (
    "<rule><tracker>100000002</tracker><type>pass</type><interface>wan</interface>"
    "<ipprotocol>inet</ipprotocol><protocol>tcp</protocol>"
    "<source><address>192.0.2.0/24</address></source>"
    "<destination><any></any><port>22</port></destination></rule>"
)
_NAT_LINKED = (
    "<rule><tracker>100000003</tracker><type>pass</type><interface>wan</interface>"
    "<ipprotocol>inet</ipprotocol><protocol>tcp</protocol>"
    "<source><any></any></source><destination><address>10.0.0.5</address>"
    "<port>443</port></destination><associated-rule-id>nat_1</associated-rule-id></rule>"
)


def _config(*rules: str) -> str:
    return f"<pfsense>{_INTERFACES}<filter>{''.join(rules)}</filter></pfsense>"


_BEFORE = _config(_LAN_PASS)
_AFTER_ADD = _config(_FENCE, _LAN_PASS)
_FENCE_PARAMS: dict[str, Any] = {
    "ridentifier": _RID,
    "type": "block",
    "interface": "opt1",
    "source": "10.1.0.0/24",
    "destination": "10.2.0.0/24",
}


# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _cmds(mock_cmd: AsyncMock) -> list[str]:
    return [call.args[1] for call in mock_cmd.await_args_list]


def _flow() -> dict[str, Any]:
    flow = parse_ssh_connection(_SSH_FLOW)
    assert flow is not None
    return flow


# ===========================================================================
# Part A -- parser, input checks, guard, fragments
# ===========================================================================


def test_parse_filter_rules_reads_spec_and_flags() -> None:
    rows = parse_filter_rules_xml(_config(_FENCE, _LAN_PASS, _NAT_LINKED))
    assert [r["tracker"] for r in rows] == ["100000001", "1600000001", "100000003"]
    fence = rows[0]
    assert fence["type"] == "block"
    assert fence["source"] == "10.1.0.0/24"
    assert fence["destination"] == "10.2.0.0/24"
    assert fence["floating"] is False
    assert fence["quick"] is True
    assert rows[1]["source"] == "network:lan"
    assert rows[1]["source_kind"] == "network"
    assert rows[2]["associated_rule_id"] == "nat_1"
    assert rows[2]["destination_port"] == "443"


_BAD_CONFIGS = ["", "   \n", "<pfsense><filter>", "<pfsense></pfsense>", "<other><filter/></other>"]


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
def test_parse_filter_rules_rejects_invalid_config(xml_text: str) -> None:
    with pytest.raises(ValueError, match=r"config\.xml"):
        parse_filter_rules_xml(xml_text)


def test_parse_filter_rules_empty_filter_section_is_zero_rules() -> None:
    assert parse_filter_rules_xml("<pfsense><filter></filter></pfsense>") == []
    assert parse_filter_rules_xml("<pfsense><filter/></pfsense>") == []


def test_parse_filter_rules_reads_negation_disabled_and_extras() -> None:
    rule = (
        "<rule><tracker>100000001</tracker><type>block</type><interface>opt1</interface>"
        "<ipprotocol>inet</ipprotocol><disabled></disabled>"
        "<source><not></not><address>10.1.0.0/24</address></source>"
        "<destination><any></any></destination>"
        "<gateway>LAB_GW</gateway><tag></tag><statetype>keep state</statetype>"
        "<icmptype>any</icmptype><nosync></nosync></rule>"
    )
    row = parse_filter_rules_xml(_config(rule))[0]
    assert row["disabled"] is True
    assert row["source_not"] is True
    assert row["destination_not"] is False
    assert row["extras"] == {"gateway": "LAB_GW", "nosync": "yes"}
    assert parse_filter_rules_xml(_config(_FENCE))[0]["extras"] == {}


def test_validate_normalises_addresses_and_ports() -> None:
    spec = _validate_rule_params(
        {
            "type": "pass",
            "interface": "lan",
            "protocol": "tcp",
            "source": "10.0.0.7/24",
            "destination": "10.2.0.5/32",
            "destination_port": "443-443",
        }
    )
    assert spec["source"] == "10.0.0.0/24"
    assert spec["destination"] == "10.2.0.5"
    assert spec["destination_port"] == "443"
    assert spec["direction"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"type": "match", "interface": "lan"},
        {"type": "block", "interface": "any"},
        {"type": "block", "interface": "lan", "quick": False},
        {"type": "block", "interface": "lan", "direction": "in"},
        {"type": "block", "interface": "lan", "ipprotocol": "inet46", "source": "10.0.0.1"},
        {"type": "block", "interface": "lan", "ipprotocol": "inet6", "source": "10.0.0.1"},
        {"type": "block", "interface": "lan", "destination_port": "22"},
        {"type": "block", "interface": "lan", "protocol": "tcp", "destination_port": "0"},
        {"type": "block", "interface": "lan", "protocol": "tcp", "destination_port": "9-1"},
        {"type": "block", "interface": "lan", "source": "WEB_SERVERS"},
        {"type": "block", "interface": "lan; rm -rf /"},
        {"type": "block", "interface": "lan\n"},
        {"type": "reject", "interface": "lan"},
        {"type": "block", "interface": "lan", "ipprotocol": "inet6", "source": "fe80::1%eth0"},
        {"type": "block", "interface": "lan", "ipprotocol": "inet6", "destination": "fe80::1%0"},
        {"type": "block", "interface": "lan", "source": "10.0.0.1\n"},
        {"type": "block", "interface": "lan", "destination": " 10.0.0.1"},
        {"type": "block", "interface": "lan", "protocol": "tcp", "destination_port": "443\n"},
    ],
)
def test_validate_rejects_bad_rules(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _validate_rule_params(params)


def test_parse_ssh_connection() -> None:
    flow = _flow()
    assert str(flow["client_ip"]) == "192.0.2.50"
    assert flow["server_port"] == 22
    assert parse_ssh_connection("") is None
    assert parse_ssh_connection("not an ssh flow") is None
    assert parse_ssh_connection("192.0.2.50 1 2001:db8::1 22") is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),  # block any -> any
        ({"source": "198.51.100.0/24"}, False),  # source excludes MEHO
        ({"source": "192.0.2.0/24"}, True),
        ({"destination": "10.9.0.0/16"}, False),  # destination excludes the firewall
        ({"protocol": "udp"}, False),
        ({"protocol": "tcp", "destination_port": "443"}, False),
        ({"protocol": "tcp", "destination_port": "20-25"}, True),
        ({"protocol": "tcp", "source_port": "1000"}, True),  # source port ignored
        ({"ipprotocol": "inet6"}, False),
        ({"ipprotocol": "inet46"}, True),
        ({"interface": "opt1"}, True),  # interface ignored on purpose
    ],
)
def test_rule_matches_mgmt_flow(overrides: dict[str, Any], expected: bool) -> None:
    spec = _validate_rule_params({"type": "block", "interface": "wan", **overrides})
    assert rule_matches_mgmt_flow(spec, _flow()) is expected


def test_mgmt_flow_treats_keyword_and_negated_address_as_match() -> None:
    rows = parse_filter_rules_xml(_config(_LAN_PASS))
    assert rule_matches_mgmt_flow(rows[0], _flow()) is True  # "lan" keyword -> match
    negated = parse_filter_rules_xml(
        _config(
            "<rule><tracker>100000009</tracker><type>pass</type><interface>lan</interface>"
            "<source><address>198.51.100.0/24</address><not></not></source>"
            "<destination><any></any></destination></rule>"
        )
    )
    assert rule_matches_mgmt_flow(negated[0], _flow()) is True


def _assert_playback_safe(body: str) -> None:
    # pfSsh.php turns a line starting with "!" or "=" into a shell call.
    for line in body.splitlines():
        assert not line.startswith(("!", "=")), line
    assert "<?php" not in body
    assert body.startswith("global $config;")


def test_add_playback_top_inserts_once_with_guard() -> None:
    spec = _validate_rule_params(_FENCE_PARAMS)
    body = _build_rule_add_playback(_RID, spec, "fence", "top")
    _assert_playback_safe(body)
    assert "if ($meho_found === 0) {" in body
    assert "$meho_rule['tracker'] = $meho_tracker;" in body
    assert "$meho_src['address'] = '10.1.0.0/24';" in body
    assert "$meho_dst['address'] = '10.2.0.0/24';" in body
    assert "get_interface_ruleindex($meho_section);" in body
    assert "$meho_section = 'opt1';" in body
    assert "shift_separators($meho_seps, -1);" in body
    assert "filter_rules_sort();" in body
    assert "filter_configure();" in body
    assert "floating" not in body


def test_add_playback_floating_bottom() -> None:
    spec = _validate_rule_params(
        {
            "type": "match",
            "interface": "any",
            "floating": True,
            "direction": "out",
            "quick": False,
            "log": True,
        }
    )
    body = _build_rule_add_playback(_RID, spec, "fence", "bottom")
    _assert_playback_safe(body)
    assert "$meho_section = 'FloatingRules';" in body
    assert "$meho_rule['floating'] = 'yes';" in body
    assert "$meho_rule['direction'] = 'out';" in body
    assert "$meho_rule['quick']" not in body
    assert "$meho_rule['log'] = true;" in body
    assert "$meho_idx['last'] + 1" in body
    assert "shift_separators" not in body


def test_delete_playback_removes_only_one() -> None:
    body = _build_rule_delete_playback(_RID)
    _assert_playback_safe(body)
    assert "$meho_tracker = '100000001';" in body
    assert "if ($meho_found === 1) {" in body
    assert "shift_separators($meho_seps, $meho_idx['index'], true);" in body
    assert "array_values($meho_rules)" in body


# ===========================================================================
# Part A -- add handler
# ===========================================================================


@pytest.mark.parametrize(
    "rid",
    [None, "", "1", "1600000001", "010000000", "12345678a", 100000001, "100000001\n"],
)
async def test_add_rejects_bad_ridentifier_before_ssh(rid: Any) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.filter_rule_add(None, {**_FENCE_PARAMS, "ridentifier": rid})
    mock_cmd.assert_not_awaited()


@pytest.mark.parametrize("descr", ["x\n", "fence\nmore", ""])
async def test_add_rejects_bad_descr_before_ssh(descr: str) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.filter_rule_add(None, {**_FENCE_PARAMS, "descr": descr})
    mock_cmd.assert_not_awaited()


@pytest.mark.parametrize("rid", ["100000001\n", "1", None])
async def test_delete_rejects_bad_ridentifier_before_ssh(rid: Any) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.filter_rule_delete(None, {"ridentifier": rid})
    mock_cmd.assert_not_awaited()


async def test_add_block_rule_happy_path() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_BEFORE),  # guard read
            _proc(_SSH_FLOW),  # management flow
            _proc(""),  # stage
            _proc(""),  # playback
            _proc(""),  # rm
            _proc(_AFTER_ADD),  # read-back
        ]
        result = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert result["status"] == "added"
    assert result["applied"] is True
    assert result["verified"] is True
    assert (result["rules_before"], result["rules_after"]) == (1, 2)
    assert result["position"] == "top"
    cmds = _cmds(mock_cmd)
    assert cmds[1] == 'echo "$SSH_CONNECTION"'
    assert cmds[3] == f"pfSsh.php playback meho_filter_rule_add_{_RID}"
    assert cmds[4] == f"rm -f /etc/phpshellsessions/meho_filter_rule_add_{_RID}"


async def test_add_pass_rule_skips_mgmt_check() -> None:
    connector = PfSenseConnector()
    after = _config(_LAN_PASS, _MEHO_PASS_SSH.replace("100000002", _RID))
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_BEFORE), _proc(""), _proc(""), _proc(""), _proc(after)]
        result = await connector.filter_rule_add(
            None,
            {
                "ridentifier": _RID,
                "type": "pass",
                "interface": "wan",
                "protocol": "tcp",
                "source": "192.0.2.0/24",
                "destination_port": "22",
                "position": "bottom",
            },
        )
    assert result["status"] == "added"
    assert 'echo "$SSH_CONNECTION"' not in _cmds(mock_cmd)


async def test_add_same_rule_is_noop() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_AFTER_ADD)]
        result = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert result["status"] == "already_present"
    assert result["applied"] is False
    assert result["existing"]["tracker"] == _RID
    assert mock_cmd.await_count == 1


async def test_add_different_rule_same_id_is_conflict() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_AFTER_ADD)]
        result = await connector.filter_rule_add(
            None, {**_FENCE_PARAMS, "destination": "10.3.0.0/24"}
        )
    assert result["status"] == "conflict"
    assert result["applied"] is False
    assert mock_cmd.await_count == 1


_FENCE_DISABLED = _FENCE.replace("<type>block</type>", "<type>block</type><disabled></disabled>")
_FENCE_NOT_SOURCE = _FENCE.replace("<source>", "<source><not></not>")
_FENCE_NOT_DEST = _FENCE.replace("<destination>", "<destination><not></not>")
_FENCE_WITH_GATEWAY = _FENCE.replace("</rule>", "<gateway>LAB_GW</gateway></rule>")
_FENCE_WITH_SCHED = _FENCE.replace("</rule>", "<sched>office</sched></rule>")


@pytest.mark.parametrize(
    ("stored", "field"),
    [
        (_FENCE_DISABLED, "disabled"),
        (_FENCE_NOT_SOURCE, "source_not"),
        (_FENCE_NOT_DEST, "destination_not"),
        (_FENCE_WITH_GATEWAY, "gateway"),
        (_FENCE_WITH_SCHED, "sched"),
    ],
)
async def test_add_stored_rule_with_other_behaviour_is_conflict(stored: str, field: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_config(stored, _LAN_PASS))]
        result = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert result["status"] == "conflict"
    assert result["applied"] is False
    assert field in result["guidance"]
    assert mock_cmd.await_count == 1


async def test_add_duplicate_id_is_ambiguous() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_config(_FENCE, _FENCE))]
        result = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert result["status"] == "ambiguous"
    assert mock_cmd.await_count == 1


async def test_add_unknown_interface_is_refused() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_BEFORE)]
        result = await connector.filter_rule_add(None, {**_FENCE_PARAMS, "interface": "opt9"})
    assert result["status"] == "unknown_interface"
    assert mock_cmd.await_count == 1


async def test_add_interface_group_is_accepted() -> None:
    config = _BEFORE.replace(
        "</interfaces>",
        "</interfaces><ifgroups><ifgroupentry><ifname>SEGS</ifname>"
        "<members>opt1</members></ifgroupentry></ifgroups>",
    )
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(config),
            _proc(_SSH_FLOW),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_config(_FENCE.replace("opt1", "SEGS"), _LAN_PASS)),
        ]
        result = await connector.filter_rule_add(None, {**_FENCE_PARAMS, "interface": "SEGS"})
    assert result["status"] == "added"


async def test_add_block_that_cuts_mgmt_path_is_refused() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_BEFORE), _proc(_SSH_FLOW)]
        result = await connector.filter_rule_add(
            None, {"ridentifier": _RID, "type": "block", "interface": "wan"}
        )
    assert result["status"] == "mgmt_path"
    assert result["applied"] is False
    assert "port 22" in result["guidance"]
    assert not any(c.startswith("pfSsh.php") for c in _cmds(mock_cmd))


@pytest.mark.parametrize("flow_proc", [_proc("", 0), _proc("garbage", 0), _proc("", 1)])
async def test_add_block_with_unreadable_mgmt_flow_is_refused(flow_proc: Any) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_BEFORE), flow_proc]
        result = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert result["status"] == "mgmt_path_unknown"
    assert mock_cmd.await_count == 2


async def test_add_verification_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_BEFORE),
            _proc(_SSH_FLOW),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_BEFORE),  # read-back: nothing landed
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.filter_rule_add(None, dict(_FENCE_PARAMS))


@pytest.mark.parametrize(
    ("landed", "field"),
    [
        (_FENCE.replace("<interface>opt1</interface>", "<interface>lan</interface>"), "interface"),
        (_FENCE.replace("<type>block</type>", "<type>pass</type>"), "type"),
        (_FENCE_NOT_SOURCE, "source_not"),
        (_FENCE_DISABLED, "disabled"),
    ],
)
async def test_add_verification_compares_the_stored_rule(landed: str, field: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_BEFORE),
            _proc(_SSH_FLOW),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_config(landed, _LAN_PASS)),  # read-back: right id, wrong rule
        ]
        with pytest.raises(RuntimeError, match=f"verification failed.*{field}"):
            await connector.filter_rule_add(None, dict(_FENCE_PARAMS))


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
async def test_add_invalid_read_back_raises(xml_text: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_BEFORE),
            _proc(_SSH_FLOW),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(xml_text),
        ]
        with pytest.raises(RuntimeError, match=r"verification failed.*config\.xml"):
            await connector.filter_rule_add(None, dict(_FENCE_PARAMS))


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
async def test_add_invalid_guard_read_stages_nothing(xml_text: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(xml_text)]
        with pytest.raises(RuntimeError, match=r"could not be read.*config\.xml"):
            await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
    assert mock_cmd.await_count == 1


# ===========================================================================
# Part A -- concurrent writes on one appliance
# ===========================================================================


def _pass_rule(rid: str, destination: str) -> str:
    return (
        f"<rule><tracker>{rid}</tracker><type>pass</type><interface>lan</interface>"
        f"<ipprotocol>inet</ipprotocol><source><any></any></source>"
        f"<destination><address>{destination}</address></destination>"
        f"<descr>meho: filter rule {rid}</descr></rule>"
    )


class _FakePfSense:
    """In-memory config.xml whose playback saves the copy it loaded at start.

    This is the real pfSense behaviour: ``write_config()`` saves the whole
    ``$config`` the fragment loaded, so two overlapping edits lose one.
    """

    def __init__(self, config: str, inserts: dict[str, str]) -> None:
        self.config = config
        self.inserts = inserts  # playback script name -> item XML the fragment adds
        self.in_flight = 0
        self.max_in_flight = 0

    async def run(self, target: Any, command: str, operator: Any = None) -> Any:
        del target, operator
        await asyncio.sleep(0)
        if command == "cat /cf/conf/config.xml":
            return _proc(self.config)
        if command == 'echo "$SSH_CONNECTION"':
            return _proc(_SSH_FLOW)
        if command.startswith("pfSsh.php playback "):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            loaded = self.config  # the fragment's $config, loaded when it starts
            for _ in range(3):
                await asyncio.sleep(0)  # the other op's reads and writes land here
            item = self.inserts[command.removeprefix("pfSsh.php playback ")]
            if item not in loaded:
                section = _SECTION_BY_ITEM_TAG[item[1 : item.index(">")]]
                self.config = loaded.replace(f"<{section}>", f"<{section}>{item}", 1)
            self.in_flight -= 1
        return _proc("")


# The config.xml section each kind of item lands in.
_SECTION_BY_ITEM_TAG = {"rule": "filter", "gateway_item": "gateways"}


_RID_A, _RID_B = "100000011", "100000012"
_INSERTS = {
    f"meho_filter_rule_add_{_RID_A}": _pass_rule(_RID_A, "10.5.0.0/24"),
    f"meho_filter_rule_add_{_RID_B}": _pass_rule(_RID_B, "10.6.0.0/24"),
}


def _add_params(rid: str, destination: str) -> dict[str, Any]:
    return {
        "ridentifier": rid,
        "type": "pass",
        "interface": "lan",
        "destination": destination,
    }


async def test_concurrent_adds_on_one_appliance_run_one_at_a_time() -> None:
    fake = _FakePfSense(_BEFORE, _INSERTS)
    connector = PfSenseConnector()
    target = SimpleNamespace(name="pf-a", host="pf.example.invalid")
    with patch.object(connector, "_run_command", side_effect=fake.run):
        first, second = await asyncio.gather(
            connector.filter_rule_add(target, _add_params(_RID_A, "10.5.0.0/24")),
            connector.filter_rule_add(target, _add_params(_RID_B, "10.6.0.0/24")),
        )
    assert (first["status"], second["status"]) == ("added", "added")
    assert (first["rules_after"], second["rules_after"]) == (2, 3)
    assert fake.max_in_flight == 1
    assert len(parse_filter_rules_xml(fake.config)) == 3


async def test_concurrent_adds_without_the_lock_lose_a_change() -> None:
    """Control: the fake really loses an update, so the test above proves the lock."""
    from meho_backplane.connectors.pfsense.ops_filter_rule import pfsense_filter_rule_add

    unlocked = pfsense_filter_rule_add.__wrapped__  # type: ignore[attr-defined]
    fake = _FakePfSense(_BEFORE, _INSERTS)
    connector = PfSenseConnector()
    target = SimpleNamespace(name="pf-a", host="pf.example.invalid")
    with patch.object(connector, "_run_command", side_effect=fake.run):
        results = await asyncio.gather(
            unlocked(connector, target, _add_params(_RID_A, "10.5.0.0/24")),
            unlocked(connector, target, _add_params(_RID_B, "10.6.0.0/24")),
            return_exceptions=True,
        )
    assert fake.max_in_flight == 2
    assert any(isinstance(r, RuntimeError) for r in results)
    assert len(parse_filter_rules_xml(fake.config)) == 2


async def test_add_then_delete_of_the_same_rule_serialize() -> None:
    """A delete started while the add runs sees the added rule, not an older config."""
    fake = _FakePfSense(_BEFORE, _INSERTS)
    rid = _RID_A

    async def run(target: Any, command: str, operator: Any = None) -> Any:
        if command == f"pfSsh.php playback meho_filter_rule_delete_{rid}":
            await asyncio.sleep(0)
            fake.config = fake.config.replace(_INSERTS[f"meho_filter_rule_add_{rid}"], "", 1)
            return _proc("")
        return await fake.run(target, command, operator)

    connector = PfSenseConnector()
    target = SimpleNamespace(name="pf-a", host="pf.example.invalid")
    with patch.object(connector, "_run_command", side_effect=run):
        added, deleted = await asyncio.gather(
            connector.filter_rule_add(target, _add_params(rid, "10.5.0.0/24")),
            connector.filter_rule_delete(target, {"ridentifier": rid}),
        )
    assert added["status"] == "added"
    assert deleted["status"] == "deleted"
    assert fake.config == _BEFORE


def test_write_lock_is_per_appliance_host() -> None:
    connector = PfSenseConnector()
    a1 = SimpleNamespace(name="a1", host="PF.example.invalid")
    a2 = SimpleNamespace(name="a2", host="pf.example.invalid")
    b = SimpleNamespace(name="b", host="other.example.invalid")
    assert connector.config_write_lock(a1) is connector.config_write_lock(a2)
    assert connector.config_write_lock(a1) is not connector.config_write_lock(b)


_GATEWAY_NAME = "LAB_GW"
_GATEWAY_ITEM = (
    f"<gateway_item><interface>lan</interface><gateway>10.0.0.254</gateway>"
    f"<name>{_GATEWAY_NAME}</name><weight>1</weight><ipprotocol>inet</ipprotocol>"
    f"<descr>meho: gateway {_GATEWAY_NAME}</descr></gateway_item>"
)
_GATEWAY_PARAMS = {"name": _GATEWAY_NAME, "interface": "lan", "gateway": "10.0.0.254"}
_BEFORE_WITH_GATEWAYS = _BEFORE.replace("<filter>", "<gateways></gateways><filter>", 1)
_MIXED_INSERTS = {**_INSERTS, f"meho_gateway_add_{_GATEWAY_NAME}": _GATEWAY_ITEM}


async def test_filter_rule_add_and_gateway_add_on_one_appliance_serialize() -> None:
    """An older write op started during a rule add must not undo the rule, or vice versa."""
    fake = _FakePfSense(_BEFORE_WITH_GATEWAYS, _MIXED_INSERTS)
    connector = PfSenseConnector()
    target = SimpleNamespace(name="pf-a", host="pf.example.invalid")
    with patch.object(connector, "_run_command", side_effect=fake.run):
        rule, gateway = await asyncio.gather(
            connector.filter_rule_add(target, _add_params(_RID_A, "10.5.0.0/24")),
            connector.gateway_add(target, dict(_GATEWAY_PARAMS)),
        )
    assert rule["status"] == "added"
    assert gateway["applied"] is True
    assert fake.max_in_flight == 1
    assert len(parse_filter_rules_xml(fake.config)) == 2
    assert [g["name"] for g in parse_gateways_xml(fake.config)] == [_GATEWAY_NAME]


async def test_filter_rule_add_and_gateway_add_without_the_lock_lose_a_change() -> None:
    """Control: the two unlocked handlers really overwrite each other on the fake."""
    from meho_backplane.connectors.pfsense.ops_filter_rule import pfsense_filter_rule_add

    unlocked_rule_add = pfsense_filter_rule_add.__wrapped__  # type: ignore[attr-defined]
    unlocked_gateway_add = pfsense_gateway_add.__wrapped__  # type: ignore[attr-defined]
    fake = _FakePfSense(_BEFORE_WITH_GATEWAYS, _MIXED_INSERTS)
    connector = PfSenseConnector()
    target = SimpleNamespace(name="pf-a", host="pf.example.invalid")
    with patch.object(connector, "_run_command", side_effect=fake.run):
        results = await asyncio.gather(
            unlocked_rule_add(connector, target, _add_params(_RID_A, "10.5.0.0/24")),
            unlocked_gateway_add(connector, target, dict(_GATEWAY_PARAMS)),
            return_exceptions=True,
        )
    assert fake.max_in_flight == 2
    assert any(isinstance(r, RuntimeError) for r in results)
    rules, gateways = parse_filter_rules_xml(fake.config), parse_gateways_xml(fake.config)
    assert (len(rules), len(gateways)) in {(1, 1), (2, 0)}


@pytest.mark.parametrize(
    "handler",
    [
        pfsense_gateway_add,
        pfsense_route_static_add,
        pfsense_nat_delete,
        pfsense_alias_delete,
        pfsense_route_static_delete,
        pfsense_gateway_delete,
        pfsense_alias_member_remove,
    ],
    ids=lambda h: h.__name__,
)
def test_every_other_config_writer_holds_the_same_lock(handler: Any) -> None:
    """Every handler that plays back a ``write_config()`` fragment is wrapped by the lock."""
    inner = handler.__wrapped__
    assert inner.__name__ == handler.__name__
    assert handler.__code__ is serialized_config_write(inner).__code__


# ===========================================================================
# Part A -- delete handler
# ===========================================================================


async def test_delete_block_rule_happy_path() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_AFTER_ADD), _proc(""), _proc(""), _proc(""), _proc(_BEFORE)]
        result = await connector.filter_rule_delete(None, {"ridentifier": _RID})
    assert result["status"] == "deleted"
    assert result["verified"] is True
    assert (result["rules_before"], result["rules_after"]) == (2, 1)
    assert result["removed"]["tracker"] == _RID
    cmds = _cmds(mock_cmd)
    assert 'echo "$SSH_CONNECTION"' not in cmds  # block rule: no mgmt check
    assert cmds[2] == f"pfSsh.php playback meho_filter_rule_delete_{_RID}"


async def test_delete_not_found_and_ambiguous_stage_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_BEFORE), _proc(_config(_FENCE, _FENCE))]
        missing = await connector.filter_rule_delete(None, {"ridentifier": _RID})
        dup = await connector.filter_rule_delete(None, {"ridentifier": _RID})
    assert missing["status"] == "not_found"
    assert dup["status"] == "ambiguous"
    assert mock_cmd.await_count == 2


async def test_delete_nat_linked_rule_is_refused() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_config(_NAT_LINKED))]
        result = await connector.filter_rule_delete(None, {"ridentifier": "100000003"})
    assert result["status"] == "nat_associated"
    assert mock_cmd.await_count == 1


async def test_delete_pass_rule_carrying_mgmt_path_is_refused() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_config(_MEHO_PASS_SSH)), _proc(_SSH_FLOW)]
        result = await connector.filter_rule_delete(None, {"ridentifier": "100000002"})
    assert result["status"] == "mgmt_path"
    assert not any(c.startswith("pfSsh.php") for c in _cmds(mock_cmd))


async def test_delete_verification_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_AFTER_ADD),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_AFTER_ADD),
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.filter_rule_delete(None, {"ridentifier": _RID})


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
async def test_delete_of_last_rule_with_invalid_read_back_raises(xml_text: str) -> None:
    """A failed read-back must not pass as "zero rules left" after the last delete."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_config(_FENCE)),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(xml_text),
        ]
        with pytest.raises(RuntimeError, match=r"verification failed.*config\.xml"):
            await connector.filter_rule_delete(None, {"ridentifier": _RID})


async def test_delete_of_last_rule_verifies_against_an_empty_filter_section() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_config(_FENCE)),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_config()),
        ]
        result = await connector.filter_rule_delete(None, {"ridentifier": _RID})
    assert result["status"] == "deleted"
    assert (result["rules_before"], result["rules_after"]) == (1, 0)


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
async def test_delete_invalid_guard_read_stages_nothing(xml_text: str) -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(xml_text)]
        with pytest.raises(RuntimeError, match=r"could not be read.*config\.xml"):
            await connector.filter_rule_delete(None, {"ridentifier": _RID})
    assert mock_cmd.await_count == 1


# ===========================================================================
# Part B -- metadata
# ===========================================================================


def _op(op_id: str) -> Any:
    return next(o for o in PFSENSE_OPS if o.op_id == op_id)


def test_ops_classification() -> None:
    add, delete = _op(_ADD_OP), _op(_DELETE_OP)
    assert (add.safety_level, add.requires_approval) == ("dangerous", True)
    assert (delete.safety_level, delete.requires_approval) == ("destructive", True)
    assert "destructive" in delete.tags and "delete" in delete.tags
    assert add.group_key == delete.group_key == "firewall"
    for op in (add, delete):
        assert op.parameter_schema["additionalProperties"] is False
        assert hasattr(PfSenseConnector, op.handler_attr)


def test_delete_folds_into_default_delete_shaped_pattern() -> None:
    patterns = get_settings().service_grant_delete_shaped_patterns
    assert _delete_shaped_reason_by_pattern(_DELETE_OP, patterns) is not None


async def test_response_schemas_accept_handler_outputs() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_BEFORE),
            _proc(_SSH_FLOW),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_AFTER_ADD),
            _proc(_AFTER_ADD),
            _proc(""),
            _proc(""),
            _proc(""),
            _proc(_BEFORE),
        ]
        added = await connector.filter_rule_add(None, dict(_FENCE_PARAMS))
        deleted = await connector.filter_rule_delete(None, {"ridentifier": _RID})
    Draft202012Validator(_op(_ADD_OP).response_schema).validate(added)
    Draft202012Validator(_op(_DELETE_OP).response_schema).validate(deleted)


async def test_delete_preview_names_the_rule() -> None:
    assert _PREVIEW_BUILDERS[_DELETE_OP] is _filter_rule_delete_preview
    connector = PfSenseConnector()
    ctx: Any = SimpleNamespace(
        params={"ridentifier": _RID}, connector_instance=connector, target=None, operator=None
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_AFTER_ADD), _proc(_BEFORE)]
        preview = await _filter_rule_delete_preview(ctx)
        declined = await _filter_rule_delete_preview(ctx)
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"]["kind"] == "filter_rule"
    assert blast["object"]["ridentifier"] == _RID
    assert blast["object"]["type"] == "block"
    assert blast["irreversibility"] == "permanent"
    assert declined is None


async def test_delete_preview_shows_negated_endpoints() -> None:
    connector = PfSenseConnector()
    ctx: Any = SimpleNamespace(
        params={"ridentifier": _RID}, connector_instance=connector, target=None, operator=None
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_config(_FENCE_NOT_SOURCE, _LAN_PASS)), _proc(_AFTER_ADD)]
        negated = await _filter_rule_delete_preview(ctx)
        plain = await _filter_rule_delete_preview(ctx)
    assert negated is not None and plain is not None
    assert negated["blast_radius"]["object"]["source"] == "10.1.0.0/24"
    assert negated["blast_radius"]["object"]["source_not"] is True
    assert negated["blast_radius"]["object"]["destination_not"] is False
    assert plain["blast_radius"]["object"]["source_not"] is False
    assert plain["blast_radius"]["object"]["destination_not"] is False


@pytest.mark.parametrize("xml_text", _BAD_CONFIGS)
async def test_delete_preview_declines_invalid_config(xml_text: str) -> None:
    connector = PfSenseConnector()
    ctx: Any = SimpleNamespace(
        params={"ridentifier": _RID}, connector_instance=connector, target=None, operator=None
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(xml_text)]
        assert await _filter_rule_delete_preview(ctx) is None


async def test_delete_preview_declines_id_with_trailing_newline() -> None:
    connector = PfSenseConnector()
    ctx: Any = SimpleNamespace(
        params={"ridentifier": f"{_RID}\n"},
        connector_instance=connector,
        target=None,
        operator=None,
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        assert await _filter_rule_delete_preview(ctx) is None
    mock_cmd.assert_not_awaited()


# ===========================================================================
# Part C -- governed flow (real dispatch)
# ===========================================================================


def _make_operator(
    *, sub: str = "op-pf", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="pfSense Filter Rule Conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _RecordingPfSense(PfSenseConnector):
    """Replays ``config_before`` until a playback runs, then ``config_after``."""

    def __init__(self, *, config_before: str, config_after: str | None = None) -> None:
        super().__init__()
        self._config_before = config_before
        self._config_after = config_after if config_after is not None else config_before
        self._played = False
        self.commands: list[str] = []

    async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {"username": "admin", "client_keys": [], "known_hosts": None}

    async def _run_command(self, target: Any, command: str, operator: Any = None) -> Any:
        self.commands.append(command)
        if command == "cat /cf/conf/config.xml":
            return _proc(self._config_after if self._played else self._config_before)
        if command == 'echo "$SSH_CONNECTION"':
            return _proc(_SSH_FLOW)
        if command.startswith("pfSsh.php playback"):
            self._played = True
        return _proc("", 0)

    @property
    def playback_ran(self) -> bool:
        return any(c.startswith("pfSsh.php playback") for c in self.commands)


async def _seed_target(name: str = "pf-perimeter") -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product="pfsense",
                host="pf.test",
                port=22,
                fqdn=None,
                secret_ref="kv/dev/pfsense/perimeter",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2.7"},
                preferred_impl_id="pfsense-ssh",
                notes="seeded by test_connectors_pfsense_filter_rule_ops",
            )
        )
        await s.commit()


async def _bootstrap(recorder: _RecordingPfSense) -> None:
    if ("pfsense", "2.7", "pfsense-ssh") not in all_connectors_v2():
        register_connector_v2(
            product="pfsense", version="2.7", impl_id="pfsense-ssh", cls=PfSenseConnector
        )
    await PfSenseConnector.register_operations()
    _CONNECTOR_INSTANCE_CACHE[PfSenseConnector] = recorder  # type: ignore[assignment]


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


@pytest.mark.asyncio
async def test_add_waits_for_approval_then_runs() -> None:
    recorder = _RecordingPfSense(config_before=_BEFORE, config_after=_AFTER_ADD)
    await _bootstrap(recorder)
    await _seed_target()
    requester = _make_operator(sub="op-requester")
    call = await call_operation(
        requester,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _ADD_OP,
            "target": "pf-perimeter",
            "params": dict(_FENCE_PARAMS),
        },
    )
    assert call["status"] == "awaiting_approval", call
    assert not recorder.playback_ran
    request_id = UUID(call["extras"]["approval_request_id"])

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "added"
    assert recorder.playback_ran


@pytest.mark.asyncio
async def test_delete_full_governed_flow() -> None:
    recorder = _RecordingPfSense(config_before=_AFTER_ADD, config_after=_BEFORE)
    await _bootstrap(recorder)
    await _seed_target()
    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _DELETE_OP,
        "target": "pf-perimeter",
        "params": {"ridentifier": _RID},
    }

    no_hash = await call_operation(requester, args)
    assert no_hash["status"] == "denied"
    assert no_hash["extras"]["error_code"] == "preview_binding_required"

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert not recorder.playback_ran
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.proposed_effect["blast_radius"]["object"]["ridentifier"] == _RID

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert recorder.playback_ran


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [(_ADD_OP, _FENCE_PARAMS), (_DELETE_OP, {"ridentifier": _RID})],
)
async def test_agent_principal_cannot_run_either_op(op_id: str, params: dict[str, Any]) -> None:
    recorder = _RecordingPfSense(config_before=_AFTER_ADD)
    await _bootstrap(recorder)
    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=SimpleNamespace(
            product="pfsense",
            fingerprint=SimpleNamespace(version="2.7"),
            preferred_impl_id="pfsense-ssh",
            id=uuid.uuid4(),
            tenant_id=_TENANT_ID,
            name="pf-perimeter",
            host="pf.test",
            port=22,
            auth_model="shared_service_account",
        ),
        params=dict(params),
    )
    assert result.status == "denied", result
    assert not recorder.playback_ran
    assert await _pending_count() == 0
