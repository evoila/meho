# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense teardown-inverse destructive ops (#3313).

Covers the three ``safety_level="destructive"`` ops that reverse a governed
bring-up -- ``pfsense.route.static.delete``, ``pfsense.gateway.delete``, and
``pfsense.alias.member.remove`` -- across three layers:

* **Unit** (mocked ``_run_command``): the canonical route matcher, the
  fail-closed gateway reference scan, the member-token split, the
  delete/remove playback fragments, input validation, and the handler happy /
  refusal / verify-failure paths (0-match ``not_found``, ambiguous, gateway
  ``referenced``, member ``member_not_found`` / ``last_member`` / duplicate).
  The ``alias.member.remove`` shared-alias path is tested hardest: a fixture
  proving the alias identity + every *other* member survive a single-member
  removal.
* **Metadata**: registration classification (destructive + requires_approval
  + ``destructive`` tag + group), response-schema conformance, and proof the
  ops fold into the destructive-tier service-grant refusal -- including
  ``alias.member.remove`` whose op-id does **not** end in ``.delete`` yet is
  still refused via its ``destructive`` safety level / tag.
* **Governed-flow conformance** (full ``call_operation`` dispatch against a
  seeded recording connector): preview -> park (hash + blast radius) ->
  distinct-human approve -> audited resume, plus the fail-closed post-approval
  re-check (referenced gateway) and the park-refused-without-blast-radius path
  for a non-resolving target.

All config.xml fixtures are synthetic (RFC 5737 / RFC 1918 addresses, no lab
hostnames / IPs / VLANs) -- the public repo must never carry lab values.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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
    _build_alias_member_remove_playback,
    _build_gateway_delete_playback,
    _build_route_delete_playback,
    _match_static_routes_canonical,
    find_gateway_references,
)
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003313")
_ROUTE_OP = "pfsense.route.static.delete"
_GATEWAY_OP = "pfsense.gateway.delete"
_MEMBER_OP = "pfsense.alias.member.remove"


# ---------------------------------------------------------------------------
# Synthetic config.xml fixtures (RFC 5737 / RFC 1918 -- no lab values)
# ---------------------------------------------------------------------------

# Two static routes (both -> LAB_GW), two gateways (LAB_GW referenced,
# RETIRING_GW unreferenced), a gateway group + default-gateway pointer.
_CONFIG_ROUTING = (
    "<pfsense>"
    "<staticroutes>"
    "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway><descr>lab net</descr></route>"
    "<route><network>10.20.0.0/24</network><gateway>LAB_GW</gateway><descr>svc net</descr></route>"
    "</staticroutes>"
    "<gateways>"
    "<gateway_item><name>LAB_GW</name><interface>opt1</interface>"
    "<gateway>192.0.2.1</gateway><descr>lab gw</descr></gateway_item>"
    "<gateway_item><name>RETIRING_GW</name><interface>opt2</interface>"
    "<gateway>192.0.2.2</gateway><descr>retiring</descr></gateway_item>"
    "<gateway_group><name>FAILOVER</name><item>LAB_GW|1</item><item>WAN_DHCP|2</item>"
    "<descr>failover grp</descr></gateway_group>"
    "<defaultgw4>WAN_DHCP</defaultgw4>"
    "</gateways>"
    "</pfsense>"
)
# After deleting route 10.10.0.0/24 (one route remains).
_CONFIG_ROUTING_NO_ROUTE = _CONFIG_ROUTING.replace(
    "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway><descr>lab net</descr></route>",
    "",
)
# After deleting the unreferenced RETIRING_GW gateway.
_CONFIG_ROUTING_NO_RETIRING_GW = _CONFIG_ROUTING.replace(
    "<gateway_item><name>RETIRING_GW</name><interface>opt2</interface>"
    "<gateway>192.0.2.2</gateway><descr>retiring</descr></gateway_item>",
    "",
)
# Two routes canonicalising to the same network (corrupt -> ambiguous).
_CONFIG_DUP_ROUTE = (
    "<pfsense><staticroutes>"
    "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "<route><network>10.10.0.5/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes></pfsense>"
)
# Two gateways sharing a name (corrupt -> ambiguous).
_CONFIG_DUP_GW = (
    "<pfsense><gateways>"
    "<gateway_item><name>DUP_GW</name><interface>opt1</interface>"
    "<gateway>192.0.2.1</gateway></gateway_item>"
    "<gateway_item><name>DUP_GW</name><interface>opt2</interface>"
    "<gateway>192.0.2.2</gateway></gateway_item>"
    "</gateways></pfsense>"
)

