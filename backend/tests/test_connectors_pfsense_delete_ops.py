# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense governed destructive-delete ops (#3232).

Covers the connector's first two ``safety_level="destructive"`` typed ops --
``pfsense.nat.delete`` and ``pfsense.alias.delete`` -- across three layers:

* **Unit** (mocked ``_run_command``): parsers, the fail-closed reference
  scan, the delete-one-by-identity playback fragments, input validation, and
  the handler happy / not-found / ambiguous / referenced / verify-failure
  paths.
* **Metadata**: registration classification (destructive + requires_approval
  + ``destructive`` tag + group), response-schema conformance, and proof
  the ops fold into the **existing single-source** delete-shaped classifier
  (the default ``*.delete`` glob *and* the ``destructive`` tag) with **no**
  new pattern list declared.
* **Governed-flow conformance** (full ``call_operation`` dispatch against a
  seeded recording connector): no agent path, no standing grant, no
  self-approval even under break-glass, no satellite mint, dispatch refused
  without a preview hash, park refused without a blast-radius block, and the
  full preview -> park (hash + blast radius) -> distinct-human approve ->
  audited resume delete. Plus the post-approval fail-closed re-checks
  (referenced alias) and the #3197 param-sensitive composite hash.

All config.xml fixtures are synthetic (RFC 5737 / RFC 1918 addresses, no lab
hostnames / IPs / VLANs / real rule contents) -- the public repo must never
carry lab values.
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
    _build_alias_delete_playback,
    _build_nat_delete_playback,
    find_alias_references,
    parse_aliases_xml,
    parse_nat_port_forwards_xml,
)
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    _delete_shaped_reason_by_pattern,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "pfsense-ssh-2.7"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003232")
_NAT_OP = "pfsense.nat.delete"
_ALIAS_OP = "pfsense.alias.delete"
_TRACKER = "1585165239"


# ---------------------------------------------------------------------------
# Synthetic config.xml fixtures (RFC 5737 / RFC 1918 -- no lab values)
# ---------------------------------------------------------------------------

# Two port-forward NAT rules; rule 1 carries an associated filter rule.
_CONFIG_TWO_NAT = (
    "<pfsense><nat>"
    "<rule><tracker>1585165239</tracker><interface>wan</interface><protocol>tcp</protocol>"
    "<target>192.0.2.10</target><local-port>443</local-port>"
    "<destination><network>wanip</network><port>443</port></destination>"
    "<associated-rule-id>nat_abc123</associated-rule-id><descr>web fwd</descr></rule>"
    "<rule><tracker>1585165240</tracker><interface>wan</interface><protocol>udp</protocol>"
    "<target>192.0.2.20</target><local-port>1194</local-port>"
    "<destination><any></any></destination><descr>vpn fwd</descr></rule>"
    "</nat></pfsense>"
)
# After deleting tracker 1585165239 (one rule remains).
_CONFIG_ONE_NAT = (
    "<pfsense><nat>"
    "<rule><tracker>1585165240</tracker><interface>wan</interface><protocol>udp</protocol>"
    "<target>192.0.2.20</target><local-port>1194</local-port>"
    "<destination><any></any></destination><descr>vpn fwd</descr></rule>"
    "</nat></pfsense>"
)
# A config where a corrupt duplicate tracker appears twice.
_CONFIG_DUP_TRACKER = (
    "<pfsense><nat>"
    "<rule><tracker>1585165239</tracker><interface>wan</interface><protocol>tcp</protocol>"
    "<target>192.0.2.10</target><descr>a</descr></rule>"
    "<rule><tracker>1585165239</tracker><interface>wan</interface><protocol>tcp</protocol>"
    "<target>192.0.2.11</target><descr>b</descr></rule>"
    "</nat></pfsense>"
)

