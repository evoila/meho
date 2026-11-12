# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The starting grant pick in the console forms (#6192).

The register-principal form and the create-agent form ask for the
agent's first grants: ``none``, ``read-everything`` or ``read-targets``.
No option is selected at first. A missing or wrong pick shows its text
next to the field. After a pick that writes grants, the console opens
the agent's grants page.

The fixtures come from ``test_ui_agents.py`` (the autouse ones are
imported by name so pytest applies them here too).
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.agents.grant_sets import StartingGrantsError
from meho_backplane.auth.agent_principals import (
    AgentPrincipalRead,
    AgentPrincipalService,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, AgentPermission, Target
from meho_backplane.settings import get_settings
from tests.test_ui_agents import (  # noqa: F401 -- the autouse fixtures apply by import
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _admin_session,
    _authenticated_client,
    _bff_env,
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_principal,
    _seed_session_sync,
    _seed_tenant,
    _ui_operator_role,
)

_REGISTER = "/ui/agents/principals/register"
_CREATE = "/ui/agents/create"


def _switch(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", mode)
    get_settings.cache_clear()


def _seed_target(name: str, *, tenant_id: uuid.UUID = _TENANT_A) -> str:
    async def _do() -> str:
        async with get_sessionmaker()() as session:
            target = Target(
                tenant_id=tenant_id,
                name=name,
                aliases=[],
                product="vault",
                host=f"{name}.example.test",
                port=8200,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
            )
            session.add(target)
            await session.commit()
            return str(target.id)

    return asyncio.run(_do())


def _grant_rows(principal_sub: str) -> list[AgentPermission]:
    async def _do() -> list[AgentPermission]:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AgentPermission).where(AgentPermission.principal_sub == principal_sub)
            )
            return list(result.scalars().all())

    return asyncio.run(_do())


def _definition_names() -> list[str]:
    async def _do() -> list[str]:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(AgentDefinition.name))
            return list(result.scalars().all())

    return asyncio.run(_do())


def _admin_client(*, with_csrf: bool = True) -> tuple[Any, Any, str]:
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=with_csrf)


def _principal_read(name: str) -> AgentPrincipalRead:
    now = datetime.now(UTC)
    return AgentPrincipalRead(
        id=uuid.uuid4(),
        tenant_id=_TENANT_A,
        name=name,
        keycloak_client_id=f"agent:{name}",
        keycloak_internal_id=f"kc-{name}",
        owner_sub=_OP_A,
        revoked=False,
        created_by_sub=_OP_A,
        created_at=now,
        updated_at=now,
    )