# A SHARED host alias with three members (+ aligned detail) and a separate
# single-member alias (SOLO) that exercises the last_member refusal.
_CONFIG_SHARED_ALIAS = (
    "<pfsense><aliases>"
    "<alias><name>SHARED_HOSTS</name><type>host</type>"
    "<address>192.0.2.10 192.0.2.11 198.51.100.5</address>"
    "<detail>env-a||env-b||env-c</detail><descr>shared</descr></alias>"
    "<alias><name>SOLO</name><type>host</type>"
    "<address>203.0.113.9</address><detail>only one</detail></alias>"
    "</aliases></pfsense>"
)
# After removing 198.51.100.5 from SHARED_HOSTS: the alias survives with its
# two other members (and aligned detail); SOLO is untouched; count unchanged.
_CONFIG_SHARED_ALIAS_AFTER = (
    "<pfsense><aliases>"
    "<alias><name>SHARED_HOSTS</name><type>host</type>"
    "<address>192.0.2.10 192.0.2.11</address>"
    "<detail>env-a||env-b</detail><descr>shared</descr></alias>"
    "<alias><name>SOLO</name><type>host</type>"
    "<address>203.0.113.9</address><detail>only one</detail></alias>"
    "</aliases></pfsense>"
)
# A degenerate alias carrying the same member twice (-> ambiguous).
_CONFIG_DUP_MEMBER = (
    "<pfsense><aliases>"
    "<alias><name>SHARED_HOSTS</name><type>host</type>"
    "<address>192.0.2.10 198.51.100.5 198.51.100.5</address>"
    "<detail>env-a||env-c||env-c2</detail></alias>"
    "</aliases></pfsense>"
)
# Two aliases sharing a name (-> ambiguous).
_CONFIG_DUP_ALIAS = (
    "<pfsense><aliases>"
    "<alias><name>SHARED_HOSTS</name><type>host</type><address>192.0.2.10</address></alias>"
    "<alias><name>SHARED_HOSTS</name><type>host</type><address>192.0.2.11</address></alias>"
    "</aliases></pfsense>"
)


# ---------------------------------------------------------------------------
# Environment fixtures
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


# ---------------------------------------------------------------------------
# Unit-test stubs
# ---------------------------------------------------------------------------


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _cmds(mock_cmd: AsyncMock) -> list[str]:
    return [call.args[1] for call in mock_cmd.await_args_list]


# ===========================================================================
# Part A -- unit: canonical route matcher + gateway reference scan + members
# ===========================================================================


def test_match_static_routes_canonical_matches_host_bits_set() -> None:
    # Query is canonical; a stored route with host bits set still matches.
    assert len(_match_static_routes_canonical(_CONFIG_ROUTING, "10.10.0.0/24")) == 1
    assert len(_match_static_routes_canonical(_CONFIG_DUP_ROUTE, "10.10.0.0/24")) == 2
    assert _match_static_routes_canonical(_CONFIG_ROUTING, "10.99.0.0/24") == []


def test_find_gateway_references_route_and_group() -> None:
    refs = find_gateway_references(_CONFIG_ROUTING, "LAB_GW")
    kinds = sorted(r["kind"] for r in refs)
    # Two static routes + one gateway group name it.
    assert kinds == ["gateway_group", "static_route", "static_route"]
    ids = {(r["kind"], r["id"]) for r in refs}
    assert ("gateway_group", "FAILOVER") in ids
    assert ("static_route", "10.10.0.0/24") in ids


