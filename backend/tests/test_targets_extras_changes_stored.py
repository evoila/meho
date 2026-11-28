# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The stored request of an ``extras_changes`` call, after its run or its end.

When the request runs, and when it ends without a run (rejected, withdrawn,
expired), its stored ``params`` keep the key names of ``extras_changes`` and
no value. A run again without params then needs the params, as for a
request with a hidden password.

Every test value is made up at run time.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.approvals import router as approvals_router
from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.broadcast.events import HIDDEN_PARAM_MARKER
from meho_backplane.connectors.targets.update import TARGETS_UPDATE_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.approval_queue import (
    approve_request,
    expire_stale_requests,
    params_with_secret_fields_hidden,
    reject_request,
    resume_dispatch_after_approval,
    withdraw_request,
)
from meho_backplane.operations.approval_rerun import (
    RerunParamsRequiredError,
    rerun_failed_request,
)
from tests.test_targets_extras_changes import (
    _KEY,
    _NAME,
    _extras,
    _operator,
    _registered_op,  # noqa: F401 — autouse fixture
    _requests,
)

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _admin_token,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    app.include_router(approvals_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_app())


def _made(label: str) -> str:
    return f"{label}-{secrets.token_hex(6)}"


def _park(client: TestClient, changes: dict[str, Any]) -> uuid.UUID:
    """A token change of the target: it waits for approval."""
    headers = {"Authorization": f"Bearer {_admin_token(_KEY)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        response = client.patch(
            f"/api/v1/targets/{_NAME}", json={"extras_changes": changes}, headers=headers
        )
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["approval_request_id"])


async def _request(request_id: uuid.UUID) -> ApprovalRequest:
    [request] = [r for r in await _requests() if r.id == request_id]
    return request


def _requester(request: ApprovalRequest) -> Any:
    return _operator(request.principal_sub)


async def _approve_and_run(request_id: uuid.UUID) -> Any:
    approver = _operator("second-person", web_session=True)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=approved)


async def _reject(request_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        await reject_request(session, request_id, operator=_operator("second-person"))
        await session.commit()


async def _withdraw(request_id: uuid.UUID) -> None:
    request = await _request(request_id)
    async with get_sessionmaker()() as session:
        await withdraw_request(session, request_id, operator=_requester(request))
        await session.commit()


async def _expire() -> None:
    async with get_sessionmaker()() as session:
        expired = await expire_stale_requests(
            session, operator=_operator("sweeper"), now=datetime.now(UTC) + timedelta(days=30)
        )
        await session.commit()
    assert expired


_CASES = [
    pytest.param({"name": "password"}, "value", id="name-value-pair"),
    pytest.param({"name": "password", "value": "<old>"}, "value", id="name-value-old-value"),
    pytest.param({"name": "password", "note": None}, "value", id="name-value-null-sibling"),
    pytest.param({"snmp_version": "v2c"}, "community", id="snmp-community"),
    pytest.param({"snmp_version": None}, "community", id="snmp-version-null"),
]


def _with_old_value(stored: dict[str, Any]) -> tuple[dict[str, Any], str]:
    old = _made("old")
    return {k: old if v == "<old>" else v for k, v in stored.items()}, old


@pytest.mark.parametrize("end", ["run", "reject", "withdraw", "expire"])
@pytest.mark.parametrize(("stored", "key"), _CASES)
async def test_the_stored_params_keep_key_names_and_no_value(
    client: TestClient, stored: dict[str, Any], key: str, end: str
) -> None:
    start, old = _with_old_value(stored)
    new = _made("new")
    row = await _insert_target(name=_NAME, extras=dict(start))

    request_id = _park(client, {key: new, "gone_key": None})
    # While the request waits, it holds the call it will run.
    assert (await _request(request_id)).params["extras_changes"][key] == new  # type: ignore[index]
    if end == "run":
        result = await _approve_and_run(request_id)
        assert result.status == "ok", result
        assert await _extras(row.id) == {**start, key: new}
    elif end == "reject":
        await _reject(request_id)
    elif end == "withdraw":
        await _withdraw(request_id)
    else:
        await _expire()

    params = (await _request(request_id)).params
    assert params is not None
    assert params["extras_changes"] == {key: HIDDEN_PARAM_MARKER, "gone_key": None}
    assert params["target_id"] == str(row.id)
    for value in (new, old):
        assert value not in repr(params)


def test_the_cleanup_hides_every_value_and_keeps_the_names() -> None:
    plain, number = _made("plain"), 7
    params = {
        "target_id": str(uuid.uuid4()),
        "extras_changes": {"a": plain, "b": number, "c": [plain], "d": None},
    }

    hidden = params_with_secret_fields_hidden(params, op_id=TARGETS_UPDATE_OP_ID)

    assert hidden == {
        "target_id": params["target_id"],
        "extras_changes": {
            "a": HIDDEN_PARAM_MARKER,
            "b": HIDDEN_PARAM_MARKER,
            "c": HIDDEN_PARAM_MARKER,
            "d": None,
        },
    }
    # A removal only holds no value, so there is nothing to hide.
    only_removal = {"target_id": params["target_id"], "extras_changes": {"d": None}}
    assert params_with_secret_fields_hidden(only_removal, op_id=TARGETS_UPDATE_OP_ID) is None


async def test_a_run_again_without_params_needs_the_params(client: TestClient) -> None:
    row = await _insert_target(
        name=_NAME, product="keycloak", extras={"managed_realm": "lab", "admin_realm": "master"}
    )
    new = _made("new")
    request_id = _park(client, {"other_key": new})
    # A realm that the realm check refuses is stored while the call waits,
    # so the run fails.
    broken = {"managed_realm": "a/b", "admin_realm": "master"}
    async with get_sessionmaker()() as session, session.begin():
        target = await session.get(type(row), row.id)
        assert target is not None
        target.extras = dict(broken)

    result = await _approve_and_run(request_id)
    assert result.status != "ok", result
    request = await _request(request_id)
    assert request.params is not None
    assert request.params["extras_changes"] == {"other_key": HIDDEN_PARAM_MARKER}

    with pytest.raises(RerunParamsRequiredError):
        await rerun_failed_request(operator=_requester(request), request_id=request_id)
    assert await _extras(row.id) == broken
