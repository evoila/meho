# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who is calling: :func:`~meho_backplane.auth.caller_kind.caller_kind`.

What these tests prove:

* Each way in gives its kind: the web console, a person's token over
  REST and over MCP, an agent login, a service login, a scheduled agent
  run, a hosted agent run (also one a person started in the web console),
  a runner login, and a person's login with no mark (``system``).
* Every login that MEHO's own code builds gives the kind it names, and
  has no token mark. The list of those logins comes from the source scan
  (``test_in_code_logins_have_a_kind.py``): a place the scan finds and
  this file does not build fails the test.
* A table pins how the five kinds line up with the evaluator's ``Asker``
  for every way in and every in-code login. ``AskerKind`` is unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from importlib import import_module
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock

import meho_connector_kit
import pytest

from meho_backplane.auth.caller_kind import CALLER_KINDS, CallerKind, caller_kind
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks.findings import investigator_operator
from meho_backplane.checks.runner import _sensor_operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.events.ingest.service import _build_ingest_operator
from meho_backplane.operations.approval_queue import expiry_operator
from meho_backplane.operations.ingest.boot_stamp import _boot_operator
from meho_backplane.operations.ingest.ingest_auth_store import _restore_operator
from meho_backplane.operations.secondary_target_policy import _Requester, _requester_operator
from meho_backplane.policy.evaluate import AskerKind
from meho_backplane.policy.explain_asker import what_if_operator
from meho_backplane.policy.shadow import asker_of
from meho_backplane.runbooks.run_service import check_step_caller
from meho_backplane.runner.executor import _build_operator
from meho_backplane.settings import get_settings
from meho_backplane.topology.scheduler import _system_operator
from meho_backplane.topology.scope_repair.stage import system_operator
from meho_backplane.ui.routes.broadcast.stream import session_reader
from meho_backplane.ui.routes.memory.operator import build_read_operator
from meho_backplane.ui.routes.operator_read import require_ui_read_operator
from meho_backplane.web_push.recipients import _operator_of

from .test_in_code_logins_have_a_kind import scan

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(**fields: Any) -> Operator:
    base: dict[str, Any] = {
        "sub": "login-1",
        "raw_jwt": "header.payload.signature",
        "tenant_id": _TENANT,
        "tenant_role": TenantRole.OPERATOR,
    }
    return Operator(**{**base, **fields})


def _kit_login() -> Operator:
    """A login built outside ``backend/src`` with the connector kit, no kind set."""
    return meho_connector_kit.Operator(
        sub="pkg:example", raw_jwt="", tenant_id=uuid.uuid4(), tenant_role=TenantRole.OPERATOR
    )


_WAYS_IN: dict[str, tuple[Operator, str | None, CallerKind]] = {
    "web console": (_login(web_session=True), None, "person"),
    "person's token over REST": (_login(token_checked=True), None, "personal_agent"),
    "person's token over MCP": (_login(token_checked=True, azp="meho-mcp"), None, "personal_agent"),
    "person's login with no mark": (_login(), None, "system"),
    "agent login": (
        _login(principal_kind=PrincipalKind.AGENT, token_checked=True),
        None,
        "service_agent",
    ),
    "service login": (
        _login(principal_kind=PrincipalKind.SERVICE, token_checked=True),
        None,
        "service_agent",
    ),
    "agent login with no mark": (_login(principal_kind=PrincipalKind.AGENT), None, "service_agent"),
    "service login with no mark": (
        _login(principal_kind=PrincipalKind.SERVICE),
        None,
        "service_agent",
    ),
    "scheduled agent run": (_login(principal_kind=PrincipalKind.AGENT), None, "service_agent"),
    "hosted agent run started with a token": (
        _login(token_checked=True),
        "agent:helper",
        "service_agent",
    ),
    "hosted agent run started in the web console": (
        _login(web_session=True),
        "agent:helper",
        "service_agent",
    ),
    "runner login": (
        _login(principal_kind=PrincipalKind.RUNNER, runner_id=uuid.uuid4()),
        None,
        "runner",
    ),
    "person's login with the kind runner and no mark": (
        _login(in_code_kind="runner"),
        None,
        "runner",
    ),
    "login built with the connector kit": (_kit_login(), None, "system"),
    "runbook check step of a web console caller": (
        check_step_caller(_login(web_session=True)),
        None,
        "personal_agent",
    ),
}