def test_find_gateway_references_default_gateway_pointer() -> None:
    # WAN_DHCP is the default-gateway pointer AND a gateway-group member.
    refs = find_gateway_references(_CONFIG_ROUTING, "WAN_DHCP")
    kinds = sorted(r["kind"] for r in refs)
    assert kinds == ["default_gateway", "gateway_group"]
    assert any(r["kind"] == "default_gateway" and r["id"] == "defaultgw4" for r in refs)


def test_find_gateway_references_legacy_defaultgw_flag() -> None:
    cfg = (
        "<pfsense><gateways>"
        "<gateway_item><name>OLD_GW</name><gateway>192.0.2.9</gateway><defaultgw></defaultgw>"
        "</gateway_item></gateways></pfsense>"
    )
    refs = find_gateway_references(cfg, "OLD_GW")
    assert [(r["kind"], r["id"]) for r in refs] == [("default_gateway", "gateway_item_flag")]


def test_find_gateway_references_unreferenced_is_empty() -> None:
    assert find_gateway_references(_CONFIG_ROUTING, "RETIRING_GW") == []
    assert find_gateway_references("", "LAB_GW") == []
    assert find_gateway_references("<broken", "LAB_GW") == []


# ===========================================================================
# Part A -- unit: playback fragments delete/remove exactly one, safely
# ===========================================================================


def test_route_delete_playback_is_single_object_and_safe() -> None:
    frag = _build_route_delete_playback("10.10.0.0/24")
    assert "$meho_network = '10.10.0.0/24';" in frag
    assert "if ($meho_removed === 1) {" in frag
    assert "write_config('meho: delete static route 10.10.0.0/24');" in frag
    assert "system_routing_configure();" in frag
    assert "<?php" not in frag
    assert "\nexec" not in frag


def test_gateway_delete_playback_is_single_object_and_safe() -> None:
    frag = _build_gateway_delete_playback("RETIRING_GW")
    assert "$meho_name = 'RETIRING_GW';" in frag
    assert "if ($meho_removed === 1) {" in frag
    assert "write_config('meho: delete gateway RETIRING_GW');" in frag
    assert "system_routing_configure();" in frag
    assert "<?php" not in frag


def test_alias_member_remove_playback_is_surgical_and_safe() -> None:
    frag = _build_alias_member_remove_playback("SHARED_HOSTS", "198.51.100.5")
    assert "$meho_name = 'SHARED_HOSTS';" in frag
    assert "$meho_value = '198.51.100.5';" in frag
    # Persists only when exactly one alias matched AND the removal applied
    # (which requires a surviving member -> never empties a shared alias).
    assert "if ($meho_matched === 1 && $meho_applied === 1) {" in frag
    assert "count($meho_new_addr) >= 1" in frag
    # Detail is realigned positionally alongside the address tokens.
    assert "$meho_new_det[] = $meho_det[$meho_j];" in frag
    assert "filter_configure();" in frag
    assert "<?php" not in frag


# ===========================================================================
# Part A -- unit: input validation rejects before any SSH round-trip
# ===========================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"network": "not-a-cidr"},
        {"network": "10.0.0.0/24; rm -rf /"},
        {},  # missing network
    ],
)
async def test_route_delete_rejects_bad_network_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.route_static_delete(None, params)
    assert mock_cmd.await_count == 0


@pytest.mark.parametrize(
    "params",
    [
        {"name": "bad name"},
        {"name": "gw'; write_config("},
        {},  # missing name
    ],
)
async def test_gateway_delete_rejects_bad_name_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.gateway_delete(None, params)
    assert mock_cmd.await_count == 0


@pytest.mark.parametrize(
    "params",
    [
        {"name": "SHARED_HOSTS", "value": "has space"},
        {"name": "SHARED_HOSTS", "value": "1.2.3.4; rm"},
        {"name": "bad name", "value": "192.0.2.10"},
        {"name": "SHARED_HOSTS"},  # missing value
        {"value": "192.0.2.10"},  # missing name
    ],
)
async def test_member_remove_rejects_bad_params_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.alias_member_remove(None, params)
    assert mock_cmd.await_count == 0


