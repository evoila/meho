# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval surfaces name the people behind an agent, service or runner login.

REST (``ApprovalRequestView.people_behind`` and ``people_behind_known``)
returns them, and the console shows them on the request card and in the
request modal. When MEHO knows nobody behind the login, every surface says
"not known". A person's request shows nothing extra. REST shows the list
only to a person; an agent, service or runner login reads ``None``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from sqlalchemy import update

from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from tests.test_ui_approvals import (
    _HX_HEADERS,
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_PEOPLE = [
    {"sub": "sub-ann", "name": "Ann Admin", "why": "registered"},
    {"sub": "sub-bob", "name": None, "why": "gave_grant"},
]


@pytest.fixture(autouse=True)
def _tenant() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")


def _seed(*, kind: str | None, people: list[dict[str, Any]] | None) -> uuid.UUID:
    rid = _seed_request(tenant_id=_TENANT_A)

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == rid)
                .values(principal_kind=kind, people_behind=people)
            )

    asyncio.run(_do())
    return rid


def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async def _do() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row

    return asyncio.run(_do())


def _modal(request_id: uuid.UUID) -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{request_id}")
    assert response.status_code == 200, response.text
    return str(response.text)


def _panel() -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)
    assert response.status_code == 200, response.text
    return str(response.text)


def test_rest_view_names_the_people_behind() -> None:
    reader = _operator(tenant_id=_TENANT_A)
    known = _view(_load(_seed(kind="agent", people=_PEOPLE)), operator=reader)
    unknown = _view(_load(_seed(kind="service", people=[])), operator=reader)
    person = _view(_load(_seed(kind="user", people=None)), operator=reader)

    assert known.people_behind_known is True
    assert known.people_behind is not None
    assert [(p.sub, p.name, p.why, p.why_text) for p in known.people_behind] == [
        ("sub-ann", "Ann Admin", "registered", "Set up by"),
        ("sub-bob", None, "gave_grant", "Grant by"),
    ]
    assert unknown.people_behind_known is False
    assert unknown.people_behind == []
    assert person.people_behind is None
    assert person.people_behind_known is None


def test_rest_view_hides_the_people_behind_from_an_agent_login() -> None:
    reader = _operator(tenant_id=_TENANT_A).model_copy(
        update={"principal_kind": PrincipalKind.AGENT}
    )
    row = _load(_seed(kind="agent", people=_PEOPLE))

    view = _view(row, operator=reader)
    assert view.people_behind is None
    assert view.people_behind_known is None


def test_modal_and_card_name_the_people_behind_an_agent() -> None:
    rid = _seed(kind="agent", people=_PEOPLE)

    modal = _modal(rid)
    panel = _panel()

    assert "data-approval-people-behind" in modal
    assert "Ann Admin" in modal
    assert "sub-bob" in modal
    assert "Set up by Ann Admin. Grant by sub-bob." in panel


def test_modal_and_card_say_not_known_when_nobody_is_known() -> None:
    rid = _seed(kind="agent", people=[])

    assert "data-approval-people-behind-unknown" in _modal(rid)
    assert "Set up by: not known. Only an Admin may approve." in _panel()


def test_a_person_request_shows_no_people_behind() -> None:
    rid = _seed(kind="user", people=None)

    assert "data-approval-people-behind" not in _modal(rid)
    assert "data-approval-people-behind-line" not in _panel()