# Aliases: ORPHAN (unreferenced) + WEB_SERVERS (referenced by a filter rule
# and by the NESTED alias) + HTTPS_PORTS (referenced by the same filter rule).
_CONFIG_ALIASES = (
    "<pfsense>"
    "<aliases>"
    "<alias><name>WEB_SERVERS</name><type>host</type>"
    "<address>192.0.2.10 192.0.2.11</address><descr>web</descr></alias>"
    "<alias><name>HTTPS_PORTS</name><type>port</type><address>443</address></alias>"
    "<alias><name>ORPHAN</name><type>host</type><address>198.51.100.5</address></alias>"
    "<alias><name>NESTED</name><type>host</type>"
    "<address>WEB_SERVERS 203.0.113.1</address></alias>"
    "</aliases>"
    "<filter>"
    "<rule><tracker>1600000001</tracker><descr>allow web</descr>"
    "<source><any></any></source>"
    "<destination><address>WEB_SERVERS</address><port>HTTPS_PORTS</port></destination></rule>"
    "</filter>"
    "</pfsense>"
)
# After deleting ORPHAN.
_CONFIG_ALIASES_NO_ORPHAN = _CONFIG_ALIASES.replace(
    "<alias><name>ORPHAN</name><type>host</type><address>198.51.100.5</address></alias>", ""
)


# ---------------------------------------------------------------------------
# Environment fixture
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
# Part A -- unit: parsers
# ===========================================================================


def test_parse_nat_port_forwards_extracts_identity_and_summary() -> None:
    rows = parse_nat_port_forwards_xml(_CONFIG_TWO_NAT)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["tracker"] == "1585165239"
    assert r0["associated_rule_id"] == "nat_abc123"
    assert r0["interface"] == "wan"
    assert r0["protocol"] == "tcp"
    assert r0["destination"] == "wanip:443"
    assert r0["target"] == "192.0.2.10"
    assert r0["local_port"] == "443"
    assert r0["descr"] == "web fwd"
    assert r0["position"] == 1
    assert rows[1]["destination"] == "any"  # <any/> flattens to "any"


def test_parse_nat_port_forwards_empty_and_malformed() -> None:
    assert parse_nat_port_forwards_xml("") == []
    assert parse_nat_port_forwards_xml("<broken") == []
    assert parse_nat_port_forwards_xml("<pfsense></pfsense>") == []


def test_parse_aliases_extracts_rows() -> None:
    rows = parse_aliases_xml(_CONFIG_ALIASES)
    names = [a["name"] for a in rows]
    assert names == ["WEB_SERVERS", "HTTPS_PORTS", "ORPHAN", "NESTED"]
    web = rows[0]
    assert web["type"] == "host"
    assert web["address"] == "192.0.2.10 192.0.2.11"


def test_parse_aliases_empty_and_malformed() -> None:
    assert parse_aliases_xml("") == []
    assert parse_aliases_xml("<broken") == []
    assert parse_aliases_xml("<pfsense></pfsense>") == []


# ===========================================================================
# Part A -- unit: fail-closed reference scan
# ===========================================================================


def test_find_alias_references_nested_alias_and_filter_rule() -> None:
    refs = find_alias_references(_CONFIG_ALIASES, "WEB_SERVERS")
    kinds = {(r["kind"], r["id"]) for r in refs}
    assert ("alias", "NESTED") in kinds  # nested-alias reference
    assert ("filter_rule", "1600000001") in kinds  # filter-rule reference


def test_find_alias_references_port_alias_in_filter_rule() -> None:
    refs = find_alias_references(_CONFIG_ALIASES, "HTTPS_PORTS")
    assert [(r["kind"], r["id"]) for r in refs] == [("filter_rule", "1600000001")]


def test_find_alias_references_unreferenced_is_empty() -> None:
    assert find_alias_references(_CONFIG_ALIASES, "ORPHAN") == []


def test_find_alias_references_nat_port_forward_and_outbound_and_onetoone() -> None:
    cfg = (
        "<pfsense><aliases>"
        "<alias><name>TARGETS</name><type>host</type><address>192.0.2.5</address></alias>"
        "<alias><name>OUTS</name><type>host</type><address>10.0.0.0/24</address></alias>"
        "<alias><name>ONES</name><type>host</type><address>203.0.113.9</address></alias>"
        "</aliases><nat>"
        "<rule><tracker>1</tracker><target>TARGETS</target>"
        "<destination><any></any></destination><descr>pf</descr></rule>"
        "<outbound><rule><descr>ob</descr>"
        "<source><network>OUTS</network></source></rule></outbound>"
        "<onetoone><descr>oto</descr>"
        "<source><address>ONES</address></source></onetoone>"
        "</nat></pfsense>"
    )
    assert [r["kind"] for r in find_alias_references(cfg, "TARGETS")] == ["nat_rule"]
    assert [r["kind"] for r in find_alias_references(cfg, "OUTS")] == ["nat_outbound_rule"]
    assert [r["kind"] for r in find_alias_references(cfg, "ONES")] == ["nat_onetoone_rule"]