# ===========================================================================
# Part A -- unit: route.static.delete handler paths
# ===========================================================================


async def test_route_delete_happy_path_deletes_one_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ROUTING),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm cleanup
            _proc(_CONFIG_ROUTING_NO_ROUTE),  # read-back verify
        ]
        result = await connector.route_static_delete(None, {"network": "10.10.0.0/24"})
    assert result["status"] == "deleted"
    assert result["verified"] is True
    assert result["matched"] == 1
    assert result["routes_before"] == 2
    assert result["routes_after"] == 1
    assert result["removed"]["network"] == "10.10.0.0/24"
    cmds = _cmds(mock_cmd)
    assert cmds[0] == "cat /cf/conf/config.xml"
    assert cmds[2] == "pfSsh.php playback meho_route_delete_10_10_0_0_24"


async def test_route_delete_canonicalises_host_bits_query() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ROUTING),
            _proc("", 0),
            _proc("", 0),
            _proc("", 0),
            _proc(_CONFIG_ROUTING_NO_ROUTE),
        ]
        # Host bits set -> canonicalised to 10.10.0.0/24 -> matches.
        result = await connector.route_static_delete(None, {"network": "10.10.0.5/24"})
    assert result["status"] == "deleted"
    assert result["network"] == "10.10.0.0/24"


async def test_route_delete_not_found_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_ROUTING)]
        result = await connector.route_static_delete(None, {"network": "10.99.0.0/24"})
    assert result["status"] == "not_found"
    assert result["matched"] == 0
    assert mock_cmd.await_count == 1


async def test_route_delete_ambiguous_refuses_fail_closed() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_DUP_ROUTE)]
        result = await connector.route_static_delete(None, {"network": "10.10.0.0/24"})
    assert result["status"] == "ambiguous"
    assert result["matched"] == 2
    assert mock_cmd.await_count == 1


async def test_route_delete_verification_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ROUTING),  # guard read (2 routes)
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_ROUTING),  # read-back: route still present
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.route_static_delete(None, {"network": "10.10.0.0/24"})


# ===========================================================================
# Part A -- unit: gateway.delete handler paths
# ===========================================================================


async def test_gateway_delete_happy_path_deletes_one_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ROUTING),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_ROUTING_NO_RETIRING_GW),  # read-back verify
        ]
        result = await connector.gateway_delete(None, {"name": "RETIRING_GW"})
    assert result["status"] == "deleted"
    assert result["verified"] is True
    assert result["reference_count"] == 0
    assert result["gateways_before"] == 2
    assert result["gateways_after"] == 1
    assert result["removed"]["name"] == "RETIRING_GW"


async def test_gateway_delete_referenced_refuses_and_names_referrers() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_ROUTING)]
        result = await connector.gateway_delete(None, {"name": "LAB_GW"})
    assert result["status"] == "referenced"
    assert result["reference_count"] == 3  # 2 routes + 1 gateway group
    kinds = sorted(r["kind"] for r in result["references"])
    assert kinds == ["gateway_group", "static_route", "static_route"]
    assert "still referenced" in result["guidance"]
    # Fail-closed: refuses after a single guard read, stages nothing.
    assert mock_cmd.await_count == 1


async def test_gateway_delete_not_found_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_ROUTING)]
        result = await connector.gateway_delete(None, {"name": "NOPE_GW"})
    assert result["status"] == "not_found"
    assert result["matched"] == 0
    assert mock_cmd.await_count == 1


async def test_gateway_delete_ambiguous_refuses_fail_closed() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_DUP_GW)]
        result = await connector.gateway_delete(None, {"name": "DUP_GW"})
    assert result["status"] == "ambiguous"
    assert result["matched"] == 2
    assert mock_cmd.await_count == 1


async def test_gateway_delete_verification_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ROUTING),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_ROUTING),  # read-back: gateway still present
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.gateway_delete(None, {"name": "RETIRING_GW"})


# ===========================================================================
# Part A -- unit: alias.member.remove handler paths (the delicate one)
# ===========================================================================


