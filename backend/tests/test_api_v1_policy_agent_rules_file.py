# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``agents`` part of a tenant's file on validate, the import preview and the import.

Each case asks with a tenant admin's token over REST
(``POST /api/v1/policy/validate`` and ``POST /api/v1/policy/import``) and
then reads the stored rows: the tenant's policy and the waiting requests.

* The part holds at most 200 rules. A file with more is refused with one
  sentence that names ``agents`` and the bound; nothing waits and nothing
  is stored.
* A stored rule that MEHO cannot read (its ``name`` is a list or a mapping)
  never blocks an import: the plan shows it as removed, and a valid file
  replaces it after the click.
* With ``CUSTOM_POLICY`` off, every refusal of a file that holds ``agents``
  names ``agents``. The other sentences are the ones a file with one of the
  five other reserved keys gets: the checks after the parse do not run.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy.agent_rules import AGENTS_NEED_CUSTOM_POLICY
from meho_backplane.policy.limits import not_read_mistake
from meho_backplane.policy.service import build_plan
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, mint_csrf_token

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

_KEYPAIR = make_rsa_keypair("kid-agents-file")


def _rules(count: int) -> str:
    lines = "".join(
        f"  - {{name: r{n:03d}, caller: personal_agent, roles: [Admin], up_to: safe}}\n"
        for n in range(1, count + 1)
    )
    return f"policy: v1\nagents:\n{lines}"


def _three(client: TestClient, content: str) -> tuple[Any, Any, Any]:
    """Validate, preview and import *content* as the tenant admin."""
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = a.tenant_admin(_KEYPAIR)
        return (
            h.post(client, "validate", bearer, content),
            h.post(client, "import", bearer, content, preview="true"),
            h.post(client, "import", bearer, content),
        )


# ---------------------------------------------------------------------------
# At most 200 rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_file_with_200_rules_passes_and_waits_for_the_click(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    checked, preview, asked = _three(client, _rules(200))
    assert checked.json()["mistakes"] == [], checked.text
    assert preview.json()["mistakes"] == [], preview.text
    assert len(preview.json()["diff"]["agents"]["added"]) == 200
    assert asked.status_code == 202, asked.text
    assert len(await h.pending_requests()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_file_with_201_rules_is_refused_and_nothing_waits(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    sentence = "`agents`: list should have at most 200 items after validation, not 201."
    checked, preview, asked = _three(client, _rules(201))
    assert checked.json()["mistakes"] == [sentence]
    assert preview.json()["mistakes"] == [sentence]
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == [sentence]
    assert await h.requests() == []
    assert await a.stored_tenant() is None


# ---------------------------------------------------------------------------
# A stored rule that MEHO cannot read
# ---------------------------------------------------------------------------


async def _store_tenant(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(TenantPolicy(tenant_id=h.TENANT, document=document, set_by_sub="fixture"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("bad", [{"name": ["x"]}, {"name": {"a": 1}}], ids=["list", "mapping"])
async def test_a_stored_rule_with_a_name_that_is_not_text_never_blocks_an_import(
    client: TestClient, embedding: AsyncMock, bad: dict[str, Any]
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _store_tenant({"classifications": [], "agents": [bad]})
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = a.tenant_admin(_KEYPAIR)
        without = h.post(client, "import", bearer, a.FILE_NO_RULES, preview="true")
        preview = h.post(client, "import", bearer, a.FILE_A, preview="true")
        asked = h.post(client, "import", bearer, a.FILE_A)
        assert asked.status_code == 202, asked.text
        (request,) = await h.pending_requests()
        clicked = await h.console_approve(
            client, _KEYPAIR, request.id, sub="tenant-admin-b", platform_admin=False
        )
    assert without.status_code == 200, without.text
    assert without.json()["diff"]["agents"]["removed"] == [{"entry": bad}]
    assert preview.status_code == 200, preview.text
    agents = preview.json()["diff"]["agents"]
    assert agents["removed"] == [{"entry": bad}]
    assert [item["entry"]["name"] for item in agents["added"]] == ["helper"]
    assert clicked.status_code == 200, clicked.text
    stored = await a.stored_tenant()
    assert stored is not None
    assert [rule["name"] for rule in stored["agents"]] == ["helper"]


# ---------------------------------------------------------------------------
# The switch off: every refusal names ``agents``
# ---------------------------------------------------------------------------

_RULE_LINE = "agents:\n  - {name: helper, caller: personal_agent, roles: [Admin], up_to: safe}\n"
_OFF_CASES = {
    "agents-alone": ("", []),
    "unknown-key": ("zzunknown: 1\n", ["`zzunknown` is not a known key."]),
    "approvers": ("approvers: []\n", [not_read_mistake("approvers")]),
    "unknown-category": (
        "classifications:\n  - {match: {connector: inslim-1.x}, category: zzcat}\n",
        [],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_OFF_CASES))
async def test_with_the_switch_off_every_refusal_names_agents(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    await h.setup_ops(embedding)
    extra, others = _OFF_CASES[case]
    expected = [AGENTS_NEED_CUSTOM_POLICY, *others]

    checked, preview, asked = _three(client, f"policy: v1\n{extra}{_RULE_LINE}")

    assert checked.json()["mistakes"] == expected
    assert preview.json()["mistakes"] == expected
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == expected
    assert await h.requests() == []
    assert await a.stored_tenant() is None


# ---------------------------------------------------------------------------
# An Admin's import in the web console's Run window
# ---------------------------------------------------------------------------


async def _console_run(client: TestClient, params: dict[str, Any], *, sub: str) -> Any:
    """Run ``policy.apply`` in the web console's Run window as the tenant admin *sub*."""
    signed_in = a.tenant_admin(
        _KEYPAIR,
        sub=sub,
        extra_claims={
            "auth_time": int(time.time()),
            "acr": "1",
            "sid": str(uuid.uuid4()),
            "azp": "meho-web",
        },
    )
    async with get_sessionmaker()() as session, session.begin():
        web = await create_session(
            session,
            operator_sub=sub,
            tenant_id=h.TENANT,
            access_token=signed_in,
            refresh_token="refresh-token",
            lifetime=timedelta(hours=1),
        )
    csrf = mint_csrf_token(str(web.id))
    client.cookies.set(SESSION_COOKIE_NAME, str(web.id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    try:
        return client.post(
            "/ui/operations/call",
            data={
                "connector_id": h.CONNECTOR,
                "op_id": POLICY_APPLY_OP_ID,
                "params": json.dumps(params),
            },
            headers={CSRF_HEADER_NAME: csrf},
            follow_redirects=False,
        )
    finally:
        client.cookies.clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_admin_import_in_the_web_console_run_window_runs_at_once(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="tenant",
            tenant_id=h.TENANT,
            content=a.FILE_A.encode(),
            custom_policy=True,
        )
    params = {
        "scope": "tenant",
        "file": a.FILE_A,
        "file_sha256": plan.file_sha256,
        "based_on": plan.based_on,
    }
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        page = await _console_run(client, params, sub="tenant-admin-a")
    assert page.status_code == 200, page.text
    for _ in range(100):
        stored = await a.stored_tenant()
        if stored is not None:
            break
        await asyncio.sleep(0.05)
    assert stored is not None, page.text
    assert [rule["name"] for rule in stored["agents"]] == ["helper"]
    assert await h.requests() == []