@pytest.mark.parametrize("way_in", list(_WAYS_IN))
def test_each_way_in_has_its_kind(way_in: str) -> None:
    operator, actor_sub, expected = _WAYS_IN[way_in]
    assert caller_kind(operator, actor_sub) == expected


def test_a_web_session_flag_on_a_machine_login_does_not_make_a_person() -> None:
    agent = _login(principal_kind=PrincipalKind.AGENT, web_session=True)
    assert caller_kind(agent, None) == "service_agent"


def test_the_runbook_check_step_keeps_the_kind_of_a_machine_or_in_code_login() -> None:
    agent = check_step_caller(_login(principal_kind=PrincipalKind.AGENT, token_checked=True))
    assert caller_kind(agent, None) == "service_agent"
    system = check_step_caller(_login(in_code_kind="system"))
    assert caller_kind(system, None) == "system"
    step = check_step_caller(_login(web_session=True))
    assert step.web_session is False
    assert step.in_code_kind == "personal_agent"


def _session_ctx() -> Any:
    return SimpleNamespace(operator_sub="person-1", tenant_id=_TENANT)


def _requester(kind: PrincipalKind) -> Operator:
    requester = _Requester(tenant_id=_TENANT, sub="requester-1", act=None, subject=None)
    return _requester_operator(
        requester, {"tenant_role": "operator", "principal_kind": kind.value, "client_id": None}
    )


def _runner_item() -> Operator:
    principal = SimpleNamespace(
        sub="person-1",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    return _build_operator(SimpleNamespace(principal=principal))


def _push_recipient() -> Operator:
    row = SimpleNamespace(
        operator_sub="person-1",
        tenant_id=_TENANT,
        tenant_role="tenant_admin",
        approver=True,
        self_approver=False,
        platform_admin=False,
    )
    operator = _operator_of(row)  # type: ignore[arg-type]
    assert operator is not None
    return operator


def _what_if(principal: Any, via: Any) -> Operator:
    caller = _login()
    return what_if_operator(caller, groups=["Operators"], principal=principal, via=via)


_Build = Callable[[pytest.MonkeyPatch], Awaitable[Operator]]


def _sync(build: Callable[[], Operator]) -> _Build:
    async def _run(_: pytest.MonkeyPatch) -> Operator:
        return build()

    return _run


async def _check_runner(monkeypatch: pytest.MonkeyPatch) -> Operator:
    monkeypatch.setattr(
        "meho_backplane.checks.runner.check_runner_jwt", AsyncMock(return_value="t")
    )
    snap = SimpleNamespace(identity_sub="__sensor__", tenant_id=_TENANT)
    return await _sensor_operator(snap)  # type: ignore[arg-type]


async def _event_intake(monkeypatch: pytest.MonkeyPatch) -> Operator:
    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.check_runner_jwt", AsyncMock(return_value="t")
    )
    return await _build_ingest_operator(_TENANT)


async def _ui_read_operator(_: pytest.MonkeyPatch) -> Operator:
    return await require_ui_read_operator(_session_ctx())


def _shim(module: str, cls: str) -> _Build:
    """Run the connector's legacy shim; return the login it hands the dispatcher."""

    async def _run(monkeypatch: pytest.MonkeyPatch) -> Operator:
        seen: list[Operator] = []

        async def _dispatch(*, operator: Operator, **_: Any) -> None:
            seen.append(operator)

        monkeypatch.setattr("meho_backplane.operations.dispatch", _dispatch)
        connector = getattr(import_module(f"meho_backplane.connectors.{module}.connector"), cls)()
        await connector.execute(None, "x.read", {})
        (operator,) = seen
        return operator

    return _run


