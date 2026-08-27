# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense write op group (#3090).

Coverage matrix:

* ``pfsense.gateway.add`` / ``pfsense.route.static.add`` happy path --
  the guard reads ``config.xml``, the append fragment is staged +
  played back + cleaned up, the config is read back, and the handler
  returns ``existed_before=False`` / ``applied=True``.
* Already-present -- an existing gateway name / route network is a
  reported no-op (``existed_before=True`` / ``applied=False`` /
  ``existing`` populated) and stages NO playback.
* Config-write failure propagation -- a non-zero playback exit, and a
  silent ``write_config`` failure that leaves the entry absent on
  read-back, both raise rather than reporting success.
* Input-validation rejections -- bad gateway name / interface / IP /
  CIDR, and a route to an undefined gateway, raise before (or, for the
  undefined-gateway guard, immediately after) the first SSH round-trip.
* Escaping / injection safety -- validation rejects shell / PHP
  metacharacters, and ``_php_squote`` escapes defensively.
* ``parse_static_routes_xml`` -- parses the ``<staticroutes>`` block.
* Response-schema conformance for both ops.
* Registration classification -- both ops are ``caution`` /
  ``requires_approval=False`` with a ``write`` tag in the ``routing``
  group.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.ops_write import (
    _php_squote,
    parse_static_routes_xml,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Environment fixture (mirrors test_connectors_pfsense_ops.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="pfsense-test",
    host="pfsense.test.invalid",
    port=22,
    secret_ref="meho/testing/pfsense/pfsense-test",
)


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's SSHCompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


# Neutral placeholder config.xml fixtures (RFC 5737 / RFC 1918 addresses;
# no real hostnames or IPs). The public repo must never carry lab values.
_CONFIG_NO_GW = (
    "<pfsense><gateways>"
    "<gateway_item><name>WAN_DHCP</name><interface>wan</interface>"
    "<gateway>192.0.2.1</gateway></gateway_item>"
    "</gateways><staticroutes></staticroutes></pfsense>"
)

_CONFIG_WITH_LAB_GW = (
    "<pfsense><gateways>"
    "<gateway_item><name>WAN_DHCP</name><interface>wan</interface>"
    "<gateway>192.0.2.1</gateway></gateway_item>"
    "<gateway_item><name>LAB_GW</name><interface>lan</interface>"
    "<gateway>10.0.0.1</gateway></gateway_item>"
    "</gateways><staticroutes></staticroutes></pfsense>"
)

_CONFIG_GW_NO_ROUTE = (
    "<pfsense><gateways>"
    "<gateway_item><name>LAB_GW</name><interface>lan</interface>"
    "<gateway>10.0.0.1</gateway></gateway_item>"
    "</gateways><staticroutes></staticroutes></pfsense>"
)

_CONFIG_GW_WITH_ROUTE = (
    "<pfsense><gateways>"
    "<gateway_item><name>LAB_GW</name><interface>lan</interface>"
    "<gateway>10.0.0.1</gateway></gateway_item>"
    "</gateways><staticroutes>"
    "<route><network>10.9.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes></pfsense>"
)

_GW_SCRIPT = "meho_gateway_add_LAB_GW"
_ROUTE_SCRIPT = "meho_route_add_10_9_0_0_24"


def _commands(mock_cmd: AsyncMock) -> list[str]:
    return [call.args[1] for call in mock_cmd.await_args_list]


# ---------------------------------------------------------------------------
# pfsense.gateway.add -- happy path
# ---------------------------------------------------------------------------


async def test_gateway_add_happy_path_stages_and_applies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_NO_GW),  # guard read
            _proc("", 0),  # stage script
            _proc("", 0),  # playback
            _proc("", 0),  # rm cleanup
            _proc(_CONFIG_WITH_LAB_GW),  # read-back verify
        ]
        result = await connector.gateway_add(
            _TARGET, {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1"}
        )

    assert result["existed_before"] is False
    assert result["applied"] is True
    assert result["existing"] is None
    assert result["ipprotocol"] == "inet"
    assert result["op_class"] == "write"
    assert result["resource"] == "gateway"

    cmds = _commands(mock_cmd)
    assert cmds[0] == "cat /cf/conf/config.xml"
    assert cmds[1].startswith(
        f"cat > /etc/phpshellsessions/{_GW_SCRIPT} <<'MEHO_PFSENSE_PLAYBACK_EOF'"
    )
    assert cmds[2] == f"pfSsh.php playback {_GW_SCRIPT}"
    assert cmds[3] == f"rm -f /etc/phpshellsessions/{_GW_SCRIPT}"
    assert cmds[4] == "cat /cf/conf/config.xml"

    body = cmds[1]
    assert body.startswith("cat >")
    assert "global $config;" in body
    assert "$gw['name'] = 'LAB_GW';" in body
    assert "$gw['interface'] = 'lan';" in body
    assert "$gw['gateway'] = '10.0.0.1';" in body
    assert "$gw['ipprotocol'] = 'inet';" in body
    assert "write_config('meho: add gateway LAB_GW');" in body
    # monitor_disable defaults off; the flag line must be absent.
    assert "monitor_disable" not in body
    # A playback fragment carries no <?php tag and no trailing exec.
    assert "<?php" not in body
    assert "\nexec" not in body


async def test_gateway_add_ipv6_sets_inet6() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_NO_GW),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_WITH_LAB_GW),
        ]
        result = await connector.gateway_add(
            _TARGET,
            {"name": "LAB_GW", "interface": "lan", "gateway": "2001:db8::1"},
        )
    assert result["ipprotocol"] == "inet6"
    assert "$gw['ipprotocol'] = 'inet6';" in _commands(mock_cmd)[1]


