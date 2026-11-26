# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who is calling: :func:`~meho_backplane.auth.caller_kind.caller_kind`.

What these tests prove:

* Each way in gives its kind: the web console, a person's token over
  REST and over MCP, an agent login, a service login, a hosted agent run
  (also one a person started in the web console), a runner login.
* Each login that MEHO's own code builds gives the kind it sets.
* A table pins how the five kinds line up with the evaluator's
  ``AskerKind`` and ``Asker.via``. ``AskerKind`` is unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from meho_backplane.auth.caller_kind import CALLER_KINDS, CallerKind, caller_kind
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks.findings import investigator_operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.operations.approval_queue import expiry_operator
from meho_backplane.operations.ingest.boot_stamp import _boot_operator
from meho_backplane.operations.ingest.ingest_auth_store import _restore_operator
from meho_backplane.operations.secondary_target_policy import _Requester, _requester_operator
from meho_backplane.policy.evaluate import AskerKind
from meho_backplane.policy.explain_asker import what_if_operator
from meho_backplane.policy.shadow import asker_of
from meho_backplane.runner.executor import _build_operator
from meho_backplane.settings import get_settings
from meho_backplane.topology.scheduler import _system_operator
from meho_backplane.topology.scope_repair.stage import system_operator
from meho_backplane.ui.routes.broadcast.stream import session_reader
from meho_backplane.ui.routes.memory.operator import build_read_operator
from meho_backplane.ui.routes.operator_read import require_ui_read_operator
from meho_backplane.web_push.recipients import _operator_of

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


_WAYS_IN: dict[str, tuple[Operator, str | None, CallerKind]] = {
    "web console": (_login(web_session=True), None, "person"),
    "person's token over REST": (_login(token_checked=True), None, "personal_agent"),
    "person's token over MCP": (_login(token_checked=True, azp="meho-mcp"), None, "personal_agent"),
    "agent login": (_login(principal_kind=PrincipalKind.AGENT), None, "service_agent"),
    "service login": (_login(principal_kind=PrincipalKind.SERVICE), None, "service_agent"),
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
    "person's login with no mark": (_login(), None, "personal_agent"),
}


@pytest.mark.parametrize("way_in", list(_WAYS_IN))
def test_each_way_in_has_its_kind(way_in: str) -> None:
    operator, actor_sub, expected = _WAYS_IN[way_in]
    assert caller_kind(operator, actor_sub) == expected


def test_a_web_session_flag_on_a_machine_login_does_not_make_a_person() -> None:
    agent = _login(principal_kind=PrincipalKind.AGENT, web_session=True)
    assert caller_kind(agent, None) == "service_agent"


def _session_ctx() -> Any:
    return SimpleNamespace(operator_sub="person-1", tenant_id=_TENANT)


def _requester(kind: PrincipalKind) -> Operator:
    requester = _Requester(tenant_id=_TENANT, sub="requester-1", act=None, subject=None)
    return _requester_operator(
        requester, {"tenant_role": "operator", "principal_kind": kind.value, "client_id": None}
    )


def _runner_item() -> Any:
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


async def _ui_read_operator() -> Operator:
    return await require_ui_read_operator(_session_ctx())


_IN_CODE: dict[str, tuple[Callable[[], Operator], CallerKind]] = {
    "findings investigator": (lambda: investigator_operator(_TENANT), "system"),
    "topology scope repair": (lambda: system_operator(_TENANT), "system"),
    "topology scheduler": (lambda: _system_operator(_TENANT), "system"),
    "boot stamp": (_boot_operator, "system"),
    "expiry operator": (lambda: expiry_operator(_TENANT), "system"),
    "ingest auth store": (lambda: _restore_operator(None), "system"),
    "connector system operator": (synthesise_system_operator, "system"),
    "runner executor": (_runner_item, "runner"),
    "secondary target, person's request": (
        lambda: _requester(PrincipalKind.USER),
        "personal_agent",
    ),
    "secondary target, agent's request": (
        lambda: _requester(PrincipalKind.AGENT),
        "service_agent",
    ),
    "explain asker, person in the web console": (lambda: _what_if("person", "web"), "person"),
    "explain asker, person with a token": (lambda: _what_if("person", "token"), "personal_agent"),
    "explain asker, agent": (lambda: _what_if("agent", None), "service_agent"),
    "explain asker, service": (lambda: _what_if("service", None), "service_agent"),
    "web push recipient": (_push_recipient, "person"),
    "console memory reader": (lambda: build_read_operator(_session_ctx()), "person"),
    "console broadcast reader": (lambda: session_reader(_session_ctx()), "person"),
}


@pytest.mark.parametrize("login", list(_IN_CODE))
def test_each_in_code_login_has_its_kind(login: str) -> None:
    build, expected = _IN_CODE[login]
    operator = build()
    assert operator.in_code_kind == expected
    assert operator.token_checked is False
    assert caller_kind(operator, None) == expected


@pytest.mark.asyncio
async def test_the_console_read_operator_is_a_person() -> None:
    operator = await _ui_read_operator()
    assert caller_kind(operator, None) == "person"
    assert operator.token_checked is False


def test_caller_kinds_lists_every_kind() -> None:
    assert set(CALLER_KINDS) == set(get_args(CallerKind))


def test_asker_kind_is_unchanged() -> None:
    assert get_args(AskerKind) == ("person", "agent", "service", "runner")


#: How each kind of caller lines up with the evaluator's asker: the way in,
#: then (caller kind, ``Asker.kind``, ``Asker.via``). In the evaluator
#: "person" is any human login, by web or by token; ``via`` tells them apart.
#: A hosted agent run keeps the caller as the asker and names the agent.
_MAPPING: tuple[tuple[str, CallerKind, AskerKind, str], ...] = (
    ("web console", "person", "person", "web"),
    ("person's token over REST", "personal_agent", "person", "token"),
    ("person's token over MCP", "personal_agent", "person", "token"),
    ("agent login", "service_agent", "agent", "token"),
    ("service login", "service_agent", "service", "token"),
    ("hosted agent run started with a token", "service_agent", "person", "token"),
    ("hosted agent run started in the web console", "service_agent", "person", "web"),
    ("runner login", "runner", "runner", "token"),
    ("findings investigator", "system", "person", "token"),
    ("boot stamp", "system", "person", "token"),
)


@pytest.mark.parametrize(("way_in", "kind", "asker_kind", "via"), _MAPPING)
def test_caller_kind_and_asker_line_up(
    way_in: str, kind: CallerKind, asker_kind: AskerKind, via: str
) -> None:
    if way_in in _WAYS_IN:
        operator, actor_sub, _ = _WAYS_IN[way_in]
    else:
        operator, actor_sub = _IN_CODE[way_in][0](), None
    asker = asker_of(operator, acting_agent=actor_sub)
    assert (caller_kind(operator, actor_sub), asker.kind, asker.via) == (kind, asker_kind, via)
