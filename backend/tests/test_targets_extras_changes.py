# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change one extra of a target and keep every other extra.

``extras_changes`` is the merge form of ``extras``: a value sets or replaces
one key and keeps its JSON type, ``null`` removes the key, and a key that is
not named stays. These tests cover the REST route, the wait for approval, the
checks that run on the merged map, the approval card and the operation answer.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import TARGETS_REGISTRY_CONNECTOR_ID
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    register_targets_update_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.api.v1 import targets as targets_api
from meho_backplane.targets.extras_changes import merge_extras_changes
from meho_backplane.targets.resolver import resolve_target_by_id, target_by_id_stmt
from meho_backplane.targets.schemas import TargetUpdate
from tests._people_behind_helpers import give_known_person

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._rest_write_helpers import run_writes_at_once
from ._targets_helpers import (
    _admin_token,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_KEY = make_rsa_keypair("kid-A")
_NAME = "extras-target"
_THREE = {"tags": ["a", "b"], "limits": {"cpu": 2}, "strict": True, "retries": 3}


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    request.getfixturevalue("_settings_env")
    embedder = AsyncMock()
    embedder.encode_one.return_value = [0.1] * 384
    embedder.encode.return_value = [[0.1] * 384]
    embedder.dimension = 384
    reset_dispatcher_caches()
    await register_targets_update_operation(embedding_service=embedder)
    yield
    reset_dispatcher_caches()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def console() -> Iterator[TestClient]:
    yield TestClient(run_writes_at_once(_app()))


@pytest.fixture
def token_client() -> Iterator[TestClient]:
    yield TestClient(_app())


def _patch(client: TestClient, body: dict[str, Any], *, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token or _admin_token(_KEY)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.patch(f"/api/v1/targets/{_NAME}", json=body, headers=headers)


def _operator(
    sub: str = "p-1",
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    web_session: bool = False,
    platform_admin: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
        platform_admin=platform_admin,
    )


async def _extras(target_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return dict(row.extras)


async def _run(operator: Operator, target_id: uuid.UUID, **fields: Any) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_UPDATE_OP_ID,
        target=None,
        params={"target_id": str(target_id), **fields},
        _approved=True,
    )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


# ---------------------------------------------------------------------------
# The merge on its own, and the model
# ---------------------------------------------------------------------------


def test_merge_sets_replaces_and_removes_and_keeps_the_rest() -> None:
    merged = merge_extras_changes(
        _THREE, {"retries": 5, "tags": None, "new": {"a": [1]}, "gone": None}
    )

    assert merged == {"limits": {"cpu": 2}, "strict": True, "retries": 5, "new": {"a": [1]}}
    assert _THREE["retries"] == 3


def test_merge_into_no_extras() -> None:
    assert merge_extras_changes(None, {"a": False}) == {"a": False}


@pytest.mark.parametrize("value", [{}, None], ids=["empty", "null"])
def test_a_body_with_an_empty_or_null_change_is_refused(value: Any) -> None:
    with pytest.raises(ValidationError):
        TargetUpdate(extras_changes=value)


def test_a_body_with_extras_and_extras_changes_is_refused() -> None:
    with pytest.raises(ValidationError, match="not both"):
        TargetUpdate(extras={"a": 1}, extras_changes={"b": 2})


# ---------------------------------------------------------------------------
# REST PATCH
# ---------------------------------------------------------------------------


async def test_rest_change_keeps_every_other_key_with_its_type(console: TestClient) -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))
    # A key added by an earlier update.
    async with get_sessionmaker()() as session, session.begin():
        stored = await session.get(TargetORM, row.id)
        assert stored is not None
        stored.extras = {**stored.extras, "added_before": "yes"}

    response = _patch(console, {"extras_changes": {"retries": 7, "strict": None}})

    assert response.status_code == 200, response.text
    expected = {"tags": ["a", "b"], "limits": {"cpu": 2}, "retries": 7, "added_before": "yes"}
    assert response.json()["extras"] == expected
    assert await _extras(row.id) == expected
    assert isinstance((await _extras(row.id))["tags"], list)