def _capture_register(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        captured.update(kwargs)
        return _principal_read(kwargs["payload"].name)

    monkeypatch.setattr(AgentPrincipalService, "register", _fake)
    return captured


def _radios(body: str) -> list[str]:
    return re.findall(r'<input\s+type="radio"\s+name="starting_grants"[^>]*>', body)


def _create_form(**extra: Any) -> dict[str, Any]:
    return {
        "name": "triage",
        "identity_ref": "agent-triage",
        "model_tier": "standard",
        "system_prompt": "You triage.",
        "turn_budget": "10",
        **extra,
    }


# ---------------------------------------------------------------------------
# The field itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [_REGISTER, _CREATE])
def test_pick_has_three_options_and_none_is_selected(path: str) -> None:
    """Both forms show the three choices with no option selected at first."""
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        body = client.get(path).text
    finally:
        mock.stop()
    radios = _radios(body)
    assert [re.search(r'value="([^"]+)"', r).group(1) for r in radios] == [  # type: ignore[union-attr]
        "none",
        "read-everything",
        "read-targets",
    ]
    assert not any("checked" in r for r in radios)
    # With the switch off (the default) the pick is optional.
    assert not any("required" in r for r in radios)
    assert "(optional)" in body


@pytest.mark.parametrize("path", [_REGISTER, _CREATE])
def test_pick_is_required_while_the_switch_is_not_off(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _switch(monkeypatch, "observe")
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        body = client.get(path).text
    finally:
        mock.stop()
    radios = _radios(body)
    assert len(radios) == 3
    assert all("required" in r for r in radios)
    assert not any("checked" in r for r in radios)


def _help_texts(body: str) -> dict[str, str]:
    """Return each choice's help text, keyed on the choice value."""
    texts: dict[str, str] = {}
    for value in ("none", "read-everything", "read-targets"):
        match = re.search(
            rf'value="{value}".*?<span class="text-xs opacity-60">([^<]+)</span>', body, re.S
        )
        assert match is not None, value
        texts[value] = html.unescape(" ".join(match.group(1).split()))
    return texts


@pytest.mark.parametrize("path", [_REGISTER, _CREATE])
@pytest.mark.parametrize("mode", ["off", "observe"])
def test_help_says_the_limits_need_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, path: str, mode: str
) -> None:
    """With the switch off or observe, the text does not promise limits MEHO does not apply."""
    _switch(monkeypatch, mode)
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        texts = _help_texts(client.get(path).text)
    finally:
        mock.stop()
    assert "runs nothing until" not in texts["none"]
    assert f"AGENT_GRANT_REQUIRED is {mode}" in texts["none"]
    assert "can still run safe operations" in texts["none"]
    assert "only when AGENT_GRANT_REQUIRED is on" in texts["none"]
    assert "only on the targets" not in texts["read-targets"]
    assert f"AGENT_GRANT_REQUIRED is {mode}" in texts["read-targets"]
    assert "also run on other targets" in texts["read-targets"]
    assert texts["read-everything"].startswith("Safe operations run at once on every target.")


@pytest.mark.parametrize("path", [_REGISTER, _CREATE])
def test_help_states_the_limits_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _switch(monkeypatch, "on")
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        texts = _help_texts(client.get(path).text)
    finally:
        mock.stop()
    assert texts["none"] == "The agent runs nothing until an admin adds a grant."
    assert texts["read-targets"] == "The same, but only on the targets you pick below."
    assert "AGENT_GRANT_REQUIRED" not in texts["read-everything"]


def test_pick_lists_only_the_tenants_live_targets() -> None:
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target("alpha-vault")
    _seed_target("beta-vault")
    _seed_target("other-tenant-vault", tenant_id=_TENANT_B)
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        body = client.get(_REGISTER).text
    finally:
        mock.stop()
    boxes = re.findall(r'name="starting_grant_targets"\s+value="([^"]+)"', body)
    assert boxes == ["alpha-vault", "beta-vault"]
    assert "other-tenant-vault" not in body


def test_pick_says_when_the_tenant_has_no_targets() -> None:
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        body = client.get(_REGISTER).text
    finally:
        mock.stop()
    assert "data-no-targets" in body


def test_edit_form_has_no_pick() -> None:
    """The pick is only asked when a definition is created."""
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        created = client.post(_CREATE, data=_create_form(), headers=_csrf_headers(csrf))
        assert created.status_code == 204, created.text
        body = client.get("/ui/agents/triage/edit").text
    finally:
        mock.stop()
    assert 'name="starting_grants"' not in body


# ---------------------------------------------------------------------------
# Register form
# ---------------------------------------------------------------------------


def test_register_read_targets_sends_the_pick_and_opens_the_grants_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={
                "name": "deploy-bot",
                "starting_grants": "read-targets",
                "starting_grant_targets": ["alpha-vault", "beta-vault"],
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants?principal_sub=agent%3Adeploy-bot"
    pick = captured["payload"].starting_grants
    assert pick.set.value == "read-targets"
    assert pick.targets == ["alpha-vault", "beta-vault"]


def test_register_read_everything_opens_the_grants_page(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={"name": "deploy-bot", "starting_grants": "read-everything"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"].startswith("/ui/agents/grants?principal_sub=")
    assert captured["payload"].starting_grants.set.value == "read-everything"


def test_register_none_goes_back_to_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={"name": "deploy-bot", "starting_grants": "none"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/principals"
    assert captured["payload"].starting_grants.set.value == "none"


def test_register_without_a_pick_sends_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No option picked: the console sends no pick, so the service decides."""
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(_REGISTER, data={"name": "deploy-bot"}, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert captured["payload"].starting_grants is None


def test_register_missing_pick_shows_the_service_text_next_to_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _refuse(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise StartingGrantsError(
            "starting_grants_required", "pick the agent's starting grants in 'starting_grants'."
        )

    monkeypatch.setattr(AgentPrincipalService, "register", _refuse)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(_REGISTER, data={"name": "deploy-bot"}, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    error = re.search(r'data-error-for="starting_grants">\s*([^<]+)<', response.text)
    assert error is not None
    assert error.group(1).strip().startswith("pick the agent&#39;s starting grants")
    # The name field has no error; the text is next to the pick.
    assert 'data-error-for="name"' not in response.text
    # The person keeps what they typed.
    assert 'value="deploy-bot"' in response.text


def test_register_unknown_target_shows_the_text_and_keeps_the_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_target("alpha-vault")

    async def _refuse(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise StartingGrantsError("starting_grants_target_unknown", "no target named 'gone'.")

    monkeypatch.setattr(AgentPrincipalService, "register", _refuse)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={
                "name": "deploy-bot",
                "starting_grants": "read-targets",
                "starting_grant_targets": ["alpha-vault"],
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="starting_grants"' in response.text
    assert "no target named" in response.text
    assert re.search(r'value="read-targets"[^>]*checked', response.text)
    assert re.search(r'value="alpha-vault"[^>]*checked', response.text)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"starting_grant_targets": ["alpha-vault"]}, "Pick &#39;Read some targets&#39;"),
        ({"starting_grants": "read-targets"}, "needs at least one target"),
        (
            {"starting_grants": "read-everything", "starting_grant_targets": ["alpha-vault"]},
            "only used with",
        ),
        ({"starting_grants": "everything"}, "Input should be"),
    ],
)
def test_register_wrong_pick_is_refused_before_the_service(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any], message: str
) -> None:
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER, data={"name": "deploy-bot", **data}, headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="starting_grants"' in response.text
    assert message in response.text
    assert captured == {}


# ---------------------------------------------------------------------------
# Create-agent form (real service and database)
# ---------------------------------------------------------------------------


def test_create_read_everything_writes_the_grant_and_opens_the_grants_page() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(starting_grants="read-everything"),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants?principal_sub=agent-triage"
    rows = _grant_rows("agent-triage")
    assert [(r.op_pattern, r.target_scope, r.verdict) for r in rows] == [("*", "*", "auto-execute")]
    assert rows[0].created_by_sub == _OP_A


def test_create_read_targets_writes_one_row_per_target() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    alpha = _seed_target("alpha-vault")
    beta = _seed_target("beta-vault")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(
                starting_grants="read-targets", starting_grant_targets=["alpha-vault", "beta-vault"]
            ),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert sorted(r.target_scope for r in _grant_rows("agent-triage")) == sorted([alpha, beta])


def test_create_none_writes_nothing_and_goes_back_to_the_list() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE, data=_create_form(starting_grants="none"), headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents"
    assert _grant_rows("agent-triage") == []


def test_create_missing_pick_with_the_switch_on_shows_the_text_next_to_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the switch on and no grant on the agent, the service needs a pick."""
    _switch(monkeypatch, "on")
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(_CREATE, data=_create_form(), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    # 200 so HTMX swaps the form back in (the create form's error mold).
    assert response.status_code == 200, response.text
    assert "data-form-error-summary" in response.text
    error = re.search(r'data-error-for="starting_grants">\s*([^<]+)<', response.text)
    assert error is not None
    assert "Choose &#39;none&#39;" in error.group(1)
    assert "starting_grants_required" not in error.group(1)
    assert _definition_names() == []


def test_create_unknown_target_writes_nothing() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(starting_grants="read-targets", starting_grant_targets=["gone"]),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'data-error-for="starting_grants"' in response.text
    assert "no target named" in response.text
    assert _definition_names() == []
    assert _grant_rows("agent-triage") == []


def test_create_target_from_another_tenant_is_unknown() -> None:
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target("other-tenant-vault", tenant_id=_TENANT_B)
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(
                starting_grants="read-targets", starting_grant_targets=["other-tenant-vault"]
            ),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert 'data-error-for="starting_grants"' in response.text
    assert _grant_rows("agent-triage") == []


def test_create_wrong_pick_keeps_the_typed_values() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(starting_grants="read-targets"),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert 'data-error-for="starting_grants"' in response.text
    assert 'value="triage"' in response.text
    assert re.search(r'value="read-targets"[^>]*checked', response.text)
    assert _definition_names() == []


# ---------------------------------------------------------------------------
# The browser shows the register errors
# ---------------------------------------------------------------------------

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# Runs the container's ``hx-on::before-swap`` code the way htmx 2.0.9 does
# (``event`` in scope, fired on the swap target) for each case, then does
# the swap htmx would do: the body goes into the container only when
# ``shouldSwap`` is on. Prints what the container holds after each case.
_SWAP_DRIVER = r"""
"use strict";
const [code, casesJson] = process.argv.slice(1);
const handler = new Function("event", code);
const out = {};
for (const c of JSON.parse(casesJson)) {
  const detail = {
    xhr: { status: c.status },
    requestConfig: { verb: c.verb },
    shouldSwap: c.status < 400,
    isError: c.status >= 400,
  };
  handler({ detail });
  out[c.name] = { swapped: detail.shouldSwap && !detail.isError,
                  container: detail.shouldSwap ? c.body : "" };
}
process.stdout.write(JSON.stringify(out));
"""


def _swap_hook(client: Any) -> str:
    page = client.get("/ui/agents/principals")
    assert page.status_code == 200, page.text
    container = re.search(r'<div\s+id="principals-modal-container"[^>]*>', page.text)
    assert container is not None, page.text
    hook = re.search(r'hx-on::before-swap="([^"]*)"', container.group(0))
    assert hook is not None, container.group(0)
    return html.unescape(hook.group(1))


@requires_node
def test_read_targets_without_a_target_shows_the_error_in_the_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 422 answer replaces the modal, so the error is visible next to the pick.

    htmx skips an error answer unless the swap target's before-swap hook
    turns the swap on. This runs the hook from the real list page against
    the real 422 answer and checks what the container shows after it.
    """
    captured = _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        hook = _swap_hook(client)
        response = client.post(
            _REGISTER,
            data={"name": "deploy-bot", "starting_grants": "read-targets"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert captured == {}

    cases = [
        {"name": "register422", "status": 422, "verb": "post", "body": response.text},
        {"name": "conflict409", "status": 409, "verb": "post", "body": "<dialog>409</dialog>"},
        {"name": "banner503", "status": 503, "verb": "post", "body": "<dialog>503</dialog>"},
        {"name": "ok200", "status": 200, "verb": "get", "body": "<dialog>ok</dialog>"},
        {"name": "get404", "status": 404, "verb": "get", "body": "not found"},
        {"name": "post500", "status": 500, "verb": "post", "body": "boom"},
    ]
    proc = subprocess.run(
        ["node", "-e", _SWAP_DRIVER, hook, json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    shown = result["register422"]
    assert shown["swapped"] is True
    error = re.search(
        r'<span class="text-xs text-error" role="alert" data-error-for="starting_grants">'
        r"\s*([^<]+)<",
        shown["container"],
    )
    assert error is not None, shown["container"]
    assert "needs at least one target" in error.group(1)
    assert 'value="deploy-bot"' in shown["container"]
    assert result["conflict409"]["swapped"] is True
    assert result["banner503"]["swapped"] is True
    assert result["ok200"]["swapped"] is True
    # A failed GET and an unexpected 500 keep htmx's default: no swap.
    assert result["get404"]["swapped"] is False
    assert result["post500"]["swapped"] is False