# ===========================================================================
# Part A -- unit: playback fragments delete exactly one, safely
# ===========================================================================


def test_nat_delete_playback_is_single_object_and_safe() -> None:
    frag = _build_nat_delete_playback("1585165239")
    assert "$meho_tracker = '1585165239';" in frag
    # Persists only on a clean single removal.
    assert "if ($meho_removed === 1) {" in frag
    assert "write_config('meho: delete nat rule tracker 1585165239');" in frag
    assert "filter_configure();" in frag
    # A playback fragment carries no <?php tag and no trailing exec.
    assert "<?php" not in frag
    assert "\nexec" not in frag


def test_alias_delete_playback_is_single_object_and_safe() -> None:
    frag = _build_alias_delete_playback("ORPHAN")
    assert "$meho_name = 'ORPHAN';" in frag
    assert "if ($meho_removed === 1) {" in frag
    assert "write_config('meho: delete alias ORPHAN');" in frag
    assert "filter_configure();" in frag
    assert "<?php" not in frag


# ===========================================================================
# Part A -- unit: input validation rejects before any SSH round-trip
# ===========================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"tracker": "not-a-number"},
        {"tracker": "1585165239; rm -rf /"},
        {"tracker": "1' OR '1"},
        {},  # missing tracker
    ],
)
async def test_nat_delete_rejects_bad_tracker_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.nat_delete(None, params)
    assert mock_cmd.await_count == 0


@pytest.mark.parametrize(
    "params",
    [
        {"name": "bad name"},
        {"name": "web'; write_config("},
        {"name": "web;rm"},
        {},  # missing name
    ],
)
async def test_alias_delete_rejects_bad_name_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.alias_delete(None, params)
    assert mock_cmd.await_count == 0


# ===========================================================================
# Part A -- unit: nat.delete handler paths
# ===========================================================================


async def test_nat_delete_happy_path_deletes_one_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_TWO_NAT),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm cleanup
            _proc(_CONFIG_ONE_NAT),  # read-back verify
        ]
        result = await connector.nat_delete(None, {"tracker": _TRACKER})

    assert result["status"] == "deleted"
    assert result["verified"] is True
    assert result["matched"] == 1
    assert result["rules_before"] == 2
    assert result["rules_after"] == 1
    assert result["removed"]["tracker"] == _TRACKER
    cmds = _cmds(mock_cmd)
    assert cmds[0] == "cat /cf/conf/config.xml"
    assert cmds[2] == "pfSsh.php playback meho_nat_delete_1585165239"
    body = cmds[1]
    assert "$meho_tracker = '1585165239';" in body


async def test_nat_delete_not_found_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_TWO_NAT)]
        result = await connector.nat_delete(None, {"tracker": "9999999999"})
    assert result["status"] == "not_found"
    assert result["matched"] == 0
    assert result["removed"] is None
    # Only the guard read ran -- no staging, playback, or cleanup.
    assert mock_cmd.await_count == 1


async def test_nat_delete_ambiguous_refuses_fail_closed() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_DUP_TRACKER)]
        result = await connector.nat_delete(None, {"tracker": _TRACKER})
    assert result["status"] == "ambiguous"
    assert result["matched"] == 2
    assert result["removed"] is None
    # Duplicate trackers -> never delete either. Guard read only.
    assert mock_cmd.await_count == 1


async def test_nat_delete_verification_failure_raises() -> None:
    """A playback that left the rule present (or removed too many) raises."""
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_TWO_NAT),  # guard read (2 rules)
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_TWO_NAT),  # read-back: rule still present (did not persist)
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.nat_delete(None, {"tracker": _TRACKER})


async def test_nat_delete_bulk_over_deletion_is_caught_by_count_check() -> None:
    """A playback that removed MORE than the one rule is caught (adversarial risk #1).

    Read-back shows an empty NAT set (both rules gone) when only one was
    approved. ``rules_after (0) != rules_before (2) - 1`` -> the count guard
    raises rather than reporting a false success. This is the load-bearing
    proof that a bulk-deletion bug on the config array cannot pass verification.
    """
    empty_nat = "<pfsense><nat></nat></pfsense>"
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_TWO_NAT),  # guard read (2 rules)
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(empty_nat),  # read-back: BOTH rules gone (bulk over-deletion)
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.nat_delete(None, {"tracker": _TRACKER})


