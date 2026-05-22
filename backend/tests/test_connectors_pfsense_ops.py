# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense read op group (G3.7-T2 #847).

Coverage matrix (per Task #847 acceptance criteria):

* ``parse_pfctl_rules`` -- parses ``pfctl -sr`` output into rule rows;
  action/direction extracted; blank lines and comments skipped;
  empty input returns empty list; malformed lines included with
  ``action=None``.
* ``parse_pfctl_states`` -- parses ``pfctl -ss`` output; proto/iface/
  src/direction/dst extracted; malformed lines included with None fields;
  empty input returns empty list.
* ``parse_pfctl_nat`` -- parses ``pfctl -sn`` output; action extracted
  (``nat``/``rdr``/``binat``/``no_nat``/``no_rdr``); empty returns [].
* ``parse_ifconfig`` -- parses ``ifconfig -a`` output; name/mtu/inet/
  inet6/ether/status/media extracted; hex netmask converted to CIDR;
  multiple interfaces.
* ``_netmask_to_cidr`` -- hex and dotted-decimal forms; invalid returns
  None.
* ``parse_gateways_xml`` -- parses ``<gateways>`` block from full
  config.xml or snippet; ``defaultgw`` bool; empty/malformed XML returns [].
* Bound-method shims on :class:`PfSenseConnector` -- ``version``,
  ``firewall_rules``, ``firewall_state``, ``nat_rules``,
  ``interface_list``, ``gateway_list``, ``config_show`` -- each runs
  the correct SSH command, passes stdout through the parser, and returns
  the expected envelope shape.
* Malformed command output → structured result (no crash); error field
  set on non-zero exit with empty stdout.
* ``PFSENSE_OPS`` registration shape -- all 8 ops carry
  ``safety_level='safe'``, ``additionalProperties=False`` on the
  parameter schema, non-empty ``llm_instructions``, and pfsense-
  namespace op_ids; ``firewall``, ``nat``, ``network``, ``config``
  groups are present.
* Idempotency contract -- the ``PFSENSE_OPS`` tuple length matches 8
  after T2 lands; the ``pfsense.about`` canary remains at index 0.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.ops_read import (
    _netmask_to_cidr,
    parse_gateways_xml,
    parse_ifconfig,
    parse_pfctl_nat,
    parse_pfctl_rules,
    parse_pfctl_states,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
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
    secret_ref: dict[str, Any]


_TARGET = _StubTarget(
    name="pfsense-test",
    host="pfsense.test.invalid",
    port=22,
    secret_ref={"username": "admin", "ssh_private_key": "dummy-key"},
)


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's SSHCompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


# ---------------------------------------------------------------------------
# parse_pfctl_rules
# ---------------------------------------------------------------------------


def test_parse_pfctl_rules_extracts_pass_in() -> None:
    output = "pass in quick on em0 all\n"
    rows = parse_pfctl_rules(output)
    assert len(rows) == 1
    assert rows[0]["action"] == "pass"
    assert rows[0]["direction"] == "in"
    assert rows[0]["rule"] == "pass in quick on em0 all"


def test_parse_pfctl_rules_extracts_block_out() -> None:
    output = "block drop out log quick on em0 all\n"
    rows = parse_pfctl_rules(output)
    assert rows[0]["action"] == "block"
    assert rows[0]["direction"] == "out"


def test_parse_pfctl_rules_handles_match_no_direction() -> None:
    output = "match inet from any to any scrub (no-df)\n"
    rows = parse_pfctl_rules(output)
    assert rows[0]["action"] == "match"
    assert rows[0]["direction"] is None


def test_parse_pfctl_rules_skips_blank_and_comment_lines() -> None:
    output = "\n# comment\npass out all\n\n"
    rows = parse_pfctl_rules(output)
    assert len(rows) == 1
    assert rows[0]["action"] == "pass"


def test_parse_pfctl_rules_empty_input_returns_empty_list() -> None:
    assert parse_pfctl_rules("") == []
    assert parse_pfctl_rules("\n\n") == []


def test_parse_pfctl_rules_unrecognised_line_included_with_none_action() -> None:
    output = "unknown_token foo bar\n"
    rows = parse_pfctl_rules(output)
    assert len(rows) == 1
    assert rows[0]["action"] is None
    assert rows[0]["rule"] == "unknown_token foo bar"


def test_parse_pfctl_rules_multiple_rules() -> None:
    output = (
        "pass in quick on em0 proto tcp from any to any port = 80\n"
        "block drop in log quick on em0 proto tcp from <bruteforce> to any port = 22\n"
        "pass out all keep state\n"
    )
    rows = parse_pfctl_rules(output)
    assert len(rows) == 3
    assert rows[0]["action"] == "pass"
    assert rows[1]["action"] == "block"
    assert rows[2]["action"] == "pass"


# ---------------------------------------------------------------------------
# parse_pfctl_states
# ---------------------------------------------------------------------------


def test_parse_pfctl_states_extracts_tcp_state() -> None:
    output = "tcp em0 10.0.0.1:50234 -> 93.184.216.34:443: ESTABLISHED:ESTABLISHED\n"
    rows = parse_pfctl_states(output)
    assert len(rows) == 1
    assert rows[0]["proto"] == "tcp"
    assert rows[0]["iface"] == "em0"
    assert rows[0]["src"] == "10.0.0.1:50234"
    assert rows[0]["direction"] == "->"
    assert rows[0]["dst"] == "93.184.216.34:443"


def test_parse_pfctl_states_extracts_bidirectional_arrow() -> None:
    output = "udp em0 10.0.0.5:1234 <-> 8.8.8.8:53\n"
    rows = parse_pfctl_states(output)
    assert rows[0]["direction"] == "<->"


def test_parse_pfctl_states_skips_blank_and_comment_lines() -> None:
    output = "\n# header\ntcp em0 1.1.1.1:1 -> 2.2.2.2:2\n"
    rows = parse_pfctl_states(output)
    assert len(rows) == 1


def test_parse_pfctl_states_empty_input_returns_empty_list() -> None:
    assert parse_pfctl_states("") == []


def test_parse_pfctl_states_malformed_line_included_with_none_fields() -> None:
    output = "this is not a state line\n"
    rows = parse_pfctl_states(output)
    assert len(rows) == 1
    assert rows[0]["proto"] is None
    assert rows[0]["state"] == "this is not a state line"


def test_parse_pfctl_states_returns_total_matching_row_count() -> None:
    output = (
        "tcp em0 10.0.0.1:1 -> 10.0.0.2:80: ESTABLISHED\n"
        "tcp em0 10.0.0.1:2 -> 10.0.0.2:443: ESTABLISHED\n"
        "udp em0 10.0.0.1:53 -> 8.8.8.8:53\n"
    )
    rows = parse_pfctl_states(output)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# parse_pfctl_nat
# ---------------------------------------------------------------------------


def test_parse_pfctl_nat_extracts_nat_rule() -> None:
    output = "nat on em0 inet from 192.168.1.0/24 to any -> (em0)\n"
    rows = parse_pfctl_nat(output)
    assert len(rows) == 1
    assert rows[0]["action"] == "nat"


def test_parse_pfctl_nat_extracts_rdr_rule() -> None:
    output = "rdr on em0 proto tcp from any to any port = 80 -> 10.0.0.100 port 8080\n"
    rows = parse_pfctl_nat(output)
    assert rows[0]["action"] == "rdr"


def test_parse_pfctl_nat_normalises_no_nat_to_underscore() -> None:
    output = "no nat on em0 from 10.0.0.0/8 to 10.0.0.0/8\n"
    rows = parse_pfctl_nat(output)
    assert rows[0]["action"] == "no_nat"


def test_parse_pfctl_nat_empty_input_returns_empty_list() -> None:
    assert parse_pfctl_nat("") == []


# ---------------------------------------------------------------------------
# _netmask_to_cidr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "netmask, expected",
    [
        ("0xffffff00", 24),
        ("0xffff0000", 16),
        ("0xffffffff", 32),
        ("0x00000000", 0),
        ("255.255.255.0", 24),
        ("255.255.0.0", 16),
        ("255.0.0.0", 8),
        ("garbage", None),
        ("0xinvalid", None),
    ],
)
def test_netmask_to_cidr(netmask: str, expected: int | None) -> None:
    assert _netmask_to_cidr(netmask) == expected


# ---------------------------------------------------------------------------
# parse_ifconfig
# ---------------------------------------------------------------------------

_IFCONFIG_SAMPLE = """\
em0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500
\tether 08:00:27:1a:2b:3c
\tinet 192.168.1.1 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: Ethernet autoselect (1000baseT <full-duplex>)
\tstatus: active
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1
\tstatus: active
"""


def test_parse_ifconfig_extracts_two_interfaces() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    names = [i["name"] for i in ifaces]
    assert "em0" in names
    assert "lo0" in names


def test_parse_ifconfig_extracts_inet_with_cidr() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    em0 = next(i for i in ifaces if i["name"] == "em0")
    assert "192.168.1.1/24" in em0["inet"]


def test_parse_ifconfig_extracts_ether() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    em0 = next(i for i in ifaces if i["name"] == "em0")
    assert em0["ether"] == "08:00:27:1a:2b:3c"


def test_parse_ifconfig_extracts_status() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    em0 = next(i for i in ifaces if i["name"] == "em0")
    assert em0["status"] == "active"


def test_parse_ifconfig_extracts_media() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    em0 = next(i for i in ifaces if i["name"] == "em0")
    assert "Ethernet" in em0["media"]


def test_parse_ifconfig_extracts_inet6() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    lo0 = next(i for i in ifaces if i["name"] == "lo0")
    assert "::1" in lo0["inet6"]


def test_parse_ifconfig_loopback_has_no_ether() -> None:
    ifaces = parse_ifconfig(_IFCONFIG_SAMPLE)
    lo0 = next(i for i in ifaces if i["name"] == "lo0")
    assert lo0["ether"] is None


def test_parse_ifconfig_empty_input_returns_empty_list() -> None:
    assert parse_ifconfig("") == []


def test_parse_ifconfig_interface_without_inet_has_empty_list() -> None:
    output = (
        "tun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> metric 0 mtu 1500\n"
        "\tstatus: no carrier\n"
    )
    ifaces = parse_ifconfig(output)
    assert ifaces[0]["inet"] == []
    assert ifaces[0]["inet6"] == []


# ---------------------------------------------------------------------------
# parse_gateways_xml
# ---------------------------------------------------------------------------

_GATEWAY_XML_SNIPPET = """<gateways>
  <gateway_item>
    <name>WAN_DHCP</name>
    <interface>wan</interface>
    <gateway>192.168.0.1</gateway>
    <monitor>192.168.0.1</monitor>
    <descr>Interface WAN Gateway</descr>
    <defaultgw/>
  </gateway_item>
  <gateway_item>
    <name>LAN_GW</name>
    <interface>lan</interface>
    <gateway>10.0.0.254</gateway>
    <descr>LAN Gateway</descr>
  </gateway_item>
</gateways>"""

_CONFIG_XML_WITH_GATEWAYS = f"""<?xml version="1.0"?>
<pfsense>
  <version>22.8</version>
  {_GATEWAY_XML_SNIPPET}
</pfsense>"""


def test_parse_gateways_xml_extracts_gateway_name() -> None:
    rows = parse_gateways_xml(_GATEWAY_XML_SNIPPET)
    names = [r["name"] for r in rows]
    assert "WAN_DHCP" in names
    assert "LAN_GW" in names


def test_parse_gateways_xml_defaultgw_is_true_when_element_present() -> None:
    rows = parse_gateways_xml(_GATEWAY_XML_SNIPPET)
    wan = next(r for r in rows if r["name"] == "WAN_DHCP")
    assert wan["defaultgw"] is True


def test_parse_gateways_xml_defaultgw_is_false_when_element_absent() -> None:
    rows = parse_gateways_xml(_GATEWAY_XML_SNIPPET)
    lan = next(r for r in rows if r["name"] == "LAN_GW")
    assert lan["defaultgw"] is False


def test_parse_gateways_xml_extracts_gateway_ip() -> None:
    rows = parse_gateways_xml(_GATEWAY_XML_SNIPPET)
    wan = next(r for r in rows if r["name"] == "WAN_DHCP")
    assert wan["gateway"] == "192.168.0.1"
    assert wan["interface"] == "wan"


def test_parse_gateways_xml_missing_optional_tag_returns_none() -> None:
    rows = parse_gateways_xml(_GATEWAY_XML_SNIPPET)
    lan = next(r for r in rows if r["name"] == "LAN_GW")
    assert lan["monitor"] is None


def test_parse_gateways_xml_parses_full_config_xml() -> None:
    rows = parse_gateways_xml(_CONFIG_XML_WITH_GATEWAYS)
    assert len(rows) == 2


def test_parse_gateways_xml_empty_input_returns_empty_list() -> None:
    assert parse_gateways_xml("") == []


def test_parse_gateways_xml_malformed_xml_returns_empty_list() -> None:
    assert parse_gateways_xml("<not closed xml") == []


def test_parse_gateways_xml_no_gateways_block_returns_empty_list() -> None:
    assert parse_gateways_xml("<pfsense><version>22.8</version></pfsense>") == []


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_version_returns_structured_version() -> None:
    connector = PfSenseConnector()
    version_content = (
        "2.7.2-RELEASE (amd64)\nbuilt on Fri Jan 12 18:00:00 UTC 2024\nFreeBSD 14.1-RELEASE-p5 #1\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(version_content)
        result = await connector.get_version(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "cat /etc/version", raw_jwt="")
    assert result["version"] == "2.7.2-RELEASE"
    assert result["kernel"] == "FreeBSD 14.1-RELEASE-p5"
    assert "error" not in result or result.get("error") is None


@pytest.mark.asyncio
async def test_pfsense_version_returns_error_on_empty_stdout() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("", exit_status=1)
        result = await connector.get_version(_TARGET, {})
    assert result["version"] is None
    assert "error" in result


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_firewall_rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_firewall_rules_runs_pfctl_sr() -> None:
    connector = PfSenseConnector()
    pfctl_output = "pass out all keep state\nblock drop in quick on em0 from any to any port = 22\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(pfctl_output)
        result = await connector.firewall_rules(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "pfctl -sr", raw_jwt="")
    assert result["total"] == 2
    assert result["rows"][0]["action"] == "pass"
    assert result["rows"][1]["action"] == "block"


@pytest.mark.asyncio
async def test_pfsense_firewall_rules_returns_error_on_failure() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("", exit_status=1)
        result = await connector.firewall_rules(_TARGET, {})
    assert result["rows"] == []
    assert result["total"] == 0
    assert "error" in result


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_firewall_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_firewall_state_runs_pfctl_ss() -> None:
    connector = PfSenseConnector()
    state_output = (
        "tcp em0 10.0.0.1:5000 -> 1.2.3.4:443: ESTABLISHED:ESTABLISHED\n"
        "udp em0 10.0.0.2:1234 -> 8.8.8.8:53\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(state_output)
        result = await connector.firewall_state(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "pfctl -ss", raw_jwt="")
    assert result["total"] == 2
    assert result["rows"][0]["proto"] == "tcp"
    assert result["rows"][1]["proto"] == "udp"


@pytest.mark.asyncio
async def test_pfsense_firewall_state_returns_rows_and_total() -> None:
    """The state handler always returns both ``rows`` and ``total`` keys."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("")
        result = await connector.firewall_state(_TARGET, {})
    assert "rows" in result
    assert "total" in result


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_nat_rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_nat_rules_runs_pfctl_sn() -> None:
    connector = PfSenseConnector()
    nat_output = "nat on em0 inet from 192.168.0.0/24 to any -> (em0)\n"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(nat_output)
        result = await connector.nat_rules(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "pfctl -sn", raw_jwt="")
    assert result["total"] == 1
    assert result["rows"][0]["action"] == "nat"


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_interface_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_interface_list_runs_ifconfig_a() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_IFCONFIG_SAMPLE)
        result = await connector.interface_list(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "ifconfig -a", raw_jwt="")
    assert result["total"] == 2
    names = [r["name"] for r in result["rows"]]
    assert "em0" in names
    assert "lo0" in names


@pytest.mark.asyncio
async def test_pfsense_interface_list_empty_output_returns_empty_rows() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("")
        result = await connector.interface_list(_TARGET, {})
    assert result["rows"] == []
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_gateway_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_gateway_list_reads_config_xml() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(_CONFIG_XML_WITH_GATEWAYS)
        result = await connector.gateway_list(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "cat /cf/conf/config.xml", raw_jwt="")
    assert result["total"] == 2
    names = [r["name"] for r in result["rows"]]
    assert "WAN_DHCP" in names


@pytest.mark.asyncio
async def test_pfsense_gateway_list_malformed_xml_returns_empty_rows() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("<broken xml")
        result = await connector.gateway_list(_TARGET, {})
    assert result["rows"] == []
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# Bound-method shims -- pfsense_config_show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_config_show_returns_xml_and_length() -> None:
    connector = PfSenseConnector()
    xml_content = "<pfsense><version>22.8</version></pfsense>"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(xml_content)
        result = await connector.config_show(_TARGET, {})
    mock_cmd.assert_awaited_once_with(_TARGET, "cat /cf/conf/config.xml", raw_jwt="")
    assert result["config_xml"] == xml_content
    assert result["length"] == len(xml_content)


@pytest.mark.asyncio
async def test_pfsense_config_show_returns_error_on_failure() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc("", exit_status=2)
        result = await connector.config_show(_TARGET, {})
    assert result["config_xml"] is None
    assert "error" in result


# ---------------------------------------------------------------------------
# PFSENSE_OPS registration shape
# ---------------------------------------------------------------------------


def test_pfsense_ops_has_eight_entries() -> None:
    """T1 canary + 7 T2 read ops = 8 total."""
    assert len(PFSENSE_OPS) == 8


def test_pfsense_ops_about_is_first() -> None:
    assert PFSENSE_OPS[0].op_id == "pfsense.about"


def test_pfsense_ops_all_have_pfsense_namespace() -> None:
    for op in PFSENSE_OPS:
        assert op.op_id.startswith("pfsense."), f"{op.op_id!r} lacks pfsense. prefix"


def test_pfsense_ops_all_safe() -> None:
    for op in PFSENSE_OPS:
        assert op.safety_level == "safe", f"{op.op_id!r} has safety_level != 'safe'"


def test_pfsense_ops_all_parameter_schemas_have_additional_properties_false() -> None:
    for op in PFSENSE_OPS:
        assert op.parameter_schema.get("additionalProperties") is False, (
            f"{op.op_id!r} parameter_schema missing additionalProperties=False"
        )


def test_pfsense_ops_all_have_llm_instructions() -> None:
    for op in PFSENSE_OPS:
        assert op.llm_instructions, f"{op.op_id!r} missing llm_instructions"
        assert op.llm_instructions.get("when_to_use"), (
            f"{op.op_id!r} missing llm_instructions.when_to_use"
        )


def test_pfsense_ops_covers_expected_op_ids() -> None:
    op_ids = {op.op_id for op in PFSENSE_OPS}
    expected = {
        "pfsense.about",
        "pfsense.version",
        "pfsense.firewall.rules",
        "pfsense.firewall.state",
        "pfsense.nat.rules",
        "pfsense.interface.list",
        "pfsense.gateway.list",
        "pfsense.config.show",
    }
    assert op_ids == expected


def test_pfsense_ops_group_keys_include_new_groups() -> None:
    group_keys = {op.group_key for op in PFSENSE_OPS if op.group_key}
    assert "identity" in group_keys
    assert "firewall" in group_keys
    assert "nat" in group_keys
    assert "network" in group_keys
    assert "config" in group_keys


def test_pfsense_ops_handler_attrs_exist_on_connector() -> None:
    """Every ``handler_attr`` in PFSENSE_OPS resolves to a method on PfSenseConnector."""
    for op in PFSENSE_OPS:
        assert hasattr(PfSenseConnector, op.handler_attr), (
            f"{op.op_id!r}: PfSenseConnector has no attr {op.handler_attr!r}"
        )


def test_pfsense_ops_no_requires_approval() -> None:
    for op in PFSENSE_OPS:
        assert not op.requires_approval, f"{op.op_id!r} has requires_approval=True"
