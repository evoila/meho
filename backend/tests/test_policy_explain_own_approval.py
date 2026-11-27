# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``own_approval`` of ``POST /api/v1/policy/explain`` says what the approve does.

For each caller, each risk level and both settings of
``APPROVAL_ALLOW_SELF_APPROVAL``: explain through the real route, then the
real call, then the real approve of that request by the same login, once
with a click in the web console and once with a token. The approve must do
what the part said: accepted, or refused with the same reason. For these
operations the part is never ``null``.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.main import app
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    register_preview_builder,
)
from meho_backplane.operations.approval_queue import NonHumanApprovalError
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.operations.grant_approval import register_rights_change_op
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._own_approval_cases import (
    ADMIN_TOKEN,
    AGENT,
    ALL_CALLERS,
    NOT_A_PERSON,
    OPERATOR_TOKEN,
    OPERATOR_WEB,
    OPS,
    SAFE_MARKED,
    SERVICE,
    Caller,
    Row,
    agrees,
    approve,
    embedding_service,
    isolate_registries,
    op,
    park,
    register,
    register_levels,
    row_of,
    stored,
    switches,
    table,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_SETTINGS = ["false", "true"]


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    return embedding_service()


async def _blast_radius(ctx: PreviewContext) -> dict[str, Any]:
    """A destructive call waits only with this block on its preview."""
    return {
        "blast_radius": {
            "object": {"kind": "vm", "id": "vm-1"},
            "children": [],
            "irreversibility": "permanent",
        }
    }


@pytest.fixture(autouse=True)
def _builders() -> Iterator[None]:
    saved = dict(_PREVIEW_BUILDERS)
    register_preview_builder(op("destructive"), _blast_radius)
    yield
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(saved)


async def _rows(
    client: TestClient, key: Any, callers: tuple[Caller, ...], ops: tuple[str, ...]
) -> list[Row]:
    return [await row_of(client, key, caller, op_id) for caller in callers for op_id in ops]


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
async def test_own_approval_says_what_the_approve_does(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    await register_levels(embedding)
    key = make_rsa_keypair(f"kid-ownappr-{self_approval}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key, ALL_CALLERS, tuple(op(name) for name in OPS))
    print(f"\nAPPROVAL_ALLOW_SELF_APPROVAL={self_approval}\n{table(rows)}")
    assert [line for row in rows for line in agrees(row)] == []
    on = self_approval == "true"
    by = {(row.caller.name, row.op_id): row.own for row in rows}
    # A call that runs at once has no part.
    assert by[(OPERATOR_WEB.name, op("caution"))] is None
    # A caution call that waits only for its token: a click approves it.
    token_wait = by[(OPERATOR_TOKEN.name, op("caution"))]
    assert token_wait is not None and token_wait["web"] is True and token_wait["token"] is on
    # The own-approval setting never covers dangerous or destructive.
    for level in ("dangerous", "destructive"):
        own = by[(OPERATOR_TOKEN.name, op(level))]
        assert own is not None and not own["web"] and not own["token"]
    # With CUSTOM_POLICY on, a safe change with only the plain mark counts as
    # dangerous, so the own-approval setting does not cover it either.
    marked = by[(OPERATOR_TOKEN.name, op(SAFE_MARKED))]
    assert marked is not None and not marked["web"] and not marked["token"]
    # A MEHO-Admin approves their own change at every level.
    admin = by[(ADMIN_TOKEN.name, op("destructive"))]
    assert admin is not None and admin["web"] and admin["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
@pytest.mark.parametrize("caller", [AGENT, SERVICE], ids=["agent", "service"])
async def test_a_machine_login_reads_the_refusal_of_the_approve(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
    caller: Caller,
) -> None:
    """The approve refuses a machine login; the part says the text of the approve sign."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    await register_levels(embedding)
    key = make_rsa_keypair(f"kid-ownappr-machine-{self_approval}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key, (caller,), tuple(op(name) for name in OPS))
    assert [row.op_id for row in rows if row.answer == "run"] == [op("safe")]
    for row in rows:
        if row.answer == "run":
            assert row.own is None
            continue
        assert row.answer == "wait" and row.own is not None and row.request_id is not None
        request = await stored(row.request_id)
        for way, web in (("web", True), ("token", False)):
            done = row.done[way]
            assert type(done.error) is NonHumanApprovalError, (row.where, way, done)
            assert str(done.error) == (
                f"principal {caller.sub!r} of kind {caller.kind.value!r} may not decide an "
                "approval request — approval is a human decision (v0.1-spec §7)"
            )
            sign = approve_right(caller.operator(web=web), request)
            assert sign.may_approve is False
            assert row.own[way] is False, (row.where, way)
            assert row.own[f"{way}_reason"] == sign.message == NOT_A_PERSON, (row.where, way)


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
async def test_a_change_of_rights_for_every_call_says_what_the_approve_does(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
) -> None:
    """Only an admin approves it, and only with a click in the web console."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    isolate_registries(monkeypatch)
    ops = ("demo.rights.caution", "demo.rights.dangerous")
    for op_id in ops:
        register_rights_change_op(op_id)
        await register(embedding, op_id, op_id.rsplit(".", 1)[1])
    key = make_rsa_keypair(f"kid-ownappr-rights-{self_approval}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key, ALL_CALLERS, ops)
    print(f"\nAPPROVAL_ALLOW_SELF_APPROVAL={self_approval}\n{table(rows)}")
    assert len(rows) == 16
    assert [line for row in rows for line in agrees(row)] == []
    by = {(row.caller.name, row.op_id): row.own for row in rows}
    admin, operator = by[(ADMIN_TOKEN.name, ops[1])], by[(OPERATOR_TOKEN.name, ops[1])]
    assert admin is not None and admin["web"] and not admin["token"]
    assert operator is not None and not operator["web"] and not operator["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
@pytest.mark.parametrize("name", ["caution", "dangerous", SAFE_MARKED])
@pytest.mark.parametrize("person", [OPERATOR_TOKEN, ADMIN_TOKEN], ids=["operator", "admin"])
async def test_a_hosted_run_reads_what_the_approve_does(
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
    name: str,
    person: Caller,
) -> None:
    """A person's login in a hosted agent run: the part says what the approve does."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    await register_levels(embedding)
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: "agent:hosted"}):
        answer = await explain_call(
            CallQuestion(asker=person.operator(), me=True, connector_id="demo-1.x", op_id=op(name))
        )
        assert answer.answer == "wait", answer
        request_id = await park(person, op(name))
    own = answer.own_approval
    assert own is not None
    for way, web in (("web", True), ("token", False)):
        done = await approve(person, request_id, web=web)
        assert getattr(own, way) is done.accepted, (way, own)
        assert getattr(own, f"{way}_reason") == done.reason or done.accepted, (way, own)
    # The safe change with only the plain mark counts as dangerous (CUSTOM_POLICY on).
    expected = person is ADMIN_TOKEN or (self_approval == "true" and name == "caution")
    assert own.web is own.token is expected, own


@pytest.mark.parametrize("approver", [False, True])
def test_a_reader_cannot_ask_for_themselves(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, approver: bool
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="true")
    reader = Caller(
        "Reader", "reader-a", PrincipalKind.USER, TenantRole.READ_ONLY, approver=approver
    )
    key = make_rsa_keypair(f"kid-ownappr-reader-{approver}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = reader.post(client, key, op("caution"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
async def test_the_check_finds_a_part_that_differs_from_the_approve(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the part forgets why a caution call waits, the comparison above fails."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="false")
    await register_levels(embedding)
    key = make_rsa_keypair("kid-ownappr-broken")
    with (
        patch(
            "meho_backplane.policy.explain_own_approval.waits_only_for_token_call",
            return_value=False,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key, (OPERATOR_TOKEN,), (op("caution"),))
    assert any(
        "Operator, token / demo.vm.caution: web" in line for row in rows for line in agrees(row)
    )