# ===========================================================================
# Part A -- unit: alias.delete handler paths
# ===========================================================================


async def test_alias_delete_happy_path_deletes_one_and_verifies() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ALIASES),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_ALIASES_NO_ORPHAN),  # read-back verify
        ]
        result = await connector.alias_delete(None, {"name": "ORPHAN"})
    assert result["status"] == "deleted"
    assert result["verified"] is True
    assert result["matched"] == 1
    assert result["reference_count"] == 0
    assert result["aliases_before"] == 4
    assert result["aliases_after"] == 3
    assert result["removed"]["name"] == "ORPHAN"


async def test_alias_delete_referenced_refuses_and_names_referrers() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_ALIASES)]
        result = await connector.alias_delete(None, {"name": "WEB_SERVERS"})
    assert result["status"] == "referenced"
    assert result["reference_count"] == 2
    ref_ids = {(r["kind"], r["id"]) for r in result["references"]}
    assert ("alias", "NESTED") in ref_ids
    assert ("filter_rule", "1600000001") in ref_ids
    assert "still referenced" in result["guidance"]
    # Fail-closed: refuses after a single guard read, stages nothing.
    assert mock_cmd.await_count == 1


async def test_alias_delete_not_found_stages_nothing() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_CONFIG_ALIASES)]
        result = await connector.alias_delete(None, {"name": "NOPE"})
    assert result["status"] == "not_found"
    assert result["matched"] == 0
    assert mock_cmd.await_count == 1


async def test_alias_delete_verification_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_CONFIG_ALIASES),  # guard read
            _proc("", 0),  # stage
            _proc("", 0),  # playback
            _proc("", 0),  # rm
            _proc(_CONFIG_ALIASES),  # read-back: alias still present
        ]
        with pytest.raises(RuntimeError, match="verification failed"):
            await connector.alias_delete(None, {"name": "ORPHAN"})


# ===========================================================================
# Part B -- registration classification + schema conformance
# ===========================================================================


def test_delete_ops_are_destructive_requires_approval_with_tag() -> None:
    delete_ids = {_NAT_OP, _ALIAS_OP}
    seen = set()
    for op in PFSENSE_OPS:
        if op.op_id not in delete_ids:
            continue
        seen.add(op.op_id)
        assert op.safety_level == "destructive", op.op_id
        assert op.requires_approval is True, op.op_id
        assert "destructive" in op.tags, op.op_id
        assert "delete" in op.tags, op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id
        assert op.llm_instructions and op.llm_instructions.get("when_to_use"), op.op_id
    assert seen == delete_ids


def test_pfsense_op_count_is_seventeen() -> None:
    assert len(PFSENSE_OPS) == 17


def test_delete_ops_group_keys() -> None:
    by_id = {op.op_id: op for op in PFSENSE_OPS}
    assert by_id[_NAT_OP].group_key == "nat"
    assert by_id[_ALIAS_OP].group_key == "alias"


def test_nat_delete_response_schema_accepts_outcomes() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == _NAT_OP)
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "delete",
            "resource": "nat_rule",
            "status": "deleted",
            "tracker": _TRACKER,
            "matched": 1,
            "removed": {"tracker": _TRACKER},
            "rules_before": 2,
            "rules_after": 1,
            "verified": True,
            "guidance": None,
        }
    )
    validator.validate(
        {
            "op_class": "delete",
            "resource": "nat_rule",
            "status": "not_found",
            "tracker": _TRACKER,
            "matched": 0,
            "removed": None,
            "rules_before": 2,
            "rules_after": None,
            "verified": False,
            "guidance": "no rule",
        }
    )


def test_alias_delete_response_schema_accepts_outcomes() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == _ALIAS_OP)
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    validator.validate(
        {
            "op_class": "delete",
            "resource": "alias",
            "status": "referenced",
            "name": "WEB_SERVERS",
            "matched": 1,
            "removed": None,
            "references": [{"kind": "filter_rule", "id": "1600000001", "descr": "allow web"}],
            "reference_count": 1,
            "aliases_before": 4,
            "aliases_after": None,
            "verified": False,
            "guidance": "referenced",
        }
    )


