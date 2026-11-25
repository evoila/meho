# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO says when a change of rights needs a second admin (#6641).

:func:`tenant_admin_ids` counts the admins of a tenant against a small fake
Keycloak (respx). A count MEHO is not sure of is ``None`` and never gives a
warning. The answer to a change of rights in a tenant with one admin holds
the sentence; with two admins, or when the count is not known, the answer
is unchanged. ``/api/v1/health`` reports the count.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.api.v1.health import VaultStatus, build_health_response
from meho_backplane.auth import tenant_admins
from meho_backplane.auth.approval_sign_in_flow import ApprovalSignInStatus
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.tenant_admins import (
    NEEDS_SECOND_ADMIN,
    NO_ADMIN,
    ONLY_ONE_ADMIN,
    RIGHTS_CHANGE_GUIDE_URL,
    admin_count,
    only_admin_note,
    tenant_admin_ids,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatcher, grant_approval
from meho_backplane.operations._errors import result_awaiting_approval
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.settings import get_settings
from meho_backplane.ui.templating import get_templates
from tests.test_api_v1_approvals_fresh_sign_in import _settings_env  # noqa: F401 -- autouse

_KC = "https://keycloak.test"
_ADMIN = f"{_KC}/admin/realms/meho"
_TOKEN = f"{_KC}/realms/meho/protocol/openid-connect/token"
_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006641")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006642")
_ADMIN_A = "user-admin-a"
_ADMIN_B = "user-admin-b"
_RIGHTS_OP = "demo.rights.change"


@pytest.fixture(autouse=True)
def _admin_client_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{_KC}/realms/meho")
    monkeypatch.delenv("KEYCLOAK_DISCOVERY_URL", raising=False)
    monkeypatch.delenv("JWT_PLATFORM_ADMIN_ROLE_NAME", raising=False)
    get_settings.cache_clear()
    tenant_admins._kept.clear()
    yield
    get_settings.cache_clear()
    tenant_admins._kept.clear()


def _group(
    group_id: str, attributes: dict[str, list[str]] | None = None, *, children: int = 0
) -> dict[str, Any]:
    return {
        "id": group_id,
        "name": group_id,
        "attributes": attributes or {},
        "subGroupCount": children,
    }


def _admins_group(tenant: uuid.UUID = _TENANT) -> dict[str, Any]:
    """The first tenant's MEHO-Admins group, as the realm file makes it."""
    return _group(
        "g-admins",
        {
            "tenant_id": [str(tenant)],
            "tenant_role": ["tenant_admin"],
            "meho_tenant": [f"{tenant}:main:Admins"],
        },
    )


def _operators_group() -> dict[str, Any]:
    return _group(
        "g-operators",
        {
            "tenant_id": [str(_TENANT)],
            "tenant_role": ["operator"],
            "meho_tenant": [f"{_TENANT}:main:Operators"],
        },
    )


def _platform_group() -> dict[str, Any]:
    return _group("g-platform", {"platform_admin": ["true"]})


def _user(user_id: str, *, enabled: bool = True) -> dict[str, Any]:
    return {"id": user_id, "username": user_id, "enabled": enabled}


def _mount(
    mock: respx.MockRouter,
    groups: list[dict[str, Any]] | httpx.Response,
    members: dict[str, list[dict[str, Any]]],
    children: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    mock.post(_TOKEN).respond(200, json={"access_token": "made-up-token"})
    if isinstance(groups, httpx.Response):
        mock.get(f"{_ADMIN}/groups").mock(return_value=groups)
    else:
        mock.get(f"{_ADMIN}/groups").respond(200, json=groups)
    for group_id, rows in (children or {}).items():
        mock.get(f"{_ADMIN}/groups/{group_id}/children").respond(200, json=rows)
    for group_id, rows in members.items():
        mock.get(f"{_ADMIN}/groups/{group_id}/members").respond(200, json=rows)


# --- counting --------------------------------------------------------------------------


async def test_one_admin_in_the_admin_group() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _operators_group()],
            {"g-admins": [_user(_ADMIN_A)], "g-operators": [_user("user-op")]},
        )
        assert await tenant_admin_ids(_TENANT) == frozenset({_ADMIN_A})


async def test_platform_admins_count_and_one_person_in_both_groups_counts_once() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _platform_group()],
            {"g-admins": [_user(_ADMIN_A)], "g-platform": [_user(_ADMIN_A), _user(_ADMIN_B)]},
        )
        assert await admin_count(_TENANT) == 2


async def test_another_tenants_admins_do_not_count() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group(), _admins_group(_OTHER_TENANT) | {"id": "g-other"}],
            {"g-admins": [_user(_ADMIN_A)], "g-other": [_user(_ADMIN_B)]},
        )
        assert await admin_count(_TENANT) == 1