async def test_gateway_add_monitor_disable_emits_flag() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_NO_GW),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_WITH_LAB_GW),
        ]
        await connector.gateway_add(
            _TARGET,
            {
                "name": "LAB_GW",
                "interface": "lan",
                "gateway": "10.0.0.1",
                "monitor_disable": True,
            },
        )
    assert "$gw['monitor_disable'] = '';" in _commands(mock_cmd)[1]


# ---------------------------------------------------------------------------
# pfsense.gateway.add -- already present
# ---------------------------------------------------------------------------


async def test_gateway_add_already_present_is_noop() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_WITH_LAB_GW)]
        result = await connector.gateway_add(
            _TARGET, {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1"}
        )
    assert result["existed_before"] is True
    assert result["applied"] is False
    assert result["existing"]["name"] == "LAB_GW"
    # Only the single guard read ran -- no staging, playback, or cleanup.
    assert mock_cmd.await_count == 1
    assert _commands(mock_cmd) == ["cat /cf/conf/config.xml"]


# ---------------------------------------------------------------------------
# pfsense.gateway.add -- config-write failure propagation
# ---------------------------------------------------------------------------


async def test_gateway_add_playback_nonzero_exit_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_NO_GW),  # guard read
            _proc("", 0),  # stage
            _proc("playback error", 1),  # playback FAILS
            _proc("", 0),  # rm cleanup (finally)
        ]
        with pytest.raises(RuntimeError, match="playback"):
            await connector.gateway_add(
                _TARGET, {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1"}
            )
    # Cleanup still ran despite the failed playback.
    assert _commands(mock_cmd)[3] == f"rm -f /etc/phpshellsessions/{_GW_SCRIPT}"


async def test_gateway_add_not_persisted_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_NO_GW),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback (exit 0, but write_config silently lost it)
            _proc("", 0),  # rm cleanup
            _proc(_CONFIG_NO_GW),  # read-back: still absent
        ]
        with pytest.raises(RuntimeError, match="did not persist"):
            await connector.gateway_add(
                _TARGET, {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1"}
            )


async def test_gateway_add_config_read_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc("", 1)]  # guard read fails
        with pytest.raises(RuntimeError, match="config"):
            await connector.gateway_add(
                _TARGET, {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1"}
            )


# ---------------------------------------------------------------------------
# pfsense.gateway.add -- input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"name": "bad name", "interface": "lan", "gateway": "10.0.0.1"},
        {"name": "bad;name", "interface": "lan", "gateway": "10.0.0.1"},
        {"name": "gw'; write_config(", "interface": "lan", "gateway": "10.0.0.1"},
        {"name": "LAB_GW", "interface": "lan rm", "gateway": "10.0.0.1"},
        {"name": "LAB_GW", "interface": "lan", "gateway": "not-an-ip"},
        {"name": "LAB_GW", "interface": "lan", "gateway": "10.0.0.1; rm -rf /"},
        {"name": "LAB_GW", "interface": "lan"},  # missing gateway
    ],
)
async def test_gateway_add_rejects_bad_input_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.gateway_add(_TARGET, params)
    # A rejected input never reaches the wire.
    assert mock_cmd.await_count == 0


