# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A retry key gives back the earlier request only when it was asked the same way.

A call with an ``idempotency_key`` gets the request that already holds the
key only when the people behind the login, the kind of login and the
choice to reveal secret values are the same as at the first call. The
people are compared as a set of casefolded subs, as the self-approval rule
compares them. Otherwise the call gets its own request and waits for a
person; the first request stays as it was.

Every call here runs through the real dispatch, every approve through
:func:`~meho_backplane.operations.approval_queue.approve_request`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestKey
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    IdempotencyKeyConflictError,
    SelfApprovalForbiddenError,
    approve_request,
    create_pending_request,
    reject_request,
)
from meho_backplane.settings import get_settings
from tests.test_destructive_preview_binding import _operator

from . import _reveal_fetch_helpers as reveal_helpers
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_KEY = "retry-run-1:row-1"
_LOGIN = "agent-login"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("APPROVAL_ALLOW_SELF_APPROVAL", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


#: A dangerous operation: every call of it waits for a second person.
_OP = "vault.app.rotate"


async def _rotate(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"name": "app", "password": reveal_helpers.SECRET_VALUE}


@pytest.fixture
async def vault_ops() -> None:
    await reveal_helpers.register_vault_read()
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_rotate,
        summary="Rotate the app password.",
        description="Rotate the app password.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        safety_level="dangerous",
        embedding_service=reveal_helpers._embedding_service(),
    )


def _people(*subs: str) -> list[dict[str, Any]]:
    return [{"sub": sub, "name": None, "why": "agent_owner"} for sub in subs]


def _behind(monkeypatch: pytest.MonkeyPatch, by_client: dict[str | None, list[str]]) -> None:
    """MEHO finds the people in *by_client* behind a login, by its client id."""

    async def _find(*_args: Any, **kwargs: Any) -> list[dict[str, Any]] | None:
        if kwargs["kind"] == PrincipalKind.USER.value:
            return None
        return _people(*by_client[kwargs["client_id"]])

    monkeypatch.setattr(approval_queue_module, "find_people_behind", _find)


def _agent(client_id: str = "agent:nightly") -> Operator:
    login = _operator(sub=_LOGIN, principal_kind=PrincipalKind.AGENT)
    return login.model_copy(update={"client_id": client_id})


def _person(sub: str, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return _operator(sub=sub).model_copy(update={"tenant_role": role})


async def _call(operator: Operator, *, key: str | None = _KEY, **params: Any) -> uuid.UUID:
    """Call the dangerous operation; the call waits for approval."""
    parked = await dispatch(
        operator=operator,
        connector_id=reveal_helpers.CONNECTOR_ID,
        op_id=_OP,
        target=None,
        params=params,
        idempotency_key=key,
    )
    assert parked.status == "awaiting_approval", parked.error
    return uuid.UUID(parked.extras["approval_request_id"])


async def _rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await session.execute(stmt)).scalars())