async def test_an_extra_tenant_group_with_only_meho_tenant_counts() -> None:
    extra = _group("g-extra", {"meho_tenant": [f"{_TENANT}:main:Admins"]})
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [extra], {"g-extra": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await admin_count(_TENANT) == 2


async def test_a_child_group_takes_the_attributes_of_its_parent() -> None:
    parent = _admins_group() | {"subGroupCount": 1}
    child = _group("g-child", {"description": ["company directory"]})
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [parent],
            {"g-admins": [_user(_ADMIN_A)], "g-child": [_user(_ADMIN_B)]},
            children={"g-admins": [child]},
        )
        assert await admin_count(_TENANT) == 2


async def test_a_disabled_person_does_not_count() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group()],
            {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B, enabled=False)]},
        )
        assert await admin_count(_TENANT) == 1


async def test_a_service_account_in_the_admin_group_does_not_count() -> None:
    """Only a person may approve a change of rights, so a client login is not counted."""
    robot = {"id": "user-robot", "username": "service-account-meho-agent", "enabled": True}
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), robot]})
        assert await admin_count(_TENANT) == 1
        tenant_admins._kept.clear()
        note = await only_admin_note(_TENANT, _ADMIN_A)
    assert note == f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"


async def test_the_service_account_prefix_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_PREFIX", "robot-")
    get_settings.cache_clear()
    robot = {"id": "user-robot", "username": "robot-meho-agent", "enabled": True}
    with respx.mock(assert_all_called=False) as mock:
        _mount(
            mock,
            [_admins_group()],
            {"g-admins": [_user(_ADMIN_A), _user("service-account-is-a-person"), robot]},
        )
        assert await admin_count(_TENANT) == 2


async def test_members_are_read_page_by_page() -> None:
    first = [_user(f"user-{n}") for n in range(tenant_admins._PAGE_SIZE)]
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {})
        route = mock.get(f"{_ADMIN}/groups/g-admins/members")
        route.side_effect = [
            httpx.Response(200, json=first),
            httpx.Response(200, json=[_user("user-last")]),
        ]
        assert await admin_count(_TENANT) == tenant_admins._PAGE_SIZE + 1
    assert route.calls[1].request.url.params["first"] == str(tenant_admins._PAGE_SIZE)


@pytest.mark.parametrize(
    "groups",
    [httpx.Response(403), httpx.Response(500), httpx.Response(200, json={"odd": "body"})],
    ids=["refused", "server error", "not a list"],
)
async def test_a_failed_group_read_is_not_known(groups: httpx.Response) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, groups, {})
        assert await tenant_admin_ids(_TENANT) is None


async def test_a_failed_member_read_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {})
        mock.get(f"{_ADMIN}/groups/g-admins/members").mock(
            side_effect=httpx.ConnectError("refused")
        )
        assert await tenant_admin_ids(_TENANT) is None


async def test_a_failed_admin_login_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_TOKEN).respond(401)
        assert await tenant_admin_ids(_TENANT) is None


async def test_no_admin_group_or_no_member_is_not_known() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_operators_group()], {"g-operators": [_user("user-op")]})
        assert await tenant_admin_ids(_TENANT) is None
    tenant_admins._kept.clear()
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": []})
        assert await tenant_admin_ids(_TENANT) is None


async def test_no_admin_client_is_not_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL")
    get_settings.cache_clear()
    assert await tenant_admin_ids(_TENANT) is None


async def test_platform_admins_from_a_realm_role_are_not_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "platform-admin")
    get_settings.cache_clear()
    assert await tenant_admin_ids(_TENANT) is None


async def test_the_count_is_kept_and_not_known_only_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A)]})
        assert await admin_count(_TENANT) == 1
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await admin_count(_TENANT) == 1
    key = (get_settings().keycloak_admin_url, _TENANT)
    tenant_admins._kept[key] = (time.monotonic() - tenant_admins._KEEP_FAILED_S - 1, None)
    with respx.mock(assert_all_called=False) as mock:
        _mount(mock, [_admins_group()], {"g-admins": [_user(_ADMIN_A), _user(_ADMIN_B)]})
        assert await admin_count(_TENANT) == 2


# --- the note --------------------------------------------------------------------------


def _known(monkeypatch: pytest.MonkeyPatch, ids: set[str] | None) -> None:
    async def read(*_args: object) -> frozenset[str] | None:
        return None if ids is None else frozenset(ids)

    monkeypatch.setattr(tenant_admins, "_read", read)


async def test_the_note_for_the_only_admin_holds_the_sentence_and_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known(monkeypatch, {_ADMIN_A})
    note = await only_admin_note(_TENANT, _ADMIN_A.upper())
    assert note == f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"