# ===========================================================================
# Part B -- classifier fold via the EXISTING single source (no new patterns)
# ===========================================================================


def test_delete_ops_fold_into_default_delete_shaped_pattern() -> None:
    """Both op-ids match the shipped default ``*.delete`` glob -- no new list."""
    patterns = get_settings().service_grant_delete_shaped_patterns
    assert "*.delete" in patterns  # the single source, not re-declared here
    assert _delete_shaped_reason_by_pattern(_NAT_OP, patterns) is not None
    assert _delete_shaped_reason_by_pattern(_ALIAS_OP, patterns) is not None


@pytest.mark.asyncio
async def test_delete_ops_fold_via_destructive_tag_even_without_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ``*.delete`` glob overridden away (via the single-source env
    var), the ``destructive`` tag still routes the grant refusal -- proving the
    descriptor-tag single source (``_delete_shaped_reason_by_descriptor``) folds
    the ops independently, with no new pattern list declared here."""
    # Override the single-source pattern set to one that does NOT match the
    # pfSense delete op-ids (the shipped SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    # override seam), so only the descriptor-tag path can refuse.
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "DELETE:*")
    get_settings.cache_clear()
    patterns = get_settings().service_grant_delete_shaped_patterns
    assert patterns == ("DELETE:*",)
    assert _delete_shaped_reason_by_pattern(_NAT_OP, patterns) is None

    # But a resolved descriptor carrying the destructive tag still refuses.
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_TWO_NAT))
    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub="svc-runner",
        op_id=_NAT_OP,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended teardown",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, "creator", payload)
    assert "destructive" in str(exc.value).lower() or "delete-shaped" in str(exc.value).lower()


# ===========================================================================
# Part C -- governed-flow conformance (full dispatch)
# ===========================================================================


def _make_operator(
    *, sub: str = "op-pf", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="pfSense Delete Conformance",
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
    blast-radius preview builder) drive real dispatch without SSH. Models the
    delete: ``cat config.xml`` returns ``config_before`` until a ``pfSsh.php
    playback`` command runs, then ``config_after`` -- so the handler's
    read-back verification is exercised end to end.
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
                notes="seeded by test_connectors_pfsense_delete_ops",
            )
        )
        await s.commit()
    return target_id


async def _bootstrap(recorder: _RecordingPfSense) -> None:
    # The package import registers the connector; re-register only if a
    # prior test cleared the v2 registry (register_connector_v2 raises on a
    # duplicate three-tuple key).
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
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_TWO_NAT))
    async with get_sessionmaker()() as s:
        for op_id in (_NAT_OP, _ALIAS_OP):
            row = (
                await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
            ).scalar_one()
            assert row.safety_level == "destructive", op_id
            assert row.requires_approval is True, op_id
            assert row.source_kind == "typed", op_id


@pytest.mark.asyncio
async def test_full_governed_flow_nat_delete() -> None:
    """preview -> park (hash + blast radius) -> distinct human approve -> resume delete."""
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT, config_after=_CONFIG_ONE_NAT)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _NAT_OP,
        "target": "pf-perimeter",
        "params": {"tracker": _TRACKER},
    }

    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    assert isinstance(bound_hash, str) and len(bound_hash) == 64

    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert not recorder.playback_ran  # nothing mutated pre-approval

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"]["kind"] == "nat_rule"
    assert blast["object"]["tracker"] == _TRACKER
    assert blast["object"]["destination"] == "wanip:443"
    assert blast["irreversibility"] == "permanent"
    # The associated filter rule is enumerated (left in place, surfaced).
    assert {c["kind"] for c in blast["children"]} == {"associated_filter_rule"}

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert resume.result["verified"] is True
    assert resume.result["rules_before"] == 2
    assert resume.result["rules_after"] == 1
    assert recorder.playback_ran


@pytest.mark.asyncio
async def test_rule_recreated_between_park_and_approval_invalidates() -> None:
    """A rule deleted-and-recreated between park and approval MUST invalidate (risk #5).

    Identity is the ``tracker`` (carried in the hash-bound params). We park a
    delete of tracker T (present at park, so the blast radius builds), then
    simulate the rule being deleted-and-recreated with a NEW tracker before
    approval by swapping the live config to one that no longer carries T. At
    resume the identity re-match finds T absent -> ``not_found``; the recreated
    rule (a different identity) is untouched and no playback runs.
    """
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT, config_after=_CONFIG_ONE_NAT)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _NAT_OP,
        "target": "pf-perimeter",
        "params": {"tracker": _TRACKER},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    # Between park and approval the rule with tracker T is gone (recreated with
    # a different tracker) -- the live config no longer carries T.
    recorder._config_before = _CONFIG_ONE_NAT  # only tracker 1585165240 remains

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "not_found"
    assert resume.result["matched"] == 0
    assert not recorder.playback_ran  # the stale/recreated rule is never touched


@pytest.mark.asyncio
async def test_full_governed_flow_alias_delete() -> None:
    recorder = _RecordingPfSense(
        config_before=_CONFIG_ALIASES, config_after=_CONFIG_ALIASES_NO_ORPHAN
    )
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _ALIAS_OP,
        "target": "pf-perimeter",
        "params": {"name": "ORPHAN"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["object"]["kind"] == "alias"
    assert blast["object"]["name"] == "ORPHAN"
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
async def test_referenced_alias_refused_post_approval_fail_closed() -> None:
    """A referenced alias is refused at execution (post-approval); nothing mutates."""
    recorder = _RecordingPfSense(config_before=_CONFIG_ALIASES)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _ALIAS_OP,
        "target": "pf-perimeter",
        "params": {"name": "WEB_SERVERS"},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    # The blast radius names the references so the approver sees the risk.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["reference_count"] == 2

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    # Fail-closed at execution: refuses, deletes nothing.
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "referenced"
    assert resume.result["reference_count"] == 2
    assert not recorder.playback_ran


@pytest.mark.asyncio
async def test_agent_principal_is_denied() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT)
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_NAT_OP,
        target=_FakePfsenseTarget(),
        params={"tracker": _TRACKER},
    )
    assert result.status == "denied", result
    assert not recorder.playback_ran  # never executed
    assert await _pending_count() == 0  # never parked


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_NAT_OP, _ALIAS_OP])
async def test_service_grant_refuses_delete_ops(op_id: str) -> None:
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_TWO_NAT))
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


@pytest.mark.asyncio
async def test_no_self_approval_even_under_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT, config_after=_CONFIG_ONE_NAT)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="solo-operator")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _NAT_OP,
        "target": "pf-perimeter",
        "params": {"tracker": _TRACKER},
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, request_id, operator=requester, params=None)


@pytest.mark.asyncio
async def test_satellite_mint_refuses_op_not_safe() -> None:
    await _bootstrap(_RecordingPfSense(config_before=_CONFIG_TWO_NAT))
    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_NAT_OP,
            target=None,
            params={"tracker": _TRACKER},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE


@pytest.mark.asyncio
async def test_dispatch_refused_without_preview_hash() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT)
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_NAT_OP,
        target=_FakePfsenseTarget(),
        params={"tracker": _TRACKER},
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert not recorder.playback_ran
    assert await _pending_count() == 0


@pytest.mark.asyncio
async def test_park_refused_without_blast_radius() -> None:
    """A tracker matching no rule -> builder declines -> no blast radius -> refused."""
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _NAT_OP,
        "target": "pf-perimeter",
        "params": {"tracker": "9999999999"},  # present in neither rule
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert await _pending_count() == 0
    assert not recorder.playback_ran


@pytest.mark.asyncio
async def test_composite_preview_hash_is_param_sensitive() -> None:
    recorder = _RecordingPfSense(config_before=_CONFIG_TWO_NAT)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    base = {"connector_id": _CONNECTOR_ID, "op_id": _NAT_OP, "target": "pf-perimeter"}
    p1 = await preview_operation(requester, {**base, "params": {"tracker": "1585165239"}})
    p1b = await preview_operation(requester, {**base, "params": {"tracker": "1585165239"}})
    p2 = await preview_operation(requester, {**base, "params": {"tracker": "1585165240"}})
    assert p1["status"] == "ok"
    assert p1["preview_hash"] == p1b["preview_hash"]  # stable for identical args
    assert p1["preview_hash"] != p2["preview_hash"]  # a different delete -> different hash