#: Each connector with a legacy shim: its folder and its class.
_SHIMS: tuple[tuple[str, str], ...] = (
    ("argocd", "ArgoCdConnector"),
    ("azure", "AzureConnector"),
    ("entra", "EntraConnector"),
    ("freeipa", "FreeipaConnector"),
    ("gcloud", "GcloudConnector"),
    ("harbor", "HarborConnector"),
    ("hetzner_robot", "HetznerRobotConnector"),
    ("keycloak", "KeycloakConnector"),
    ("loki", "LokiConnector"),
    ("mongodb", "MongoDbConnector"),
    ("mssql", "MssqlConnector"),
    ("phpipam", "PhpipamConnector"),
    ("postgres", "PostgresConnector"),
    ("prometheus", "PrometheusConnector"),
    ("proxmox", "ProxmoxConnector"),
    ("rabbitmq", "RabbitMqConnector"),
    ("tempo", "TempoConnector"),
    ("vault", "VaultConnector"),
)

#: Every login that MEHO's own code builds: name -> (source file, builder, kind).
_IN_CODE: dict[str, tuple[str, _Build, CallerKind]] = {
    "check runner": ("checks/runner.py", _check_runner, "system"),
    "event intake": ("events/ingest/service.py", _event_intake, "system"),
    "findings investigator": (
        "checks/findings.py",
        _sync(lambda: investigator_operator(_TENANT)),
        "system",
    ),
    "topology scope repair": (
        "topology/scope_repair/stage.py",
        _sync(lambda: system_operator(_TENANT)),
        "system",
    ),
    "topology scheduler": (
        "topology/scheduler.py",
        _sync(lambda: _system_operator(_TENANT)),
        "system",
    ),
    "boot stamp": ("operations/ingest/boot_stamp.py", _sync(_boot_operator), "system"),
    "expiry of approval requests": (
        "operations/approval_queue.py",
        _sync(lambda: expiry_operator(_TENANT)),
        "system",
    ),
    "ingest auth store": (
        "operations/ingest/ingest_auth_store.py",
        _sync(lambda: _restore_operator(None)),
        "system",
    ),
    "connectors' system login": (
        "connectors/_shared/system_operator.py",
        _sync(synthesise_system_operator),
        "system",
    ),
    **{
        f"connector shim: {module}": (
            f"connectors/{module}/connector.py",
            _shim(module, cls),
            "system",
        )
        for module, cls in _SHIMS
    },
    "runner executor": ("runner/executor.py", _sync(_runner_item), "runner"),
    "secondary target, person's request": (
        "operations/secondary_target_policy.py",
        _sync(lambda: _requester(PrincipalKind.USER)),
        "personal_agent",
    ),
    "secondary target, agent's request": (
        "operations/secondary_target_policy.py",
        _sync(lambda: _requester(PrincipalKind.AGENT)),
        "service_agent",
    ),
    "explain asker, person in the web console": (
        "policy/explain_asker.py",
        _sync(lambda: _what_if("person", "web")),
        "person",
    ),
    "explain asker, person with a token": (
        "policy/explain_asker.py",
        _sync(lambda: _what_if("person", "token")),
        "personal_agent",
    ),
    "explain asker, agent": (
        "policy/explain_asker.py",
        _sync(lambda: _what_if("agent", None)),
        "service_agent",
    ),
    "explain asker, service": (
        "policy/explain_asker.py",
        _sync(lambda: _what_if("service", None)),
        "service_agent",
    ),
    "web push recipient": ("web_push/recipients.py", _sync(_push_recipient), "person"),
    "console read operator": ("ui/routes/operator_read.py", _ui_read_operator, "person"),
    "console memory reader": (
        "ui/routes/memory/operator.py",
        _sync(lambda: build_read_operator(_session_ctx())),
        "person",
    ),
    "console broadcast reader": (
        "ui/routes/broadcast/stream.py",
        _sync(lambda: session_reader(_session_ctx())),
        "person",
    ),
}


def test_every_place_the_scan_finds_is_built_here() -> None:
    scanned = {login.file for login in scan()}
    built = {file for file, _, _ in _IN_CODE.values()}
    assert scanned == built


