# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of a tenant import with personal agent rules, and the gate.

* The card in the web console (``GET /ui/approvals/{request_id}``) shows, for
  each role, the operations that start to run at once.
* A tenant import planned with ``CUSTOM_POLICY`` on holds
  ``planned_with_custom_policy: true``. When the switch at the run is not
  the same as on the card, the run stores nothing and asks for a new
  preview.
* The gate does not read the rules yet: with rules stored and the switch
  on, a personal agent's call gets the same answer as before, and every row
  of the approval table is the same as with the switch on and no rules
  stored.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.policy.service import POLICY_CHANGED
from meho_backplane.settings import get_settings
from tests.approval_table import build_rows, register_all_descriptors

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    if on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_card_in_the_web_console_shows_what_starts_to_run_at_once(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    key = make_rsa_keypair("kid-agents-card-ui")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = h.post(client, "import", a.tenant_admin(key), a.FILE_A)
        assert asked.status_code == 202, asked.text
        request_id = asked.json()["approval_request_id"]
        page = await a.console_get(client, key, f"/ui/approvals/{request_id}", sub="reviewer-b")
    assert page.status_code == 200, page.text
    assert "runs_at_once" in page.text
    assert "inslim.vm.restart" in page.text
    assert "tenant_rule:helper" in page.text
    (request,) = await h.pending_requests()
    card = request.proposed_effect["preview"]
    assert card["planned_with_custom_policy"] is True
    assert a.inslim(card["diff"]["runs_at_once"]["Admin"]) == ["inslim.vm.restart"]
    assert a.inslim(card["diff"]["runs_at_once"]["Reader"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("at_ask", "at_run"), [(False, True), (True, False)], ids=str)
async def test_the_run_stores_nothing_when_the_switch_is_not_the_same_as_on_the_card(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    at_ask: bool,
    at_run: bool,
) -> None:
    """Planned without the rules part, the run must not drop the stored rules, and back."""
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), a.FILE_A)).status == "ok"
    stored = await a.stored_tenant()

    _switch(monkeypatch, at_ask)
    key = make_rsa_keypair("kid-agents-card-switch")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = h.post(client, "import", a.tenant_admin(key), a.FILE_NO_RULES)
        assert asked.status_code == 202, asked.text
        (request,) = await h.pending_requests()
        card = request.proposed_effect["preview"]
        assert card.get("planned_with_custom_policy") is (True if at_ask else None)
        assert ("agents" in card["diff"]) is at_ask
        _switch(monkeypatch, at_run)
        clicked = await h.console_approve(
            client, key, request.id, sub="tenant-admin-b", platform_admin=False
        )
    assert clicked.status_code == 200, clicked.text
    (run_row,) = [
        row
        for row in await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        if row.operator_sub == "tenant-admin-b"
    ]
    assert run_row.status_code != 200
    assert POLICY_CHANGED in str(run_row.payload)
    assert await a.stored_tenant() == stored


async def _answers() -> list[tuple[str, str, str]]:
    """The answer of each fixture call, for a personal agent and for an agent login."""
    out = []
    for kind in (PrincipalKind.USER, PrincipalKind.AGENT):
        operator = h.person("caller", platform_admin=False, web=False, kind=kind)
        operator = operator.model_copy(update={"token_checked": True})
        for op_id in ("inslim.vm.list", "inslim.vm.restart"):
            result = await dispatch(
                operator=operator,
                connector_id="inslim-1.x",
                op_id=op_id,
                target=None,
                params={},
            )
            out.append((kind.value, op_id, result.status))
    return out


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_with_rules_stored_a_call_gets_the_same_answer_as_before(
    embedding: AsyncMock,
) -> None:
    await h.setup_ops(embedding)
    before = await _answers()
    await a.set_limit("dangerous")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), a.FILE_A)).status == "ok"
    assert (await a.stored_tenant() or {})["agents"]
    after = await _answers()
    assert after == before
    assert ("user", "inslim.vm.restart", "awaiting_approval") in after


@pytest.mark.asyncio
async def test_with_rules_stored_and_the_switch_on_the_approval_table_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_approval_table_snapshot import _use_built_in_settings

    _use_built_in_settings(monkeypatch)
    monkeypatch.delenv("POLICY_CLASSIFICATION", raising=False)
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    rule = {"name": "helper", "caller": "personal_agent", "roles": ["Admin"], "up_to": "dangerous"}
    try:
        without_rules = build_rows(await register_all_descriptors())
        clear_typed_op_snapshot()
        await h.store_install({"limits": {"personal_agent_up_to": "dangerous"}})
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                TenantPolicy(tenant_id=h.TENANT, document={"agents": [rule]}, set_by_sub="fixture")
            )
        with_rules = build_rows(await register_all_descriptors())
    finally:
        clear_typed_op_snapshot()
        get_settings.cache_clear()
    assert with_rules
    assert with_rules == without_rules
