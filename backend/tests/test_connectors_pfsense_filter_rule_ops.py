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
from meho_backplane.connectors.pfsense.ops_filter_rule import (
    _build_rule_add_playback,
    _build_rule_delete_playback,
    _filter_rule_delete_preview,
    _validate_rule_params,
    parse_filter_rules_xml,
    parse_ssh_connection,
    rule_matches_mgmt_flow,
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


def test_parse_filter_rules_empty_and_malformed() -> None:
    assert parse_filter_rules_xml("") == []
    assert parse_filter_rules_xml("<pfsense><filter>") == []
    assert parse_filter_rules_xml("<pfsense></pfsense>") == []


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
            _proc(_AFTER_ADD),
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
        mock_cmd.side_effect = [_proc(_AFTER_ADD), _proc(""), _proc(""), _proc(""), _proc("")]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.filter_rule_delete(None, {"ridentifier": _RID})


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