async def test_rest_change_with_extras_and_extras_changes_changes_nothing(
    console: TestClient,
) -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))

    response = _patch(console, {"extras": {"x": 1}, "extras_changes": {"y": 2}})

    assert response.status_code == 422, response.text
    assert "not both" in response.text
    assert await _extras(row.id) == _THREE


async def test_the_whole_map_form_replaces(console: TestClient) -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))

    response = _patch(console, {"extras": {"only": "this"}})

    assert response.status_code == 200, response.text
    assert await _extras(row.id) == {"only": "this"}


@pytest.mark.parametrize("field", ["extras_changes", "extras"])
@pytest.mark.parametrize(
    ("before", "after"),
    [(1, True), (0, False), (True, 1), (1, 1.0)],
    ids=["1-to-true", "0-to-false", "true-to-1", "1-to-1.0"],
)
async def test_a_change_of_only_the_json_type_is_stored(
    console: TestClient, field: str, before: Any, after: Any
) -> None:
    row = await _insert_target(name=_NAME, extras={"flag": before, "keep": "k"})
    sent = {"flag": after} if field == "extras_changes" else {"flag": after, "keep": "k"}

    response = _patch(console, {field: sent})

    assert response.status_code == 200, response.text
    stored = await _extras(row.id)  # a new database session
    assert stored == {"flag": after, "keep": "k"}
    assert type(stored["flag"]) is type(after)


def test_the_locked_read_holds_for_update_on_postgresql() -> None:
    stmt = target_by_id_stmt(_TENANT_ID, uuid.uuid4(), for_update=True)

    assert "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect()))
    assert stmt.get_execution_options().get("populate_existing") is True


async def test_the_console_change_merges_into_the_extras_of_the_locked_read(
    console: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await _insert_target(name=_NAME, extras={"a": 1})
    locked: list[bool] = []

    async def other_change_then_lock(session: Any, tenant_id: Any, target_id: Any, **kw: Any) -> Any:
        if kw.get("for_update"):
            locked.append(True)
            # Another change of a different key commits after the first read
            # of this request and before its lock. The row the request holds
            # in memory does not see it.
            await session.execute(
                update(TargetORM)
                .where(TargetORM.id == target_id)
                .values(extras={"a": 1, "other": "kept"})
                .execution_options(synchronize_session=False)
            )
        return await resolve_target_by_id(session, tenant_id, target_id, **kw)

    monkeypatch.setattr(targets_api, "resolve_target_by_id", other_change_then_lock)

    response = _patch(console, {"extras_changes": {"mine": True}})

    assert response.status_code == 200, response.text
    assert locked == [True]
    assert await _extras(row.id) == {"a": 1, "other": "kept", "mine": True}


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def test_a_token_change_waits_and_a_key_added_meanwhile_stays(
    token_client: TestClient,
) -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))

    response = _patch(token_client, {"extras_changes": {"retries": 9, "tags": None}})

    assert response.status_code == 202, response.text
    request_id = uuid.UUID(response.json()["approval_request_id"])
    [request] = await _requests()
    assert request.params == {
        "target_id": str(row.id),
        "extras_changes": {"retries": 9, "tags": None},
    }
    assert await _extras(row.id) == _THREE

    # While the request waits, another change adds a key.
    async with get_sessionmaker()() as session, session.begin():
        stored = await session.get(TargetORM, row.id)
        assert stored is not None
        stored.extras = {**stored.extras, "added_while_waiting": [1, 2]}

    approver = _operator("second-person", web_session=True)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver)
        await session.commit()
    result = await resume_dispatch_after_approval(operator=approver, request=approved)

    assert result.status == "ok", result
    assert await _extras(row.id) == {
        "limits": {"cpu": 2},
        "strict": True,
        "retries": 9,
        "added_while_waiting": [1, 2],
    }