# ---------------------------------------------------------------------------
# pfsense.route.static.add -- happy path
# ---------------------------------------------------------------------------


async def test_route_add_happy_path_stages_and_applies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_GW_NO_ROUTE),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm cleanup
            _proc(_CONFIG_GW_WITH_ROUTE),  # read-back verify
        ]
        result = await connector.route_static_add(
            _TARGET, {"network": "10.9.0.0/24", "gateway": "LAB_GW"}
        )

    assert result["existed_before"] is False
    assert result["applied"] is True
    assert result["existing"] is None
    assert result["network"] == "10.9.0.0/24"
    assert result["resource"] == "static_route"

    cmds = _commands(mock_cmd)
    assert cmds[0] == "cat /cf/conf/config.xml"
    assert cmds[1].startswith(
        f"cat > /etc/phpshellsessions/{_ROUTE_SCRIPT} <<'MEHO_PFSENSE_PLAYBACK_EOF'"
    )
    assert cmds[2] == f"pfSsh.php playback {_ROUTE_SCRIPT}"
    assert cmds[3] == f"rm -f /etc/phpshellsessions/{_ROUTE_SCRIPT}"

    body = cmds[1]
    assert "global $config;" in body
    assert "$route['network'] = '10.9.0.0/24';" in body
    assert "$route['gateway'] = 'LAB_GW';" in body
    assert "write_config('meho: add static route 10.9.0.0/24');" in body
    assert "system_routing_configure();" in body


async def test_route_add_canonicalises_host_bits() -> None:
    # 10.9.0.5/24 canonicalises to 10.9.0.0/24, which already exists.
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_GW_WITH_ROUTE)]
        result = await connector.route_static_add(
            _TARGET, {"network": "10.9.0.5/24", "gateway": "LAB_GW"}
        )
    assert result["network"] == "10.9.0.0/24"
    assert result["existed_before"] is True
    assert result["applied"] is False
    assert mock_cmd.await_count == 1


# ---------------------------------------------------------------------------
# pfsense.route.static.add -- already present + guards
# ---------------------------------------------------------------------------


async def test_route_add_already_present_is_noop() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_GW_WITH_ROUTE)]
        result = await connector.route_static_add(
            _TARGET, {"network": "10.9.0.0/24", "gateway": "LAB_GW"}
        )
    assert result["existed_before"] is True
    assert result["applied"] is False
    assert result["existing"]["network"] == "10.9.0.0/24"
    assert mock_cmd.await_count == 1


async def test_route_add_unknown_gateway_raises_after_read() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_GW_NO_ROUTE)]
        with pytest.raises(ValueError, match="not defined"):
            await connector.route_static_add(
                _TARGET, {"network": "10.9.0.0/24", "gateway": "NOPE_GW"}
            )
    # The gateway-existence guard reads config once, then refuses -- no staging.
    assert mock_cmd.await_count == 1


