# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Personal agent rules in a tenant's policy file, through the real paths.

Each case asks the way a real caller does: a token over REST
(``POST /api/v1/policy/import``), or the ``policy.apply`` call of a hosted
agent run, and approves with a click in the web console
(``POST /ui/approvals/{request_id}/approve``). The refusals read the audit
row of the refused call and check that nothing waits.

* With ``CUSTOM_POLICY`` on, a tenant file with ``agents`` is checked,
  previewed, imported after an Admin's click, exported and imported again
  with "No change.".
* ``caller: person`` and ``caller: service_agent`` are refused; a rule above
  the outer limit is refused; nothing is stored.
* A stored tenant file that is now above a lowered limit names a mistake on
  the next import, and the install's plan lists the tenant.
* An agent login, a service login and a hosted agent run are refused before
  anything waits.
* With the switch off, a file with ``agents`` is refused with "needs
  CUSTOM_POLICY"; a stored part is ignored and the export leaves it out.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.policy.apply import IMPORT_NEEDS_PERSON, POLICY_APPLY_OP_ID
from meho_backplane.policy.agent_rules import AGENTS_NEED_CUSTOM_POLICY
from meho_backplane.policy.service import NO_CHANGE
from meho_backplane.settings import get_settings

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

_ABOVE = (
    "`agents[0].up_to` is above the outer limit for personal agents: the rule `helper` "
    "allows dangerous, the limit is caution. Only a platform admin raises the limit, in the "
    "install's policy file."
)


async def _assert_refused(method: str, path: str, sub: str, status: int) -> None:
    """The last audit row of *sub* on *path* has the refused status; nothing waits."""
    rows = await h.audit_rows(method, path)
    assert [r.status_code for r in rows if r.operator_sub == sub][-1:] == [status]
    assert await h.pending_requests() == []


# ---------------------------------------------------------------------------
# Import with a token, a click in the web console, export, import again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_token_import_waits_for_a_click_then_export_and_import_again_is_no_change(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    key = make_rsa_keypair("kid-agents-roundtrip")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = a.tenant_admin(key)
        checked = h.post(client, "validate", bearer, a.FILE_BOUND)
        preview = h.post(client, "import", bearer, a.FILE_BOUND, preview="true")
        asked = h.post(client, "import", bearer, a.FILE_BOUND)
        assert checked.json()["mistakes"] == [], checked.text
        diff = preview.json()["diff"]
        assert [item["entry"]["name"] for item in diff["agents"]["added"]] == ["helper"]
        assert {role: a.inslim(items) for role, items in diff["runs_at_once"].items()} == {
            "Reader": [],
            "Operator": ["inslim.vm.restart"],
            "Admin": ["inslim.vm.restart"],
        }
        assert asked.status_code == 202, asked.text
        assert await a.stored_tenant() is None
        (request,) = await h.requests()
        assert request.rights_change is True
        assert request.proposed_effect["preview"]["diff"]["runs_at_once"] == diff["runs_at_once"]
        (asked_row,) = await h.audit_rows("APPROVAL", "approval.request")
        assert (asked_row.operator_sub, asked_row.payload["op_id"]) == (
            "tenant-admin-a",
            POLICY_APPLY_OP_ID,
        )

        clicked = await h.console_approve(
            client, key, request.id, sub="tenant-admin-b", platform_admin=False
        )
        assert clicked.status_code == 200, clicked.text
        (run_row,) = await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        assert (run_row.operator_sub, run_row.status_code) == ("tenant-admin-b", 200)

        exported = a.export_tenant(client, bearer)
        again = h.post(client, "import", bearer, exported.text)
    assert (await a.stored_tenant() or {})["agents"][0]["name"] == "helper"
    assert "agents:" in exported.text
    assert "name: helper" in exported.text
    assert again.status_code == 200, again.text
    assert again.json()["message"] == NO_CHANGE
    assert again.json()["diff"]["agents"] == {"added": [], "changed": [], "removed": []}
    assert len(await h.requests()) == 1


