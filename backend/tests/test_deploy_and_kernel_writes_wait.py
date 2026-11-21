# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed writes that choose what a system runs later, or who gets rights, wait for a person.

These calls get the rule of a credential call: a person approves each call,
and no standing grant, no agent grant and no run approval covers it.

* ``argocd.app.set``, ``argocd.appproject.create`` and
  ``argocd.appproject.update``: every call.
* ``argocd.app.sync`` when it names a revision. A sync without one keeps the
  rules of the operation, and so does ``argocd.app.rollback``.
* ``linux.sysctl.write`` of a key or a value that can make the kernel run a
  program or write a file. A plain tuning key with a number keeps the rules
  of the operation.

Each call waits for a person's own call, an agent login and a service
login, with and without a standing grant, and as a step of an approved
multi-step run. The approval card names the revision, and the key and the
value. A sensor never runs such a call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.agents.grant_schemas import AgentGrantCreate, GrantPrincipalKind
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.service import SensorRequiresNoPersonError, _require_read
from meho_backplane.connectors.argocd.op_classes import sync_sets_a_revision
from meho_backplane.connectors.linux import ops_write
from meho_backplane.connectors.linux.op_classes import sysctl_write_can_run_a_program
from meho_backplane.connectors.linux.ops_host import (
    _SYSCTL_READ_OP,
    linux_sysctl_read,
    validate_sysctl_key,
)
from meho_backplane.connectors.linux.ops_write import _SYSCTL_WRITE_OP, linux_sysctl_write
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.op_classes import call_always_waits, is_credential_capable
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import approved_run_var, enforce_subop_policy
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    credential_grant_reason,
)
from meho_backplane.operations.wait_when_set import raise_descriptor_for_params
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import (
    _AGENT,
    _SERVICE,
    _TENANT,
    _agent,
    _name_in_every_grant,
    _register_all,
    _service,
)

_RUN = "run-credential-calls"

_ALWAYS = ("argocd.app.set", "argocd.appproject.create", "argocd.appproject.update")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _person() -> Operator:
    return Operator(
        sub="op:caller",
        name="Caller",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


_LOGINS = [_person(), _agent(), _service()]

_PROJECT = {"metadata": {"name": "team-a"}, "spec": {"sourceRepos": ["*"]}}
_APP_SPEC = {"project": "team-a", "source": {"repoURL": "https://git.example.test/app.git"}}

#: Params of each call that waits, with an id for the test name.
_WAITING_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("argocd.app.set", {"name": "web", "spec": _APP_SPEC}),
    ("argocd.appproject.create", {"project": _PROJECT}),
    ("argocd.appproject.update", {"project": _PROJECT}),
    ("argocd.app.sync", {"name": "web", "revision": "feature-branch"}),
    ("linux.sysctl.write", {"key": "kernel.modprobe", "value": "x"}),
    ("linux.sysctl.write", {"key": "kernel.core_pattern", "value": "core"}),
    ("linux.sysctl.write", {"key": "kernel.hotplug", "value": "x"}),
    ("linux.sysctl.write", {"key": "kernel.poweroff_cmd", "value": "x"}),
    ("linux.sysctl.write", {"key": "fs.binfmt_misc.register", "value": "x"}),
    ("linux.sysctl.write", {"key": "net.core.somaxconn", "value": "a/b"}),
]
_WAITING_IDS = [
    "app-set",
    "appproject-create",
    "appproject-update",
    "sync-with-revision",
    "sysctl-modprobe",
    "sysctl-core-pattern",
    "sysctl-hotplug",
    "sysctl-poweroff-cmd",
    "sysctl-binfmt-misc",
    "sysctl-value-with-a-path",
]

#: Params of calls that keep the rules of their operation.
_PLAIN_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("argocd.app.sync", {"name": "web"}),
    ("argocd.app.rollback", {"name": "web", "id": 3}),
    ("linux.sysctl.write", {"key": "vm.swappiness", "value": "10"}),
]
_PLAIN_IDS = ["sync-without-revision", "rollback", "sysctl-plain-tuning"]


# ---------------------------------------------------------------------------
# Which calls wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _ALWAYS)
def test_every_call_of_an_app_spec_or_project_write_waits(op_id: str) -> None:
    assert is_credential_capable(op_id) is True
    assert credential_grant_reason(op_id) is not None
    assert call_always_waits(op_id, {}) is True