async def test_route_add_playback_nonzero_exit_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_GW_NO_ROUTE),
            _proc("", 0),
            _proc("boom", 1),
            _proc("", 0),
        ]
        with pytest.raises(RuntimeError, match="playback"):
            await connector.route_static_add(
                _TARGET, {"network": "10.9.0.0/24", "gateway": "LAB_GW"}
            )


@pytest.mark.parametrize(
    "params",
    [
        {"network": "not-a-cidr", "gateway": "LAB_GW"},
        {"network": "10.9.0.0/24", "gateway": "bad gateway"},
        {"network": "10.9.0.0/24", "gateway": "gw'; drop"},
        {"gateway": "LAB_GW"},  # missing network
    ],
)
async def test_route_add_rejects_bad_input_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.route_static_add(_TARGET, params)
    assert mock_cmd.await_count == 0


# ---------------------------------------------------------------------------
# Escaping / injection safety
# ---------------------------------------------------------------------------


def test_php_squote_escapes_quote_and_backslash() -> None:
    assert _php_squote("plain") == "'plain'"
    assert _php_squote("a'b") == "'a\\'b'"
    assert _php_squote("a\\b") == "'a\\\\b'"


# ---------------------------------------------------------------------------
# parse_static_routes_xml
# ---------------------------------------------------------------------------


def test_parse_static_routes_xml_extracts_rows() -> None:
    rows = parse_static_routes_xml(_CONFIG_GW_WITH_ROUTE)
    assert rows == [{"network": "10.9.0.0/24", "gateway": "LAB_GW", "descr": None}]


def test_parse_static_routes_xml_empty_and_malformed() -> None:
    assert parse_static_routes_xml("") == []
    assert parse_static_routes_xml("<broken") == []
    assert parse_static_routes_xml("<pfsense></pfsense>") == []


# ---------------------------------------------------------------------------
# Response-schema conformance
# ---------------------------------------------------------------------------


def test_gateway_add_response_schema_accepts_both_outcomes() -> None:
    from jsonschema import Draft202012Validator

    op = next(o for o in PFSENSE_OPS if o.op_id == "pfsense.gateway.add")
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "write",
            "resource": "gateway",
            "name": "LAB_GW",
            "interface": "lan",
            "gateway": "10.0.0.1",
            "ipprotocol": "inet",
            "monitor_disable": False,
            "existed_before": False,
            "applied": True,
            "existing": None,
        }
    )
    validator.validate(
        {
            "op_class": "write",
            "resource": "gateway",
            "name": "LAB_GW",
            "interface": "lan",
            "gateway": "10.0.0.1",
            "ipprotocol": "inet",
            "monitor_disable": False,
            "existed_before": True,
            "applied": False,
            "existing": {"name": "LAB_GW", "interface": "lan"},
        }
    )


def test_route_add_response_schema_accepts_both_outcomes() -> None:
    from jsonschema import Draft202012Validator

    op = next(o for o in PFSENSE_OPS if o.op_id == "pfsense.route.static.add")
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "write",
            "resource": "static_route",
            "network": "10.9.0.0/24",
            "gateway": "LAB_GW",
            "existed_before": False,
            "applied": True,
            "existing": None,
        }
    )


# ---------------------------------------------------------------------------
# Registration classification
# ---------------------------------------------------------------------------


def test_write_ops_are_caution_no_approval_with_write_tag() -> None:
    write_ids = {"pfsense.gateway.add", "pfsense.route.static.add"}
    seen = set()
    for op in PFSENSE_OPS:
        if op.op_id not in write_ids:
            continue
        seen.add(op.op_id)
        assert op.safety_level == "caution", op.op_id
        assert op.requires_approval is False, op.op_id
        assert "write" in op.tags, op.op_id
        assert op.group_key == "routing", op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id
        assert op.llm_instructions and op.llm_instructions.get("when_to_use"), op.op_id
    assert seen == write_ids