async def test_member_remove_happy_path_keeps_shared_alias_and_others() -> None:
    """Removing one member trims the alias in place: identity + other members survive."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_SHARED_ALIAS),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_SHARED_ALIAS_AFTER),  # read-back verify
        ]
        result = await connector.alias_member_remove(
            None, {"name": "SHARED_HOSTS", "value": "198.51.100.5"}
        )
    assert result["status"] == "removed"
    assert result["verified"] is True
    assert result["removed"] is True
    assert result["alias_retained"] is True
    assert result["members_before"] == 3
    assert result["members_after"] == 2
    # The alias identity + the two OTHER members survive; only the named member left.
    assert result["residual_members"] == ["192.0.2.10", "192.0.2.11"]
    cmds = _cmds(mock_cmd)
    body = cmds[1]
    assert "$meho_value = '198.51.100.5';" in body


async def test_member_remove_not_found_alias_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_SHARED_ALIAS)]
        result = await connector.alias_member_remove(None, {"name": "NOPE", "value": "192.0.2.10"})
    assert result["status"] == "not_found"
    assert result["matched"] == 0
    assert mock_cmd.await_count == 1


async def test_member_remove_member_not_found_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_SHARED_ALIAS)]
        result = await connector.alias_member_remove(
            None, {"name": "SHARED_HOSTS", "value": "203.0.113.99"}
        )
    assert result["status"] == "member_not_found"
    assert result["matched"] == 1
    assert result["member_matched"] == 0
    assert mock_cmd.await_count == 1


async def test_member_remove_last_member_refuses_fail_closed() -> None:
    """Removing the ONLY member must refuse -- never empty a shared alias into deletion."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_SHARED_ALIAS)]
        result = await connector.alias_member_remove(None, {"name": "SOLO", "value": "203.0.113.9"})
    assert result["status"] == "last_member"
    assert result["member_matched"] == 1
    assert result["members_before"] == 1
    assert "alias.delete" in result["guidance"]
    # Fail-closed: refuses after a single guard read, stages nothing.
    assert mock_cmd.await_count == 1


async def test_member_remove_ambiguous_duplicate_alias_name() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_DUP_ALIAS)]
        result = await connector.alias_member_remove(
            None, {"name": "SHARED_HOSTS", "value": "192.0.2.10"}
        )
    assert result["status"] == "ambiguous"
    assert result["matched"] == 2
    assert mock_cmd.await_count == 1


async def test_member_remove_ambiguous_duplicate_member_value() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_DUP_MEMBER)]
        result = await connector.alias_member_remove(
            None, {"name": "SHARED_HOSTS", "value": "198.51.100.5"}
        )
    assert result["status"] == "ambiguous"
    assert result["member_matched"] == 2
    assert mock_cmd.await_count == 1


async def test_member_remove_verification_failure_raises_if_alias_deleted() -> None:
    """A playback that emptied/deleted the shared alias (or dropped an extra member) raises."""
    connector = PfSenseConnector()
    empty_aliases = "<pfsense><aliases></aliases></pfsense>"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_SHARED_ALIAS),  # guard read (2 aliases)
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(empty_aliases),  # read-back: the alias vanished (bulk over-deletion)
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.alias_member_remove(
                None, {"name": "SHARED_HOSTS", "value": "198.51.100.5"}
            )


# ===========================================================================
# Part B -- registration classification + schema conformance
# ===========================================================================


def test_teardown_ops_are_destructive_requires_approval_with_tag() -> None:
    ids = {_ROUTE_OP, _GATEWAY_OP, _MEMBER_OP}
    seen = set()
    for op in PFSENSE_OPS:
        if op.op_id not in ids:
            continue
        seen.add(op.op_id)
        assert op.safety_level == "destructive", op.op_id
        assert op.requires_approval is True, op.op_id
        assert "destructive" in op.tags, op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id
        assert op.llm_instructions and op.llm_instructions.get("when_to_use"), op.op_id
    assert seen == ids