@pytest.mark.parametrize(
    ("params", "waits"),
    [
        ({"name": "web", "revision": "abc123"}, True),
        ({"name": "web"}, False),
        ({"name": "web", "revision": None}, False),
        ({"name": "web", "revision": ""}, False),
    ],
)
def test_a_sync_waits_only_when_it_names_a_revision(params: dict[str, Any], waits: bool) -> None:
    assert sync_sets_a_revision(params) is waits
    assert call_always_waits("argocd.app.sync", params) is waits


@pytest.mark.parametrize(
    "key",
    [
        "kernel.modprobe",
        "kernel/modprobe",
        "KERNEL.MODPROBE",
        "kernel.core_pattern",
        "kernel.hotplug",
        "kernel.poweroff_cmd",
        "fs.binfmt_misc.register",
        "fs/binfmt_misc/status",
        "fs.binfmt_misc",
        "kernel..modprobe",
        "kernel/./modprobe",
        "kernel/../kernel/modprobe",
        "/kernel/modprobe",
        "kernel.modprobe.",
    ],
)
def test_a_key_that_names_a_program_or_a_file_waits(key: str) -> None:
    params = {"key": key, "value": "1"}
    assert sysctl_write_can_run_a_program(params) is True
    assert call_always_waits("linux.sysctl.write", params) is True


_OPTION_KEYS = ["--load", "-p", "--system", "-a", "-"]

#: The ``key`` schema of ``linux.sysctl.read`` and ``linux.sysctl.write`` as
#: an agent reads it. The refusal of a key that starts with ``-`` lives in
#: ``validate_sysctl_key``, so this schema stays the same.
_PUBLISHED_KEY_SCHEMA_PATTERN = r"^[A-Za-z0-9._/-]+$"


@pytest.mark.parametrize("op", [_SYSCTL_READ_OP, _SYSCTL_WRITE_OP], ids=lambda op: op.op_id)
def test_the_published_key_schema_stays_the_same(op: Any) -> None:
    key_schema = op.parameter_schema["properties"]["key"]
    assert key_schema["type"] == "string"
    assert key_schema["minLength"] == 1
    assert key_schema["pattern"] == _PUBLISHED_KEY_SCHEMA_PATTERN


@pytest.mark.parametrize("key", _OPTION_KEYS)
def test_a_key_that_sysctl_reads_as_an_option_waits_and_is_refused(key: str) -> None:
    params = {"key": key, "value": "settings.conf"}
    assert sysctl_write_can_run_a_program(params) is True
    assert call_always_waits("linux.sysctl.write", params) is True
    with pytest.raises(ValueError, match="starts with '-'"):
        validate_sysctl_key(key)


class _NoCommandConnector:
    """A connector that fails the test when a handler runs a command."""

    async def _run_command(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the handler ran a command")


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _OPTION_KEYS)
async def test_the_read_handler_refuses_an_option_key_before_any_command(key: str) -> None:
    with pytest.raises(ValueError, match="starts with '-'"):
        await linux_sysctl_read(_NoCommandConnector(), SimpleNamespace(), {"key": key})  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _OPTION_KEYS)
async def test_the_write_handler_refuses_an_option_key_before_any_command(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the handler went past the key check")

    monkeypatch.setattr(ops_write, "_resolve_sudo_password", _no_call)
    monkeypatch.setattr(ops_write, "run_remote_bash_with_sudo", _no_call)
    with pytest.raises(ValueError, match="starts with '-'"):
        await linux_sysctl_write(
            _NoCommandConnector(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            {"key": key, "value": "1"},
        )


def test_a_key_with_a_dash_inside_is_still_a_key() -> None:
    assert validate_sysctl_key("net.ipv4.conf.br-lan.forwarding") == (
        "net.ipv4.conf.br-lan.forwarding"
    )
    assert (
        call_always_waits(
            "linux.sysctl.write", {"key": "net.ipv4.conf.br-lan.forwarding", "value": "1"}
        )
        is False
    )


def test_a_value_with_a_path_waits() -> None:
    assert sysctl_write_can_run_a_program({"key": "vm.swappiness", "value": "/x"}) is True


@pytest.mark.parametrize(
    "params", [{"key": None, "value": "1"}, {"key": "vm.swappiness", "value": 1}, {}]
)
def test_a_sysctl_call_meho_cannot_read_waits(params: dict[str, Any]) -> None:
    assert call_always_waits("linux.sysctl.write", params) is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("vm.swappiness", "10"),
        ("net.core.somaxconn", "4096"),
        ("net.ipv4.ip_local_port_range", "32768 60999"),
        ("net/ipv4/conf/eth0.100/forwarding", "1"),
        ("kernel.pid_max", "4194304"),
    ],
)
def test_a_plain_tuning_key_keeps_its_rules(key: str, value: str) -> None:
    assert call_always_waits("linux.sysctl.write", {"key": key, "value": value}) is False


