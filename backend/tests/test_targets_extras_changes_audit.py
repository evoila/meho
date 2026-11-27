# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the card, the audit rows and the answer of an ``extras_changes`` call hold.

* The approval card hides every value that the card of a full ``extras``
  change hides for the same stored map and the same change.
* The audit rows, the stored result and the sender's answer name the keys
  that were set and removed, never a value of the extras.
* A call with ``extras`` and ``extras_changes`` is refused before an
  approval request is stored.
* The realm warnings of the card use the merged map.
* The instruction text names every field of the answer.

Every test value is made up at run time.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.approvals import router as approvals_router
from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.connectors.targets.ops import TARGETS_REGISTRY_CONNECTOR_ID
from meho_backplane.connectors.targets.update import (
    _LLM_INSTRUCTIONS,
    TARGETS_UPDATE_OP_ID,
    TARGETS_UPDATE_RESPONSE_SCHEMA,
    targets_update_card_warnings,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.tools.approvals import _get_handler as _mcp_get
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from tests._people_behind_helpers import give_known_person
from tests.test_targets_extras_changes import (
    _KEY,
    _NAME,
    _extras,
    _operator,
    _registered_op,  # noqa: F401 — autouse fixture
    _requests,
)

from ._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
from ._rest_write_helpers import run_writes_at_once
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


@pytest.fixture
def console() -> Iterator[TestClient]:
    yield TestClient(run_writes_at_once(_app()))


def _made(label: str) -> str:
    return f"{label}-{secrets.token_hex(6)}"


def _call(client: TestClient, method: str, path: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {_admin_token(_KEY)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.request(method, path, json=body, headers=headers)


def _patch(client: TestClient, body: dict[str, Any]) -> Any:
    return _call(client, "PATCH", f"/api/v1/targets/{_NAME}", body)


async def _approve_and_run(request_id: uuid.UUID) -> Any:
    approver = _operator("second-person", web_session=True)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=approved)


async def _card_texts(client: TestClient, request_id: str) -> list[str]:
    """The card as the stored row, the REST views and the MCP approvals tool show it."""
    shown = _call(client, "GET", f"/api/v1/approvals/{request_id}")
    listed = _call(client, "GET", "/api/v1/approvals?status=all")
    assert shown.status_code == 200, shown.text
    assert listed.status_code == 200, listed.text
    tool = await _mcp_get(_operator(), {"approval_request_id": request_id})
    [stored] = [r for r in await _requests() if str(r.id) == request_id]
    return [repr(stored.proposed_effect), shown.text, listed.text, json.dumps(tool, default=str)]


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(AuditLog))).scalars().all())


def _columns(row: Any) -> str:
    return " ".join(repr(getattr(row, c.key)) for c in row.__table__.columns)


# ---------------------------------------------------------------------------
# The approval card
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "key", "hidden"),
    [
        ({"name": "password"}, "value", True),
        ({"snmp_version": "v2c"}, "community", True),
        ({"snmp_version": None}, "community", True),
        ({"snmpPort": None, "other": 1}, "community", True),
        ({"label": "old"}, "label", False),
    ],
    ids=[
        "name-value-pair",
        "snmp-community",
        "snmp-version-null",
        "snmp-port-null",
        "plain-value",
    ],
)
async def test_the_card_hides_what_the_full_map_card_hides(
    client: TestClient, stored: dict[str, Any], key: str, hidden: bool
) -> None:
    old, new = _made("old"), _made("new")
    start = {**stored, key: old}
    row = await _insert_target(name=_NAME, extras=dict(start))
    shown_on: dict[str, list[bool]] = {}
    for form, body in (
        ("extras_changes", {"extras_changes": {key: new}}),
        ("extras", {"extras": {**start, key: new}}),
    ):
        response = _patch(client, body)
        assert response.status_code == 202, response.text
        request_id = response.json()["approval_request_id"]
        before = await _card_texts(client, request_id)
        result = await _approve_and_run(uuid.UUID(request_id))
        assert result.status == "ok", result
        after = await _card_texts(client, request_id)
        # The card of the request: the old and the new value, before and
        # after the run, in each of the four places.
        shown_on[form] = [v in text for v in (old, new) for text in before + after]
        assert await _extras(row.id) == {**start, key: new}
        # Back to the stored map for the next form.
        async with get_sessionmaker()() as session, session.begin():
            target = await session.get(type(row), row.id)
            assert target is not None
            target.extras = dict(start)

    assert shown_on["extras_changes"] == shown_on["extras"]
    if hidden:
        assert not any(shown_on["extras_changes"])
    else:
        assert all(shown_on["extras_changes"])