async def test_the_card_names_the_changed_keys_and_hides_a_secret_value() -> None:
    row = await _insert_target(name=_NAME, extras={"retries": 3, "old": "x"})
    agent = _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR)
    await give_known_person(agent)
    canary = "canary-secret-value-must-not-show"

    call = await call_operation(
        agent,
        {
            "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
            "op_id": TARGETS_UPDATE_OP_ID,
            "params": {
                "target_id": str(row.id),
                "extras_changes": {"retries": 4, "old": None, "api_password": canary},
            },
        },
    )

    assert call["status"] == "awaiting_approval", call
    [request] = await _requests()
    assert request.proposed_effect is not None
    preview = request.proposed_effect["preview"]
    assert preview["fields"] == ["extras_changes"]
    changes = preview["changes"]["extras_changes"]
    assert changes["retries"] == {"from": 3, "to": 4}
    assert changes["old"] == {"from": "x", "to": None}
    assert set(changes) == {"retries", "old", "api_password"}
    assert canary not in str(preview)
    assert await _extras(row.id) == {"retries": 3, "old": "x"}


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


async def test_the_operation_answer_shows_the_extras_after_the_change() -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))

    result = await _run(_operator(), row.id, extras_changes={"retries": 4})

    assert result.status == "ok", result
    assert result.result["extras"] == {**_THREE, "retries": 4}
    assert result.result["updated_fields"] == ["extras_changes"]


async def test_the_operation_refuses_extras_with_extras_changes() -> None:
    row = await _insert_target(name=_NAME, extras=dict(_THREE))

    result = await _run(_operator(), row.id, extras={"a": 1}, extras_changes={"b": 2})

    assert result.status != "ok", result
    assert await _extras(row.id) == _THREE


# ---------------------------------------------------------------------------
# Checks on the merged map
# ---------------------------------------------------------------------------


async def test_the_size_limit_refuses_a_merged_map_that_is_too_big() -> None:
    big = {f"k{i}": i for i in range(900)}
    row = await _insert_target(name=_NAME, extras=big)

    result = await _run(_operator(), row.id, extras_changes={f"n{i}": i for i in range(200)})

    assert result.status != "ok", result
    assert await _extras(row.id) == big


@pytest.mark.parametrize("changes", [{"vault_role": "narrow-role"}, {"vault_mount": "m"}])
async def test_vault_login_keys_need_a_platform_admin(changes: dict[str, Any]) -> None:
    row = await _insert_target(name=_NAME, product="vault", extras={"a": 1})

    refused = await _run(_operator(), row.id, extras_changes=changes)
    assert refused.status != "ok", refused
    assert await _extras(row.id) == {"a": 1}

    allowed = await _run(_operator(platform_admin=True), row.id, extras_changes=changes)
    assert allowed.status == "ok", allowed
    assert await _extras(row.id) == {"a": 1, **changes}


async def test_removing_a_vault_login_key_needs_a_platform_admin() -> None:
    row = await _insert_target(name=_NAME, product="vault", extras={"vault_role": "r", "a": 1})

    refused = await _run(_operator(), row.id, extras_changes={"vault_role": None})

    assert refused.status != "ok", refused
    assert await _extras(row.id) == {"vault_role": "r", "a": 1}


@pytest.mark.parametrize("changes", [{"managed_realm": None}, {"managed_realm": "a/b"}])
async def test_the_keycloak_realm_check_runs_on_the_merged_map(changes: dict[str, Any]) -> None:
    start = {"managed_realm": "lab", "admin_realm": "master"}
    row = await _insert_target(name=_NAME, product="keycloak", extras=dict(start))

    result = await _run(_operator(), row.id, extras_changes=changes)

    assert result.status != "ok", result
    assert await _extras(row.id) == start


async def test_a_keycloak_realm_can_be_corrected_with_one_key() -> None:
    row = await _insert_target(
        name=_NAME, product="keycloak", extras={"managed_realm": "wrong", "admin_realm": "master"}
    )

    result = await _run(_operator(), row.id, extras_changes={"managed_realm": "right"})

    assert result.status == "ok", result
    assert await _extras(row.id) == {"managed_realm": "right", "admin_realm": "master"}