@pytest.mark.asyncio
@pytest.mark.parametrize("login", list(_IN_CODE))
async def test_each_in_code_login_has_its_kind(login: str, monkeypatch: pytest.MonkeyPatch) -> None:
    file, build, expected = _IN_CODE[login]
    operator = await build(monkeypatch)
    assert operator.in_code_kind == expected
    assert operator.token_checked is False
    assert caller_kind(operator, None) == expected
    named = {found.kind for found in scan() if found.file == file and found.kind is not None}
    assert named <= {expected}


def test_caller_kinds_lists_every_kind() -> None:
    assert set(CALLER_KINDS) == set(get_args(CallerKind))


def test_asker_kind_is_unchanged() -> None:
    assert get_args(AskerKind) == ("person", "agent", "service", "runner")


_SYSTEM: tuple[CallerKind, AskerKind, str, str | None] = ("system", "person", "token", None)

#: Every way in and every in-code login: (caller kind, ``Asker.kind``,
#: ``Asker.via``, ``Asker.acting_agent``). Where the two differ, both are
#: written as they are today: a console reader is a ``person`` and a person
#: by token; the runner executor is a ``runner`` and a person by token; each
#: ``system`` login is a person by token.
_MAPPING: dict[str, tuple[CallerKind, AskerKind, str, str | None]] = {
    "web console": ("person", "person", "web", None),
    "person's token over REST": ("personal_agent", "person", "token", None),
    "person's token over MCP": ("personal_agent", "person", "token", None),
    "person's login with no mark": _SYSTEM,
    "agent login": ("service_agent", "agent", "token", None),
    "service login": ("service_agent", "service", "token", None),
    "agent login with no mark": ("service_agent", "agent", "token", None),
    "service login with no mark": ("service_agent", "service", "token", None),
    "scheduled agent run": ("service_agent", "agent", "token", None),
    "hosted agent run started with a token": ("service_agent", "person", "token", "agent:helper"),
    "hosted agent run started in the web console": (
        "service_agent",
        "person",
        "web",
        "agent:helper",
    ),
    "runner login": ("runner", "runner", "token", None),
    "person's login with the kind runner and no mark": ("runner", "person", "token", None),
    "login built with the connector kit": _SYSTEM,
    "runbook check step of a web console caller": ("personal_agent", "person", "token", None),
    "check runner": _SYSTEM,
    "event intake": _SYSTEM,
    "findings investigator": _SYSTEM,
    "topology scope repair": _SYSTEM,
    "topology scheduler": _SYSTEM,
    "boot stamp": _SYSTEM,
    "expiry of approval requests": _SYSTEM,
    "ingest auth store": _SYSTEM,
    "connectors' system login": _SYSTEM,
    **{f"connector shim: {module}": _SYSTEM for module, _ in _SHIMS},
    "runner executor": ("runner", "person", "token", None),
    "secondary target, person's request": ("personal_agent", "person", "token", None),
    "secondary target, agent's request": ("service_agent", "agent", "token", None),
    "explain asker, person in the web console": ("person", "person", "web", None),
    "explain asker, person with a token": ("personal_agent", "person", "token", None),
    "explain asker, agent": ("service_agent", "agent", "token", None),
    "explain asker, service": ("service_agent", "service", "token", None),
    "web push recipient": ("person", "person", "web", None),
    "console read operator": ("person", "person", "token", None),
    "console memory reader": ("person", "person", "token", None),
    "console broadcast reader": ("person", "person", "token", None),
}


def test_the_mapping_has_a_row_for_every_way_in_and_every_in_code_login() -> None:
    assert set(_MAPPING) == set(_WAYS_IN) | set(_IN_CODE)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", list(_MAPPING))
async def test_caller_kind_and_asker_line_up(row: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if row in _WAYS_IN:
        operator, actor_sub, _ = _WAYS_IN[row]
    else:
        operator, actor_sub = await _IN_CODE[row][1](monkeypatch), None
    asker = asker_of(operator, acting_agent=actor_sub)
    got = (caller_kind(operator, actor_sub), asker.kind, asker.via, asker.acting_agent)
    assert got == _MAPPING[row]