# ---------------------------------------------------------------------------
# Both fields are refused before a request is stored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.USER], ids=["agent", "token"])
async def test_extras_with_extras_changes_is_refused_before_a_request(
    kind: PrincipalKind,
) -> None:
    row = await _insert_target(name=_NAME, extras={"a": 1})
    if kind is PrincipalKind.AGENT:
        caller = _operator("agent:a", kind=kind, role=TenantRole.OPERATOR)
        await give_known_person(caller)
    else:
        caller = _operator("person-token")

    result = await dispatch(
        operator=caller,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_UPDATE_OP_ID,
        target=None,
        params={"target_id": str(row.id), "extras": {"b": 2}, "extras_changes": {"c": 3}},
    )

    assert result.status == "error", result
    assert (result.error or "").startswith("invalid_params"), result
    assert "not both" in str(result.extras)
    assert await _requests() == []
    assert await _extras(row.id) == {"a": 1}


# ---------------------------------------------------------------------------
# Audit rows and the stored result hold key names only
# ---------------------------------------------------------------------------


async def test_the_audit_rows_name_the_set_and_removed_keys(
    client: TestClient, console: TestClient
) -> None:
    await _insert_target(name=_NAME, extras={"a": 1, "b": 2, "c": 3})

    assert _patch(console, {"extras_changes": {"a": 5, "b": None}}).status_code == 200
    response = _patch(client, {"extras_changes": {"c": 6, "a": None, "n": 1}})
    assert response.status_code == 202, response.text
    result = await _approve_and_run(uuid.UUID(response.json()["approval_request_id"]))
    assert result.status == "ok", result

    rows = await _audit_rows()
    [console_row] = [r for r in rows if r.method == "PATCH" and r.status_code == 200]
    assert console_row.payload["extras_keys_set"] == ["a"]
    assert console_row.payload["extras_keys_removed"] == ["b"]
    [run_row] = [r for r in rows if r.method == "DISPATCH" and r.path == TARGETS_UPDATE_OP_ID]
    assert run_row.payload["extras_keys_set"] == ["c", "n"]
    assert run_row.payload["extras_keys_removed"] == ["a"]


async def test_audit_rows_and_the_stored_result_hold_no_extras_value(
    client: TestClient,
) -> None:
    sent_value, other_value, old_value = _made("sent"), _made("other"), _made("old")
    row = await _insert_target(name=_NAME, extras={"sent_key": old_value, "other_key": other_value})

    response = _patch(client, {"extras_changes": {"sent_key": sent_value, "gone_key": None}})
    assert response.status_code == 202, response.text
    request_id = response.json()["approval_request_id"]
    result = await _approve_and_run(uuid.UUID(request_id))
    assert result.status == "ok", result
    assert await _extras(row.id) == {"sent_key": sent_value, "other_key": other_value}

    rows = await _audit_rows()
    assert rows
    audit_text = " ".join(_columns(r) for r in rows)
    [request] = await _requests()
    stored_result = repr(request.resume_result)
    for value in (sent_value, other_value, old_value):
        assert value not in audit_text
        assert value not in stored_result
    assert other_value not in _columns(request)
    for name in ("sent_key", "gone_key"):
        assert name in audit_text
        assert name in stored_result

    answer = _call(client, "GET", f"/api/v1/approvals/{request_id}/result")
    assert answer.status_code == 200, answer.text
    op_answer = answer.json()["result"]["result"]
    assert op_answer["extras_keys_set"] == ["sent_key"]
    assert op_answer["extras_keys_removed"] == ["gone_key"]
    assert "extras" not in op_answer
    for value in (sent_value, other_value, old_value):
        assert value not in answer.text


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


async def test_a_call_without_extras_changes_has_the_same_answer_fields() -> None:
    row = await _insert_target(name=_NAME, extras={"a": 1})
    answers = []
    for fields in ({"extras": {"a": 2}}, {"notes": "n"}):
        result = await dispatch(
            operator=_operator(),
            connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
            op_id=TARGETS_UPDATE_OP_ID,
            target=None,
            params={"target_id": str(row.id), **fields},
            _approved=True,
        )
        assert result.status == "ok", result
        answers.append(result.result)

    base = {"target_id", "name", "product", "version", "host", "updated_fields"}
    assert set(answers[0]) == set(answers[1]) == base


def test_the_instruction_text_names_every_field_of_the_answer() -> None:
    shape = _LLM_INSTRUCTIONS["output_shape"]
    for field in TARGETS_UPDATE_RESPONSE_SCHEMA["properties"]:
        assert f"'{field}'" in shape, field


# ---------------------------------------------------------------------------
# The realm warnings of the card
# ---------------------------------------------------------------------------


async def test_the_card_realm_warnings_use_the_merged_map() -> None:
    row = await _insert_target(
        name=_NAME,
        product="keycloak",
        extras={"managed_realm": "apps", "admin_realm": "master"},
    )
    ctx = PreviewContext(
        descriptor=cast("Any", MagicMock()),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params={"target_id": str(row.id), "extras_changes": {"managed_realm": "master"}},
    )

    warnings = await targets_update_card_warnings(ctx)

    assert len(warnings) == 1
    assert "admin realm" in warnings[0]