# ---------------------------------------------------------------------------
# The call waits for every login, with and without a standing grant
# ---------------------------------------------------------------------------


async def _verdict(
    operator: Operator, row: EndpointDescriptor, params: dict[str, Any]
) -> tuple[PermissionVerdict, str]:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_params(descriptor, params)
    token = work_ref_var.set(_RUN)
    try:
        verdict, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            params=params,
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return verdict, reason or ""


def _row(rows: list[EndpointDescriptor], op_id: str) -> EndpointDescriptor:
    return next(row for row in rows if row.op_id == op_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _WAITING_CALLS, ids=_WAITING_IDS)
async def test_the_call_waits_for_every_login_with_and_without_a_grant(
    op_id: str, params: dict[str, Any]
) -> None:
    rows = await _register_all()
    row = _row(rows, op_id)
    for operator in _LOGINS:
        verdict, reason = await _verdict(operator, row, params)
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (operator.principal_kind, reason)
    await _name_in_every_grant([row])
    for operator in _LOGINS:
        verdict, reason = await _verdict(operator, row, params)
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (operator.principal_kind, reason)
        assert "auto-granted" not in reason, reason
        assert "by run approval" not in reason, reason


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _WAITING_CALLS, ids=_WAITING_IDS)
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
async def test_a_step_of_an_approved_run_waits_under_a_grant(
    op_id: str, params: dict[str, Any], operator: Operator
) -> None:
    rows = await _register_all()
    row = _row(rows, op_id)
    await _name_in_every_grant([row])
    approved = approved_run_var.set(True)
    token = work_ref_var.set(_RUN)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id=connector_id_of(row),
            op_id=op_id,
            safety_level=row.safety_level,
            requires_approval=False,
            target=None,
            params=params,
        )
    finally:
        work_ref_var.reset(token)
        approved_run_var.reset(approved)
    assert result is not None
    assert result.status == "awaiting_approval", result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _PLAIN_CALLS, ids=_PLAIN_IDS)
async def test_a_plain_call_keeps_its_grant(op_id: str, params: dict[str, Any]) -> None:
    rows = await _register_all()
    row = _row(rows, op_id)
    verdict, reason = await _verdict(_service(), row, params)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    await _name_in_every_grant([row])
    verdict, reason = await _verdict(_service(), row, params)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


@pytest.mark.asyncio
async def test_a_plain_sync_and_a_sysctl_grant_cover_the_plain_call_only() -> None:
    rows = await _register_all()
    calls = (
        ("argocd.app.sync", {"name": "web"}, {"name": "web", "revision": "v2"}),
        (
            "linux.sysctl.write",
            {"key": "vm.swappiness", "value": "10"},
            {"key": "kernel.modprobe", "value": "x"},
        ),
    )
    await _name_in_every_grant([_row(rows, op_id) for op_id, _, _ in calls])
    for op_id, plain, waiting in calls:
        row = _row(rows, op_id)
        verdict, reason = await _verdict(_service(), row, plain)
        assert verdict is PermissionVerdict.AUTO_EXECUTE, (op_id, reason)
        verdict, reason = await _verdict(_service(), row, waiting)
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (op_id, reason)


# ---------------------------------------------------------------------------
# Grant create, agent grant create and run approval create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_grant_create_refuses_an_app_spec_or_project_write(op_id: str) -> None:
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE,
                op_id=op_id,
                connector_id="argocd-3.x",
                reason="unattended deploy",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_run_approval_create_refuses_an_app_spec_or_project_write(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="argocd-3.x", op_id=op_id)],
                reason="one deploy",
            ),
        )
    assert refused.value.message == f"entries[0]: {credential_grant_reason(op_id)} Leave it out."