async def _approve(request_id: uuid.UUID, operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=operator)
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_a_retry_after_the_people_behind_changed_gets_its_own_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    first = await _call(_agent())

    _behind(monkeypatch, {"agent:nightly": ["person-b"]})
    retry = await _call(_agent())
    assert retry != first

    rows = await _rows()
    assert [row.id for row in rows] == [first, retry]
    assert rows[0].people_behind == _people("person-a")
    assert rows[0].status == "pending"
    assert rows[0].idempotency_key == _KEY
    assert rows[1].people_behind == _people("person-b")

    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(retry, _person("person-b"))
    await _approve(retry, _person("person-c", TenantRole.TENANT_ADMIN))
    assert [row.status for row in await _rows()] == ["pending", "approved"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_the_retry_of_the_new_request_gets_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    first = await _call(_agent())
    _behind(monkeypatch, {"agent:nightly": ["person-b"]})
    second = await _call(_agent())
    again = await _call(_agent())
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    back = await _call(_agent())

    assert again == second
    assert back == first
    assert len(await _rows()) == 2
    async with get_sessionmaker()() as session:
        links = (await session.execute(select(ApprovalRequestKey))).scalars().all()
    assert [(link.approval_request_id, link.idempotency_key) for link in links] == [(second, _KEY)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
@pytest.mark.parametrize(
    ("at_first", "at_retry"),
    [
        (["person-a"], ["person-a"]),
        (["Person-A"], ["person-a"]),
        (["person-a", "person-b"], ["person-b", "person-a"]),
    ],
)
async def test_the_same_people_behind_get_the_earlier_request(
    monkeypatch: pytest.MonkeyPatch, at_first: list[str], at_retry: list[str]
) -> None:
    _behind(monkeypatch, {"agent:nightly": at_first})
    first = await _call(_agent())
    _behind(monkeypatch, {"agent:nightly": at_retry})
    retry = await _call(_agent())

    assert retry == first
    assert len(await _rows()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_two_logins_with_one_login_name_get_two_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _behind(monkeypatch, {"agent:one": ["person-a"], "agent:two": ["person-b"]})
    one = await _call(_agent("agent:one"))
    two = await _call(_agent("agent:two"))

    assert one != two
    rows = await _rows()
    assert [row.people_behind for row in rows] == [_people("person-a"), _people("person-b")]
    assert {row.principal_sub for row in rows} == {_LOGIN}
    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(two, _person("person-b"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_a_retry_with_another_secret_choice_gets_its_own_request() -> None:
    requester = _person("requester")
    first = await _call(requester)
    revealed = await _call(requester, reveal_secret=True)
    assert revealed != first

    rows = await _rows()
    assert [(row.id, row.reveal_secret, row.status) for row in rows] == [
        (first, False, "pending"),
        (revealed, True, "pending"),
    ]
    assert rows[0].idempotency_key == _KEY

    assert await _call(requester) == first
    assert await _call(requester, reveal_secret=True) == revealed
    assert len(await _rows()) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
@pytest.mark.parametrize(
    ("first", "retry", "same"),
    [
        ({}, {"reveal_secret": "true"}, False),
        ({}, {"reveal_secret": 1}, False),
        ({"reveal_secret": True}, {"reveal_secret": "true"}, True),
        ({"reveal_secret": True}, {"reveal_secret": 1}, True),
        ({"reveal_secret": False}, {}, True),
        ({}, {"reveal_secret": ""}, True),
    ],
)
async def test_the_secret_choice_is_read_as_the_dispatcher_reads_it(
    first: dict[str, Any], retry: dict[str, Any], same: bool
) -> None:
    requester = _person("requester")
    first_id = await _call(requester, **first)
    retry_id = await _call(requester, **retry)

    assert (retry_id == first_id) is same
    assert len(await _rows()) == (1 if same else 2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
@pytest.mark.parametrize("key", ["", "k", "k" * 200, "k" * 5000])
async def test_an_empty_short_or_long_key_follows_the_same_rule(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    first = await _call(_agent(), key=key)
    assert await _call(_agent(), key=key) == first
    _behind(monkeypatch, {"agent:nightly": ["person-b"]})
    assert await _call(_agent(), key=key) != first
    assert len(await _rows()) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_a_persons_own_retry_gets_one_request_they_cannot_approve() -> None:
    requester = _person("requester")
    first = await _call(requester)
    assert await _call(requester) == first

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].people_behind is None
    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(first, requester)


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_ops")
async def test_the_same_login_as_another_kind_gets_its_own_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _behind(monkeypatch, {None: []})
    as_person = await _call(_operator(sub=_LOGIN))
    as_service = await _call(_operator(sub=_LOGIN, principal_kind=PrincipalKind.SERVICE))

    assert as_service != as_person
    assert [row.principal_kind for row in await _rows()] == ["user", "service"]


async def _park(
    operator: Operator,
    *,
    target_id: uuid.UUID | None = None,
    params: dict[str, Any] | None = None,
    reveal_secret: bool = False,
) -> ApprovalRequest:
    """Park a request with the shared key straight through ``create_pending_request``."""
    call_params = params if params is not None else {"name": "web-01"}
    target = SimpleNamespace(id=target_id) if target_id is not None else None
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="gh-rest-3",
            op_id="POST:/vms",
            target=target,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            idempotency_key=_KEY,
            reveal_secret=reveal_secret,
            proposed_effect={"safety_level": "caution"},
        )
        await session.commit()
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("asked_otherwise", ["reveal_secret", "people_behind"])
@pytest.mark.parametrize("other_call", ["target", "params", "work_ref"])
async def test_a_retry_asked_another_way_for_another_call_is_refused(
    monkeypatch: pytest.MonkeyPatch, asked_otherwise: str, other_call: str
) -> None:
    """The same key with another call is refused, also when it is asked another way."""
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    first = await _park(_agent())

    retry: dict[str, Any] = {}
    if asked_otherwise == "reveal_secret":
        retry["reveal_secret"] = True
    else:
        _behind(monkeypatch, {"agent:nightly": ["person-b"]})
    if other_call == "target":
        retry["target_id"] = uuid.uuid4()
    elif other_call == "params":
        retry["params"] = {"name": "web-02"}
    token = work_ref_var.set("change-ticket-2") if other_call == "work_ref" else None
    try:
        with pytest.raises(IdempotencyKeyConflictError) as refused:
            await _park(_agent(), **retry)
    finally:
        if token is not None:
            work_ref_var.reset(token)

    assert refused.value.request_id == first.id
    assert [(row.id, row.status, row.idempotency_key) for row in await _rows()] == [
        (first.id, "pending", _KEY)
    ]
    async with get_sessionmaker()() as session:
        assert list((await session.execute(select(ApprovalRequestKey))).scalars()) == []


@pytest.mark.asyncio
async def test_a_request_with_a_linked_key_refuses_another_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that keeps the key in its linked row refuses another call too."""
    _behind(monkeypatch, {"agent:nightly": ["person-a"]})
    first = await _park(_agent())
    _behind(monkeypatch, {"agent:nightly": ["person-b"]})
    second = await _park(_agent())
    assert second.id != first.id
    async with get_sessionmaker()() as session:
        await reject_request(session, first.id, operator=_person("reviewer"))
        await session.commit()

    _behind(monkeypatch, {"agent:nightly": ["person-c"]})
    with pytest.raises(IdempotencyKeyConflictError) as refused:
        await _park(_agent(), target_id=uuid.uuid4())

    assert refused.value.request_id == second.id
    assert [(row.id, row.status) for row in await _rows()] == [
        (first.id, "rejected"),
        (second.id, "pending"),
    ]