def test_teardown_ops_group_keys() -> None:
    by_id = {op.op_id: op for op in PFSENSE_OPS}
    assert by_id[_ROUTE_OP].group_key == "routing"
    assert by_id[_GATEWAY_OP].group_key == "routing"
    assert by_id[_MEMBER_OP].group_key == "alias"


def test_route_delete_response_schema_accepts_outcomes() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == _ROUTE_OP)
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "delete",
            "resource": "static_route",
            "status": "deleted",
            "network": "10.10.0.0/24",
            "matched": 1,
            "removed": {"network": "10.10.0.0/24"},
            "routes_before": 2,
            "routes_after": 1,
            "verified": True,
            "guidance": None,
        }
    )


def test_gateway_delete_response_schema_accepts_referenced() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == _GATEWAY_OP)
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "delete",
            "resource": "gateway",
            "status": "referenced",
            "name": "LAB_GW",
            "matched": 1,
            "removed": None,
            "references": [{"kind": "static_route", "id": "10.10.0.0/24", "descr": "lab net"}],
            "reference_count": 3,
            "gateways_before": 2,
            "gateways_after": None,
            "verified": False,
            "guidance": "referenced",
        }
    )


def test_member_remove_response_schema_accepts_outcomes() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == _MEMBER_OP)
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "update",
            "resource": "alias_member",
            "status": "removed",
            "alias": "SHARED_HOSTS",
            "value": "198.51.100.5",
            "matched": 1,
            "member_matched": 1,
            "removed": True,
            "members_before": 3,
            "members_after": 2,
            "residual_members": ["192.0.2.10", "192.0.2.11"],
            "alias_retained": True,
            "verified": True,
            "guidance": None,
        }
    )
    validator.validate(
        {
            "op_class": "update",
            "resource": "alias_member",
            "status": "last_member",
            "alias": "SOLO",
            "value": "203.0.113.9",
            "matched": 1,
            "member_matched": 1,
            "removed": False,
            "members_before": 1,
            "members_after": None,
            "residual_members": None,
            "alias_retained": None,
            "verified": False,
            "guidance": "last member",
        }
    )


# ===========================================================================
# Part C -- governed-flow conformance (full dispatch)
# ===========================================================================


def _make_operator(
    *, sub: str = "op-pf", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="pfSense Teardown Conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "2.7") -> None:
        self.version = version


class _FakePfsenseTarget:
    def __init__(self) -> None:
        self.product = "pfsense"
        self.fingerprint = _FakeFingerprint(version="2.7")
        self.preferred_impl_id: str | None = "pfsense-ssh"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "pf-perimeter"
        self.host = "pf.test"
        self.port = 22
        self.auth_model = "shared_service_account"


class _RecordingPfSense(PfSenseConnector):
    """A PfSenseConnector whose ``_run_command`` replays canned config.xml.

    Seeded into ``_CONNECTOR_INSTANCE_CACHE`` so the dispatcher (and the
    blast-radius preview builder) drive real dispatch without SSH. Returns
    ``config_before`` until a ``pfSsh.php playback`` command runs, then
    ``config_after`` -- exercising the handler's read-back verification.
    """

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
        if command.startswith("pfSsh.php playback"):
            self._played = True
        return _proc("", 0)

    @property
    def playback_ran(self) -> bool:
        return any(c.startswith("pfSsh.php playback") for c in self.commands)


async def _seed_target(name: str = "pf-perimeter") -> UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
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
                notes="seeded by test_connectors_pfsense_teardown_ops",
            )
        )
        await s.commit()
    return target_id


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
async def test_ops_registered_destructive_requires_approval() -> None:
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_ROUTING))
    async with get_sessionmaker()() as s:
        for op_id in (_ROUTE_OP, _GATEWAY_OP, _MEMBER_OP):
            row = (
                await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
            ).scalar_one()
            assert row.safety_level == "destructive", op_id
            assert row.requires_approval is True, op_id
            assert row.source_kind == "typed", op_id