# ---------------------------------------------------------------------------
# Refused files: callers of later versions, a rule above the limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("caller", ["person", "service_agent"])
async def test_a_caller_of_a_later_version_is_refused_with_its_place(
    client: TestClient, embedding: AsyncMock, caller: str
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    content = a.FILE_A.replace("personal_agent", caller)
    key = make_rsa_keypair(f"kid-agents-{caller}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = h.post(client, "import", a.tenant_admin(key), content)
    assert resp.status_code == 422, resp.text
    (mistake,) = resp.json()["mistakes"]
    assert mistake.startswith(f"`agents[0].caller` is `{caller}`, which is not in this version")
    await _assert_refused("POST", "/api/v1/policy/import", "tenant-admin-a", 422)
    assert await h.requests() == []
    assert await a.stored_tenant() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_rule_above_the_outer_limit_is_refused_and_nothing_is_stored(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("caution")
    key = make_rsa_keypair("kid-agents-above")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = h.post(client, "import", a.tenant_admin(key), a.FILE_A)
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [_ABOVE]
    await _assert_refused("POST", "/api/v1/policy/import", "tenant-admin-a", 422)
    assert await h.requests() == []
    assert await a.stored_tenant() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_stored_file_above_a_lowered_limit_names_a_mistake_and_the_install_plan_lists_it(
    client: TestClient, embedding: AsyncMock
) -> None:
    """The same file again is not "No change.": its rule is above the new limit."""
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), a.FILE_A)).status == "ok"
    stored = await a.stored_tenant()

    key = make_rsa_keypair("kid-agents-lowered")
    lower = "policy: v1\nclassifications: []\nlimits: {personal_agent_up_to: caution}\n"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        install = h.post(client, "import", h.token(key), lower, scope="install", preview="true")
        await a.set_limit("caution")
        again = h.post(client, "import", a.tenant_admin(key), a.FILE_A)
    sentence = (
        "`agents[0]` (`helper`) allows dangerous, above the new limit caution. The rule "
        "counts only up to the new limit."
    )
    assert install.json()["diff"]["agents_above_limit"] == [
        {"tenant_id": str(h.TENANT), "entries": [sentence]}
    ]
    assert f"Tenant {h.TENANT}: {sentence}" in install.json()["warnings"]
    assert again.status_code == 422, again.text
    assert again.json()["mistakes"] == [_ABOVE]
    assert again.json()["no_change"] is False
    assert await a.stored_tenant() == stored


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_install_plan_has_no_section_when_no_tenant_is_above_the_limit(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), a.FILE_A)).status == "ok"
    key = make_rsa_keypair("kid-agents-install-same")
    same = "policy: v1\nclassifications: []\nlimits: {personal_agent_up_to: dangerous}\n"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        install = h.post(client, "import", h.token(key), same, scope="install", preview="true")
    assert install.json()["ok"] is True, install.text
    assert "agents_above_limit" not in install.json()["diff"]


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    ("kind", "sub"),
    [(PrincipalKind.AGENT, "example-agent"), (PrincipalKind.SERVICE, "example-service")],
    ids=["agent", "service"],
)
async def test_an_agent_or_a_service_login_is_refused_before_anything_waits(
    client: TestClient, embedding: AsyncMock, kind: PrincipalKind, sub: str
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    key = make_rsa_keypair(f"kid-agents-{kind.value}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = a.tenant_admin(key, sub=sub, principal_kind=kind.value)
        resp = h.post(client, "import", bearer, a.FILE_A)
    assert resp.status_code == 403, resp.text
    await _assert_refused("POST", "/api/v1/policy/import", sub, 403)
    result = await a.dispatch_tenant_apply(h.person(sub, kind=kind, web=False), a.FILE_A)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    await _assert_refused("DISPATCH", POLICY_APPLY_OP_ID, sub, 403)
    assert await h.requests() == []
    assert await a.stored_tenant() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_hosted_agent_run_is_refused_before_anything_waits(embedding: AsyncMock) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    with actor_delegation("agent:example"):
        result = await a.dispatch_tenant_apply(h.person("example-admin", web=False), a.FILE_A)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    (row,) = await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
    assert (row.operator_sub, row.actor_sub, row.status_code) == (
        "example-admin",
        "agent:example",
        403,
    )
    assert await h.requests() == []
    assert await a.stored_tenant() is None


# ---------------------------------------------------------------------------
# The switch off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_switch_off_agents_needs_custom_policy(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = a.tenant_admin(key)
        checked = h.post(client, "validate", bearer, a.FILE_A)
        preview = h.post(client, "import", bearer, a.FILE_A, preview="true")
        resp = h.post(client, "import", bearer, a.FILE_A)
    assert checked.json()["mistakes"] == [AGENTS_NEED_CUSTOM_POLICY]
    assert preview.json()["mistakes"] == [AGENTS_NEED_CUSTOM_POLICY]
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [AGENTS_NEED_CUSTOM_POLICY]
    await _assert_refused("POST", "/api/v1/policy/import", "tenant-admin-a", 422)
    assert await h.requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_with_the_switch_turned_off_again_a_stored_part_is_ignored(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), a.FILE_A)).status == "ok"
    stored = await a.stored_tenant()

    monkeypatch.delenv("CUSTOM_POLICY")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-agents-off-again")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = a.tenant_admin(key)
        exported = a.export_tenant(client, bearer)
        again = h.post(client, "import", bearer, exported.text)
        edited = h.post(client, "import", bearer, a.FILE_NO_RULES, preview="true")
    assert exported.status_code == 200
    assert "agents" not in exported.text
    assert "inslim-1.x" in exported.text
    assert again.json()["message"] == NO_CHANGE, again.text
    assert "agents" not in again.json()["diff"]
    assert "runs_at_once" not in again.json()["diff"]
    assert "agents" not in edited.json()["diff"]
    assert await a.stored_tenant() == stored
