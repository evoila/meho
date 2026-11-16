# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``POST /api/v1/policy/explain`` (``meho policy explain``).

* One answer per verdict: run, wait, deny. Each names today's answer and,
  with ``POLICY_ENGINE_SHADOW`` on, the evaluator's rules in order and
  the floor that decided.
* With the switch off, the answer is today's answer only.
* A policy file that is not imported changes the answer and names its
  entries as ``file:...``; nothing is stored.
* A stored approval request answers with the rule stored on it.
* Who may ask, and the questions that cannot be asked.
* Explain writes no audit row for the call and no approval request.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.classification import ClassificationEntry
from meho_backplane.policy.explain import FILE_NEEDS_CLASSIFICATION, _wait_path
from meho_backplane.policy.explain_asker import READ_ONLY_CANNOT_CALL
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_CONNECTOR = "explapi-1.x"
_SAFE = "explapi.vm.list"
_CAUTION = "explapi.vm.restart"
_DESTRUCTIVE = "explapi.vm.delete"


@pytest.fixture
def shadow(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def classification(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _setup(embedding: AsyncMock) -> None:
    for op_id, level in ((_SAFE, "safe"), (_CAUTION, "caution"), (_DESTRUCTIVE, "destructive")):
        await register_typed_operation(
            product="explapi",
            version="1.x",
            impl_id="explapi",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=level,  # type: ignore[arg-type]
            requires_approval=level == "destructive",
            embedding_service=embedding,
        )


def _token(key: Any, *, sub: str = "admin-a", **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(_TENANT), **claims)


def _ask(client: TestClient, token: str, **body: Any) -> Any:
    return client.post(
        "/api/v1/policy/explain", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def _groups(op_id: str, *groups: str, **extra: Any) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR, "op_id": op_id, "groups": list(groups), **extra}


async def _count(model: Any, *where: Any) -> int:
    async with get_sessionmaker()() as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(model).where(*where))
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# One answer per verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_run_names_the_rules_in_order(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-run")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(client, _token(key), **_groups(_SAFE, "Operators")).json()
    assert body["status"] == "ok", body
    assert body["engine"] == "shadow"
    assert body["answer"] == "run"
    assert body["decided_by"] == "today"
    assert body["approvers"] is None
    assert body["today"] == {"verdict": "run", "policy_verdict": "auto-execute", "reason": None}
    evaluator = body["evaluator"]
    assert evaluator["verdict"] == "run"
    assert evaluator["same_as_today"] is True
    assert evaluator["floor"] is None
    assert evaluator["rule"]["rule_id"] == "person.safe"
    assert [rule["rule_id"] for rule in evaluator["rules"]] == ["person.safe"]
    assert body["asker"] == {
        "me": False,
        "kind": "user",
        "role": "operator",
        "groups": ["MEHO-Operators"],
        "via": "token",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_wait_names_who_may_approve(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-wait")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token_call = _ask(client, _token(key), **_groups(_CAUTION, "Operators")).json()
        web_call = _ask(client, _token(key), **_groups(_CAUTION, "Operators", via="web")).json()
    assert token_call["answer"] == "wait", token_call
    assert token_call["approvers"] == {
        "groups": ["MEHO-Operators", "MEHO-SA-Operators", "MEHO-Admins", "MEHO-Approvers"],
        "rule": "default",
    }
    assert token_call["evaluator"]["verdict"] == "wait"
    assert token_call["evaluator"]["floor"]["rule_id"] == "token_runs_only_safe"
    # The same person in the web interface runs a caution call at once.
    assert web_call["answer"] == "run", web_call
    assert web_call["asker"]["via"] == "web"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_a_destructive_call_waits_on_a_floor(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-floor")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(client, _token(key), **_groups(_DESTRUCTIVE, "Admins", via="web")).json()
    assert body["answer"] == "wait", body
    assert body["approvers"]["groups"] == ["MEHO-SA-Operators", "MEHO-Admins"]
    floor = body["evaluator"]["floor"]
    assert floor["source"] == "floor"
    assert floor == body["evaluator"]["rule"]
    assert body["classification"]["risk_level"] == "destructive"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_deny_for_an_agent_without_a_grant(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "on")
    get_settings.cache_clear()
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-deny")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(client, _token(key), **_groups(_SAFE, "Admins", principal="agent")).json()
    assert body["answer"] == "deny", body
    assert body["approvers"] is None
    assert body["asker"]["kind"] == "agent"
    # An agent acts with at most the operator role, as a real agent token.
    assert body["asker"]["role"] == "operator"
    assert body["evaluator"]["rule"]["rule_id"] == "agent_no_grant"
    assert body["evaluator"]["same_as_today"] is True


@pytest.mark.asyncio
async def test_with_the_switch_off_only_todays_answer(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(
            client,
            _token(key, sub="op-a", tenant_role="operator"),
            connector_id=_CONNECTOR,
            op_id=_CAUTION,
            me=True,
        ).json()
    assert body["engine"] == "off"
    assert body["evaluator"] is None
    assert body["answer"] == "wait"
    assert body["today"]["policy_verdict"] == "needs-approval"
    assert body["asker"]["me"] is True
    # An operator sees who may approve, not the rule that says so.
    assert body["approvers"]["rule"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_explain_writes_no_audit_row_and_no_request(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-nowrite")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        for op_id in (_SAFE, _CAUTION, _DESTRUCTIVE):
            assert _ask(client, _token(key), **_groups(op_id, "Operators")).status_code == 200
    # The REST request may write its own row; no call row and no policy decision.
    assert await _count(AuditLog, AuditLog.path != "/api/v1/policy/explain") == 0
    assert await _count(AuditLog, AuditLog.policy_decision.is_not(None)) == 0
    assert await _count(AuditLog, AuditLog.policy_rule.is_not(None)) == 0
    assert await _count(ApprovalRequest) == 0


@pytest.mark.asyncio
async def test_an_unknown_op_or_target_is_an_error_answer(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-unknown")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        unknown_op = _ask(client, _token(key), **_groups("explapi.vm.nope", "Operators")).json()
        unknown_target = _ask(
            client, _token(key), **_groups(_SAFE, "Operators", target="<no-such-target>")
        ).json()
    assert unknown_op["status"] == "error", unknown_op
    assert unknown_op["answer"] is None
    assert unknown_op["extras"]["error_code"] in {"unknown_op", "unknown_connector"}
    assert unknown_target["status"] == "error", unknown_target
    assert unknown_target["extras"]["error_code"] == "no_target"


# ---------------------------------------------------------------------------
# A policy file that is not imported
# ---------------------------------------------------------------------------

_WAIT_FILE = (
    "policy: v1\n"
    "classifications:\n"
    "  - {match: {connector: explapi-1.x}, category: compute}\n"
    "  - {match: {op: explapi.vm.list}, always_wait: true}\n"
)


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow", "classification")
async def test_a_file_that_is_not_imported_changes_the_answer(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-file")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        stored = _ask(client, _token(key), **_groups(_SAFE, "Operators")).json()
        asked = _ask(client, _token(key), **_groups(_SAFE, "Operators", file=_WAIT_FILE)).json()
    assert stored["answer"] == "run", stored
    assert stored["classification"]["entries"] == []
    assert asked["file"] is True
    assert asked["answer"] == "wait", asked
    classification = asked["classification"]
    assert classification["requires_approval"] is True
    assert classification["category"] == "compute"
    assert [entry["path"] for entry in classification["entries"]] == [
        "file:classifications[0]",
        "file:classifications[1]",
    ]
    wait_rules = [rule for rule in asked["evaluator"]["rules"] if rule["rule_id"] == "always_wait"]
    assert wait_rules and wait_rules[0]["path"] == "file:classifications[1]"
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(TenantPolicy))).first() is None


def test_the_wait_names_the_rule_that_causes_it_not_an_overridden_one() -> None:
    # A more specific install rule ends the install group's wait; the tenant rule waits.
    install_group = ClassificationEntry(
        ref="install:classifications[0]", scope="install", group="vm", always_wait=True
    )
    install_op = ClassificationEntry(
        ref="install:classifications[1]", scope="install", op="explapi.vm.list", always_wait=False
    )
    tenant_connector = ClassificationEntry(
        ref="tenant:classifications[0]", scope="tenant", connector="explapi-1.x", always_wait=True
    )
    matched = [install_group, install_op, tenant_connector]
    assert _wait_path(matched) == "tenant:classifications[0]"
    assert _wait_path([install_group, install_op]) is None
    assert _wait_path([install_group, tenant_connector]) == "install:classifications[0]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("classification")
async def test_a_file_with_mistakes_is_422(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    bad = "policy: v1\nclassifications:\n  - {match: {op: x}, colour: red}\n"
    key = make_rsa_keypair("kid-expl-badfile")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _ask(client, _token(key), **_groups(_SAFE, "Operators", file=bad))
    assert resp.status_code == 422
    assert resp.json()["detail"]["mistakes"] == ["`classifications[0].colour` is not a known key."]


@pytest.mark.asyncio
async def test_a_file_needs_the_classification_switch(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-fileoff")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _ask(client, _token(key), **_groups(_SAFE, "Operators", file=_WAIT_FILE))
    assert resp.status_code == 409
    assert resp.json()["detail"] == FILE_NEEDS_CLASSIFICATION


# ---------------------------------------------------------------------------
# A stored approval request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("shadow")
async def test_a_request_answers_with_its_stored_rule(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    caller = Operator(
        sub="op-a",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )
    result = await dispatch(
        operator=caller, connector_id=_CONNECTOR, op_id=_CAUTION, target=None, params={}
    )
    assert result.status == "awaiting_approval", result
    request_id = result.extras["approval_request_id"]
    key = make_rsa_keypair("kid-expl-request")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _ask(client, _token(key), request_id=str(request_id)).json()
        missing = _ask(client, _token(key), request_id=str(uuid.uuid4()))
        mixed = _ask(client, _token(key), request_id=str(request_id), op_id=_SAFE)
        reader = _ask(
            client, _token(key, sub="reader", tenant_role="read_only"), request_id=str(request_id)
        )
    stored = body["request"]
    assert stored["op_id"] == _CAUTION
    assert stored["status"] == "pending"
    assert stored["policy_rule"]["decided"] is False
    assert stored["policy_rule"]["rule_id"] == "token_runs_only_safe"
    assert stored["approvers"]["rule"] == "default"
    assert body["answer"] is None
    assert missing.status_code == 404
    assert mixed.status_code == 422
    assert reader.status_code == 403


# ---------------------------------------------------------------------------
# Who may ask, and questions that cannot be asked
# ---------------------------------------------------------------------------


def test_explain_needs_a_token(client: TestClient) -> None:
    assert client.post("/api/v1/policy/explain", json={"me": True}).status_code == 401


@pytest.mark.asyncio
async def test_who_may_ask_what(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-who")
    me = {"connector_id": _CONNECTOR, "op_id": _SAFE, "me": True}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        operator = _token(key, sub="op-a", tenant_role="operator")
        reader = _token(key, sub="reader", tenant_role="read_only")
        agent = _token(key, sub="agent-a", tenant_role="operator", principal_kind="agent")
        assert _ask(client, operator, **me).status_code == 200
        assert _ask(client, agent, **me).json()["asker"]["kind"] == "agent"
        assert _ask(client, reader, **me).status_code == 403
        # Asking for other groups or with a file needs a policy admin.
        assert _ask(client, operator, **_groups(_SAFE, "Operators")).status_code == 403
        # A machine login has no web session to ask about.
        assert _ask(client, agent, **me, via="web").status_code == 422


@pytest.mark.asyncio
async def test_questions_that_cannot_be_asked(client: TestClient, embedding: AsyncMock) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-expl-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        no_op = _ask(client, token, me=True)
        both = _ask(client, token, **_groups(_SAFE, "Operators", me=True))
        neither = _ask(client, token, connector_id=_CONNECTOR, op_id=_SAFE)
        unknown = _ask(client, token, **_groups(_SAFE, "Pilots"))
        readers = _ask(client, token, **_groups(_SAFE, "Readers", "MEHO-Approvers"))
        principal_me = _ask(
            client, token, connector_id=_CONNECTOR, op_id=_SAFE, me=True, principal="agent"
        )
        extra = _ask(client, token, **_groups(_SAFE, "Operators", colour="red"))
    for resp in (no_op, both, neither, unknown, readers, principal_me, extra):
        assert resp.status_code == 422, resp.text
    assert "Pilots" in unknown.json()["detail"]
    assert readers.json()["detail"] == READ_ONLY_CANNOT_CALL
