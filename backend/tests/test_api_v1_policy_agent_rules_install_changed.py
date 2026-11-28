# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A tenant import is refused when the install policy differs at the run.

With ``CUSTOM_POLICY`` on, the stored card of a tenant import records the
hash of the install's ``limits`` and ``classifications``. At the run after
the click, MEHO compares the recorded value with the install's stored
policy. When they differ, the run is refused with one sentence, nothing is
stored, and the audit row of the run holds the sentence. When only other
parts of the install's policy changed, the run stores the file.
With the switch off, the card has no such record and the run is as before.

A tenant admin asks for the import with a token; another admin of the
tenant clicks Approve in the web console.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.connectors.policy.apply import INSTALL_POLICY_CHANGED, POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.settings import get_settings

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEYPAIR = make_rsa_keypair("kid-agents-install-changed")

_INSTALL: dict[str, Any] = {
    "classifications": [{"match": {"op": "inslim.vm.restart"}, "always_wait": True}],
    "limits": {"personal_agent_up_to": "dangerous"},
}

_FILE_RULE = (
    "policy: v1\nclassifications: []\nagents:\n  - name: helper\n"
    "    caller: personal_agent\n    roles: [Admin, Operator]\n    up_to: caution\n"
)


def _drop_wait(document: dict[str, Any]) -> None:
    document["classifications"] = []


def _lower_limit(document: dict[str, Any]) -> None:
    document["limits"] = {"personal_agent_up_to": "caution"}


def _new_description(document: dict[str, Any]) -> None:
    document["description"] = "another text"


_CHANGES = [
    pytest.param(_drop_wait, False, id="7-drops-always-wait"),
    pytest.param(_lower_limit, False, id="8-lowers-the-limit"),
    pytest.param(_new_description, True, id="9-only-the-description"),
]


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    if on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    get_settings.cache_clear()


async def _change_install(change: Callable[[dict[str, Any]], None]) -> None:
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one()
        document = copy.deepcopy(row.document)
        change(document)
        row.document = document


async def _ask_change_and_click(
    client: TestClient, content: str, change: Callable[[dict[str, Any]], None]
) -> tuple[dict[str, Any], Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        asked = h.post(client, "import", a.tenant_admin(_KEYPAIR), content)
        assert asked.status_code == 202, asked.text
        (request,) = await h.pending_requests()
        await _change_install(change)
        clicked = await h.console_approve(
            client, _KEYPAIR, request.id, sub="tenant-admin-b", platform_admin=False
        )
    assert clicked.status_code == 200, clicked.text
    (run_row,) = [
        row
        for row in await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        if row.operator_sub == "tenant-admin-b"
    ]
    return request.proposed_effect["preview"], run_row


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "runs"), _CHANGES)
async def test_a_tenant_import_is_refused_when_the_install_policy_differs_at_the_run(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[dict[str, Any]], None],
    runs: bool,
) -> None:
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await h.store_install(copy.deepcopy(_INSTALL))

    card, run_row = await _ask_change_and_click(client, _FILE_RULE, change)

    assert len(card["planned_with_install_limits"]) == 64
    stored = await a.stored_tenant()
    if runs:
        assert stored is not None
        assert [rule["name"] for rule in stored["agents"]] == ["helper"]
        assert INSTALL_POLICY_CHANGED not in str(run_row.payload)
    else:
        assert stored is None
        assert INSTALL_POLICY_CHANGED in str(run_row.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "runs"), _CHANGES)
async def test_with_the_switch_off_the_run_does_not_read_the_install_policy(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[dict[str, Any]], None],
    runs: bool,
) -> None:
    del runs
    _switch(monkeypatch, False)
    await h.setup_ops(embedding)
    await h.store_install(copy.deepcopy(_INSTALL))

    card, run_row = await _ask_change_and_click(client, a.FILE_NO_RULES, change)

    assert "planned_with_install_limits" not in card
    assert "planned_with_custom_policy" not in card
    stored = await a.stored_tenant()
    assert stored is not None
    assert stored.get("description") == "an unrelated edit"
    assert "agents" not in stored
    assert INSTALL_POLICY_CHANGED not in str(run_row.payload)
