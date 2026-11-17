# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Three more readers of a class stored on the row of an ingested operation.

* No standing grant covers an operation whose row stores ``credential_mint``:
  grant create refuses it, and a live grant never clears a call of it.
* A reveal of an operation that stores ``credential_read`` but also changes
  something (a ``POST``) still counts as a change for the approve in order.
* The dispatcher notes the class of the row it really runs. The outer MCP
  row of ``call_operation`` reads that note, also when the dispatcher ran
  another row than the one the caller named.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ServicePrincipalGrant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatcher, reset_dispatcher_caches
from meho_backplane.operations._catalog_guard import CatalogSelection
from meho_backplane.operations.approved_reveal import is_write_reveal
from meho_backplane.operations.ingest.safety_floors import _FLOORS
from meho_backplane.operations.op_class_of import (
    RanOpClass,
    note_ran_op_class,
    ran_op_class_var,
    request_write_note_op_class,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    consult_and_record_grant,
    credential_mint_grant_reason,
)
from meho_backplane.settings import get_settings
from tests._stored_op_class_harness import (
    CONNECTOR_ID,
    GET_CLIENTS,
    POST_CLIENTS,
    Clients,
    call,
    ingest,
    operator,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    Clients.calls = []
    Clients.answer = [{"name": "a"}]
    Clients.failure = None
    yield
    _FLOORS.clear()
    reset_dispatcher_caches()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


# -- no standing grant covers a stored credential_mint -----------------------------------


def test_stored_credential_mint_cannot_be_granted() -> None:
    assert credential_mint_grant_reason(GET_CLIENTS) is None
    assert credential_mint_grant_reason(GET_CLIENTS, "write") is None
    assert credential_mint_grant_reason(GET_CLIENTS, "credential_mint") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "covered"), [("credential_mint", False), (None, True)])
async def test_live_grant_never_clears_a_stored_credential_mint(
    stub_embedding_service: AsyncMock, stored: str | None, covered: bool
) -> None:
    rows = await ingest(
        stub_embedding_service, {GET_CLIENTS: stored} if stored else {}, approval=True
    )
    who = operator(PrincipalKind.SERVICE)
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                tenant_id=who.tenant_id,
                principal_sub=who.sub,
                op_id=GET_CLIENTS,
                connector_id=CONNECTOR_ID,
                reason="a live grant that matches the call",
                created_by_sub="op-admin",
            )
        )
        await session.commit()

    granted = await consult_and_record_grant(
        operator=who,
        descriptor=rows[GET_CLIENTS],
        target=None,
        connector_id=CONNECTOR_ID,
        record=False,
    )

    assert (granted is not None) is covered


@pytest.mark.asyncio
async def test_grant_create_refuses_a_stored_credential_mint(
    stub_embedding_service: AsyncMock,
) -> None:
    await ingest(stub_embedding_service, {GET_CLIENTS: "credential_mint"}, approval=True)
    who = operator(PrincipalKind.SERVICE)
    payload = ServiceGrantCreate(
        principal_sub="svc:deploy-bot",
        op_id=GET_CLIENTS,
        connector_id=CONNECTOR_ID,
        reason="unattended build",
    )

    with pytest.raises(GrantValidationError, match="makes a new secret"):
        await ServicePrincipalGrantService().create(who.tenant_id, "op-admin", payload)


# -- a reveal of a stored credential_read on a POST is a change --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "changes"), [(POST_CLIENTS, True), (GET_CLIENTS, False)])
async def test_reveal_of_a_stored_credential_read_that_writes_is_a_change(
    stub_embedding_service: AsyncMock, op_id: str, changes: bool
) -> None:
    await ingest(stub_embedding_service, {op_id: "credential_read"}, approval=True)
    result = await call(operator(), op_id, {})
    assert result.status == "awaiting_approval", (result.status, result.error)
    async with get_sessionmaker()() as session:
        (request,) = (await session.execute(select(ApprovalRequest))).scalars().all()
    request.reveal_secret = True

    assert request.proposed_effect["op_class"] == "credential_read"
    assert is_write_reveal(request) is changes


def test_request_write_note_class_keeps_the_write_of_the_method() -> None:
    effect = {"op_class": "credential_read"}
    assert request_write_note_op_class(POST_CLIENTS, effect) == "write"
    assert request_write_note_op_class(GET_CLIENTS, effect) == "credential_read"
    assert request_write_note_op_class(POST_CLIENTS, {"op_class": "credential_write"}) == (
        "credential_write"
    )
    assert request_write_note_op_class(GET_CLIENTS, None) == "read"


# -- the dispatcher notes the class of the row it runs ------------------------------------


def test_the_first_noted_class_wins_and_no_holder_means_no_note() -> None:
    note_ran_op_class("credential_read")
    holder = RanOpClass()
    token = ran_op_class_var.set(holder)
    try:
        note_ran_op_class("credential_mint")
        note_ran_op_class("read")
    finally:
        ran_op_class_var.reset(token)
    assert holder.op_class == "credential_mint"


@pytest.mark.asyncio
async def test_dispatcher_notes_the_class_of_the_row_it_picked(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller names a row with no stored class; the picked row stores a secret class."""
    rows = await ingest(stub_embedding_service, {})
    picked = rows[GET_CLIENTS]
    picked.op_class = "credential_read"

    async def select_other_row(**kwargs: Any) -> CatalogSelection:
        return CatalogSelection(picked, kwargs["connector_id"], None)

    monkeypatch.setattr(dispatcher, "select_catalog_for_target", select_other_row)
    holder = RanOpClass()
    token = ran_op_class_var.set(holder)
    try:
        await call(operator(), GET_CLIENTS, {}, approved=True)
    finally:
        ran_op_class_var.reset(token)

    assert holder.op_class == "credential_read"


@pytest.mark.parametrize("stored", ["credential_read", "credential_mint", "credential_write"])
def test_policy_shadow_marks_a_stored_secret_class_as_never_granted(stored: str) -> None:
    """The shadow policy uses the same check as grant create and use."""
    from meho_backplane.policy.shadow import _op_class

    def row(op_class: str | None) -> Any:
        return SimpleNamespace(
            op_id=GET_CLIENTS,
            op_class=op_class,
            policy_always_wait=False,
            safety_level="caution",
            method="GET",
            tags=[],
        )

    assert "credential_read" in _op_class(row(stored))
    assert "credential_read" not in _op_class(row(None))