async def test_the_note_for_no_admin_says_so_and_holds_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known(monkeypatch, set())
    note = await only_admin_note(_TENANT, _ADMIN_A)
    assert note == f"This tenant has no admin. See {RIGHTS_CHANGE_GUIDE_URL}"
    assert NO_ADMIN in note
    assert "only one admin" not in note
    assert "second admin" not in note


@pytest.mark.parametrize(
    ("ids", "requester"),
    [({_ADMIN_A, _ADMIN_B}, _ADMIN_A), ({_ADMIN_A}, "user-operator"), (None, _ADMIN_A)],
    ids=["two admins", "the one admin can approve another person", "not known"],
)
async def test_no_note_when_another_admin_could_approve_or_not_known(
    monkeypatch: pytest.MonkeyPatch, ids: set[str] | None, requester: str
) -> None:
    _known(monkeypatch, ids)
    assert await only_admin_note(_TENANT, requester) is None


def test_the_answer_without_a_note_is_unchanged() -> None:
    request_id = uuid.uuid4()
    result = result_awaiting_approval("demo.op", request_id, 1.0)
    assert result.error == "awaiting_approval: 'demo.op' requires approval before execution"
    assert result.extras == {
        "error_code": "awaiting_approval",
        "approval_request_id": str(request_id),
    }


# --- the answer at request time -----------------------------------------------------------


def _descriptor() -> EndpointDescriptor:
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="demo",
        version="1",
        impl_id="demo",
        op_id=_RIGHTS_OP,
        source_kind="typed",
        handler_ref=None,
        summary="Give a person a role.",
        description="A made-up change of rights.",
        tags=[],
        parameter_schema={"type": "object"},
        response_schema=None,
        llm_instructions=None,
        safety_level="dangerous",
        requires_approval=True,
        is_enabled=True,
    )