@pytest.mark.asyncio
async def test_full_governed_flow_route_delete() -> None:
    """preview -> park (hash + blast radius) -> distinct human approve -> resume delete."""
    recorder = _RecordingPfSense(
        config_before=_CONFIG_ROUTING, config_after=_CONFIG_ROUTING_NO_ROUTE
    )
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _ROUTE_OP,
        "target": "pf-perimeter",
        "params": {"network": "10.10.0.0/24"},
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert not recorder.playback_ran

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["object"]["kind"] == "static_route"
    assert blast["object"]["network"] == "10.10.0.0/24"
    assert blast["object"]["gateway"] == "LAB_GW"
    assert blast["irreversibility"] == "permanent"

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
    assert resume.result["verified"] is True
    assert recorder.playback_ran


@pytest.mark.asyncio
async def test_full_governed_flow_gateway_delete_unreferenced() -> None:
    recorder = _RecordingPfSense(
        config_before=_CONFIG_ROUTING, config_after=_CONFIG_ROUTING_NO_RETIRING_GW
    )
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _GATEWAY_OP,
        "target": "pf-perimeter",
        "params": {"name": "RETIRING_GW"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["object"]["kind"] == "gateway"
    assert blast["object"]["name"] == "RETIRING_GW"
    assert blast["reference_count"] == 0
    assert blast["children"] == []

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
    assert resume.result["verified"] is True


@pytest.mark.asyncio
async def test_referenced_gateway_refused_post_approval_fail_closed() -> None:
    """A referenced gateway parks (blast radius names the referrers) then is refused
    at execution (post-approval), fail-closed -- nothing mutates."""
    recorder = _RecordingPfSense(config_before=_CONFIG_ROUTING)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _GATEWAY_OP,
        "target": "pf-perimeter",
        "params": {"name": "LAB_GW"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["reference_count"] == 3

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "referenced"
    assert resume.result["reference_count"] == 3
    assert not recorder.playback_ran


@pytest.mark.asyncio
async def test_full_governed_flow_member_remove_keeps_shared_alias() -> None:
    """Full dispatch of a shared-alias member removal: the alias + other members survive."""
    recorder = _RecordingPfSense(
        config_before=_CONFIG_SHARED_ALIAS, config_after=_CONFIG_SHARED_ALIAS_AFTER
    )
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _MEMBER_OP,
        "target": "pf-perimeter",
        "params": {"name": "SHARED_HOSTS", "value": "198.51.100.5"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["object"]["kind"] == "alias_member"
    assert blast["object"]["alias"] == "SHARED_HOSTS"
    assert blast["object"]["member"] == "198.51.100.5"
    assert blast["residual_member_count"] == 2
    assert blast["alias_retained"] is True

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "removed"
    assert resume.result["alias_retained"] is True
    assert resume.result["residual_members"] == ["192.0.2.10", "192.0.2.11"]


@pytest.mark.asyncio
async def test_park_refused_when_member_would_be_last() -> None:
    """A last-member removal never resolves a blast radius -> park refused (fail-closed)."""
    recorder = _RecordingPfSense(config_before=_CONFIG_SHARED_ALIAS)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _MEMBER_OP,
        "target": "pf-perimeter",
        "params": {"name": "SOLO", "value": "203.0.113.9"},
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert await _pending_count() == 0
    assert not recorder.playback_ran


@pytest.mark.asyncio
async def test_agent_principal_is_denied() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_ROUTING)
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_GATEWAY_OP,
        target=_FakePfsenseTarget(),
        params={"name": "RETIRING_GW"},
    )
    assert result.status == "denied", result
    assert not recorder.playback_ran
    assert await _pending_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_ROUTE_OP, _GATEWAY_OP, _MEMBER_OP])
async def test_service_grant_refuses_teardown_ops(op_id: str) -> None:
    """All three fold into the destructive-tier grant refusal -- including
    ``alias.member.remove``, whose op-id does not end in ``.delete`` (it is
    refused via its ``destructive`` safety level / tag, not the glob)."""
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_ROUTING))
    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub="svc-runner",
        op_id=op_id,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended teardown",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, "creator", payload)
    msg = str(exc.value).lower()
    assert "delete-shaped" in msg or "destructive" in msg