def _agent_grant(op_pattern: str) -> AgentGrantCreate:
    return AgentGrantCreate(
        principal_sub=_AGENT,
        principal_kind=GrantPrincipalKind.USER_AGENT,
        op_pattern=op_pattern,
        verdict=PermissionVerdict.AUTO_EXECUTE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS)
async def test_agent_grant_create_refuses_an_app_spec_or_project_write(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(_TENANT, "op-admin", _agent_grant(op_id))
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "connector_id"),
    [("argocd.app.sync", "argocd-api-3.x"), ("linux.sysctl.write", "linux-ssh-1.x")],
)
async def test_grants_of_a_sync_or_a_sysctl_write_are_still_accepted(
    op_id: str, connector_id: str
) -> None:
    assert credential_grant_reason(op_id) is None
    created = await ServicePrincipalGrantService().create(
        _TENANT,
        "op-admin",
        ServiceGrantCreate(
            principal_sub=_SERVICE,
            op_id=op_id,
            connector_id=connector_id,
            reason="unattended upkeep",
        ),
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert created.op_id == op_id
    agent_row = await AgentGrantService().grant(_TENANT, "op-admin", _agent_grant(op_id))
    assert agent_row.op_pattern == op_id

    if op_id == "linux.sysctl.write":
        # It runs a command as root (declare_root_shell), so a run approval
        # never covers any call of it.
        return
    approval = await RunApprovalService().request(
        _service(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=connector_id, op_id=op_id)],
            reason="one upkeep run",
        ),
    )
    assert approval is not None


# ---------------------------------------------------------------------------
# The approval card names the revision, and the key and the value
# ---------------------------------------------------------------------------


@dataclass
class _Descriptor:
    op_id: str


async def _card(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    ctx = PreviewContext(
        descriptor=_Descriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_person(),
        target=None,
        params=params,
        connector_id="c",
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect


@pytest.mark.asyncio
async def test_the_card_of_a_waiting_sync_names_the_revision() -> None:
    params = {"name": "web", "revision": "feature-branch"}
    effect = await _card("argocd.app.sync", params)
    assert effect["params_echo"]["revision"] == "feature-branch"
    shown = proposed_effect_for_agent(
        "argocd.app.sync", effect, params=params, connector_id="c", tenant=None
    )
    assert shown == effect


@pytest.mark.asyncio
async def test_the_card_of_a_waiting_sysctl_write_names_the_key_and_the_value() -> None:
    params = {"key": "kernel.core_pattern", "value": "core.%p"}
    effect = await _card("linux.sysctl.write", params)
    assert effect["params_echo"] == params
    shown = proposed_effect_for_agent(
        "linux.sysctl.write", effect, params=params, connector_id="c", tenant=None
    )
    assert shown == effect


# ---------------------------------------------------------------------------
# A sensor never runs a call that waits
# ---------------------------------------------------------------------------


def test_a_sensor_of_a_waiting_call_is_refused() -> None:
    descriptor = SimpleNamespace(safety_level="safe")
    for op_id, params in _WAITING_CALLS:
        payload = SimpleNamespace(connector_id="c", op_id=op_id, params=params)
        with pytest.raises(SensorRequiresNoPersonError) as refused:
            _require_read(payload, descriptor)  # type: ignore[arg-type]
        assert refused.value.error_code == "sensor_requires_safe_operation"


@pytest.mark.asyncio
async def test_a_sensor_of_a_waiting_call_reads_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lookup(**_kwargs: Any) -> Any:
        return SimpleNamespace(safety_level="safe")

    monkeypatch.setattr(sensor_runner, "lookup_descriptor", _lookup)
    for op_id, params in _WAITING_CALLS:
        sensor = SimpleNamespace(connector_id="c", op_id=op_id, params=params, tenant_id=_TENANT)
        outcome = await sensor_runner._safe_tier_floor(sensor)  # type: ignore[arg-type]
        assert outcome is not None, op_id
        assert "op_waits_for_person" in json.dumps(outcome, default=str)