async def _ask(monkeypatch: pytest.MonkeyPatch, op_id: str = _RIGHTS_OP) -> Any:
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_RIGHTS_OP})
    operator = Operator(
        sub=_ADMIN_A,
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    descriptor = _descriptor()
    descriptor.op_id = op_id
    return await dispatcher._handle_needs_approval(
        op_id=op_id,
        connector_id="demo-1",
        operator=operator,
        descriptor=descriptor,
        target=None,
        params={"user": "someone"},
        params_hash="hash",
        duration_ms=1.0,
    )


async def test_the_only_admin_asks_for_a_change_of_rights_and_reads_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known(monkeypatch, {_ADMIN_A})
    result = await _ask(monkeypatch)
    assert result.status == "awaiting_approval"
    assert ONLY_ONE_ADMIN in (result.error or "")
    assert result.extras["approval_note"] == f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"
    assert result.extras["approval_request_id"]


async def test_a_waiting_change_of_rights_with_no_admin_reads_no_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known(monkeypatch, set())
    result = await _ask(monkeypatch)
    note = f"{NO_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"
    assert result.status == "awaiting_approval"
    assert result.extras["approval_note"] == note
    assert result.error == (
        f"awaiting_approval: {_RIGHTS_OP!r} requires approval before execution. {note}"
    )
    assert NEEDS_SECOND_ADMIN not in (result.error or "")


async def test_a_step_with_no_admin_reads_no_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_RIGHTS_OP})
    _known(monkeypatch, set())
    operator = Operator(
        sub=_ADMIN_A,
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    result = await enforce_subop_policy(
        operator=operator,
        connector_id="demo-rest-1.0",
        op_id=_RIGHTS_OP,
        safety_level="dangerous",
        requires_approval=True,
        target=None,
        params={"user": "someone"},
    )
    assert result is not None
    assert result.extras["approval_note"] == f"{NO_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"


@pytest.mark.parametrize(
    ("ids", "note"),
    [
        (set(), f"{NO_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"),
        ({_ADMIN_A}, f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"),
    ],
    ids=["no admin", "one admin"],
)
async def test_the_approved_retry_note_for_each_count(
    monkeypatch: pytest.MonkeyPatch, ids: set[str], note: str
) -> None:
    """Only the sentence that is false for an approved request goes; the rest stays."""
    _known(monkeypatch, ids)
    returned = await only_admin_note(_TENANT, _ADMIN_A)
    result = result_awaiting_approval(
        _RIGHTS_OP, uuid.uuid4(), 1.0, note=returned, already_approved=True
    )
    kept = note.removeprefix(NEEDS_SECOND_ADMIN).lstrip()
    assert result.extras["approval_note"] == kept
    assert NEEDS_SECOND_ADMIN not in (result.error or "")
    assert (result.error or "").endswith(f". {kept}")


@pytest.mark.parametrize("ids", [{_ADMIN_A, _ADMIN_B}, None], ids=["two admins", "not known"])
async def test_the_answer_is_unchanged_with_two_admins_or_an_unknown_count(
    monkeypatch: pytest.MonkeyPatch, ids: set[str] | None
) -> None:
    _known(monkeypatch, ids)
    result = await _ask(monkeypatch)
    assert result.status == "awaiting_approval"
    assert result.error == f"awaiting_approval: {_RIGHTS_OP!r} requires approval before execution"
    assert "approval_note" not in result.extras


async def test_a_request_that_is_no_change_of_rights_gets_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _known(monkeypatch, {_ADMIN_A})
    result = await _ask(monkeypatch, op_id="demo.vm.restart")
    assert result.status == "awaiting_approval"
    assert "approval_note" not in result.extras


async def test_a_failed_note_read_still_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def read(*_args: object) -> frozenset[str] | None:
        calls.append("note")
        raise RuntimeError("made-up failure")

    async def step_event(**_kwargs: object) -> None:
        calls.append("step event")

    monkeypatch.setattr(tenant_admins, "_read", read)
    monkeypatch.setattr(
        "meho_backplane.operations.approval_queue.record_approval_step_event", step_event
    )
    result = await _ask(monkeypatch)
    assert result.status == "awaiting_approval"
    assert result.extras["approval_request_id"]
    assert "approval_note" not in result.extras
    assert calls == ["step event", "note"]


async def test_a_step_of_a_multi_step_operation_gets_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_RIGHTS_OP})
    _known(monkeypatch, {_ADMIN_A})
    operator = Operator(
        sub=_ADMIN_A,
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    result = await enforce_subop_policy(
        operator=operator,
        connector_id="demo-rest-1.0",
        op_id=_RIGHTS_OP,
        safety_level="dangerous",
        requires_approval=True,
        target=None,
        params={"user": "someone"},
    )
    assert result is not None
    assert result.status == "awaiting_approval"
    assert result.extras["approval_note"] == f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"
    assert ONLY_ONE_ADMIN in (result.error or "")


# --- /api/v1/health -------------------------------------------------------------------


@pytest.mark.parametrize("ids", [{_ADMIN_A}, {_ADMIN_A, _ADMIN_B}, None])
async def test_health_reports_the_count(
    monkeypatch: pytest.MonkeyPatch, ids: set[str] | None
) -> None:
    async def vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    async def sign_in() -> ApprovalSignInStatus:
        return ApprovalSignInStatus(state="ok", client="meho-cli-approve")

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", vault)
    monkeypatch.setattr("meho_backplane.api.v1.health.read_approval_sign_in", sign_in)
    _known(monkeypatch, ids)
    operator = Operator(
        sub="op-6641",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    response = await build_health_response(operator)
    assert response.approvals is not None
    assert response.approvals.rights_change_admins == (None if ids is None else len(ids))


# --- the console shows the note -------------------------------------------------------

_NOTE = f"{ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"


def _waiting(note: str | None) -> dict[str, Any]:
    return result_awaiting_approval(_RIGHTS_OP, uuid.uuid4(), 1.0, note=note).model_dump(
        mode="json"
    )


def _render(template: str, envelope: dict[str, Any]) -> str:
    context: dict[str, Any] = {"envelope": envelope, "error_message": None}
    if template == "operations/_result.html":
        context |= {"connector_id": "demo", "op_id": _RIGHTS_OP, "chain": None}
    return get_templates().env.get_template(template).render(**context)


@pytest.mark.parametrize(
    ("template", "marker"),
    [
        ("keycloak/_write_result.html", "data-write-approval-note"),
        ("operations/_result.html", "data-run-approval-note"),
    ],
)
def test_the_console_answer_shows_the_note(template: str, marker: str) -> None:
    html = _render(template, _waiting(_NOTE))
    assert marker in html
    assert ONLY_ONE_ADMIN in html
    assert RIGHTS_CHANGE_GUIDE_URL in html


@pytest.mark.parametrize("template", ["keycloak/_write_result.html", "operations/_result.html"])
def test_the_console_answer_without_a_note_is_unchanged(template: str) -> None:
    envelope = _waiting(None)
    html = _render(template, envelope)
    assert "approval-note" not in html
    assert ONLY_ONE_ADMIN not in html
    # The note is escaped like any other text.
    with_markup = _render(template, _waiting("<b>a & b</b>"))
    assert "&lt;b&gt;a &amp; b&lt;/b&gt;" in with_markup


def test_the_classify_page_shows_the_note() -> None:
    template = get_templates().env.get_template("policy/_classify_result.html")
    requested = {
        "status": "awaiting_approval",
        "approval_request_id": str(uuid.uuid4()),
        "error": None,
        "matched_operations": 2,
        "approval_note": _NOTE,
    }
    html = template.render(error=None, preview=None, form={}, requested=requested)
    assert "data-classify-approval-note" in html
    assert ONLY_ONE_ADMIN in html
    plain = template.render(
        error=None, preview=None, form={}, requested=requested | {"approval_note": None}
    )
    assert "data-classify-approval-note" not in plain
