# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A site with one tenant works exactly as before the tenant list (#5243).

One login can now list several tenants in the ``meho_tenants`` claim
(docs/decisions/2026-10-28-several-tenants-per-person.md). The promise is:
a site with one tenant sees no change. This test checks that promise
against a real Keycloak, with real logins:

1. Start an empty Keycloak. Set the realm up as the public docs say, with
   ``meho admin keycloak bootstrap-clients``, and make one person per role
   group.
2. Remove the ``meho-tenants`` mapper. This is today's realm: the tokens
   have no ``meho_tenants`` claim, so MEHO takes the old path.
3. Each person logs in and sends the same requests (REST and MCP), with no
   ``Meho-Tenant`` header.
4. Run ``bootstrap-clients`` again, as the upgrade notes say. It adds the
   mapper back, so each token now lists one tenant.
5. The same people log in again and send the same requests.

Then the answers, the audit rows and the Vault login role must be the
same in both rounds. The only new thing is the ``tenants`` list in the
status answers: empty without the claim, one entry with it. The test
checks that list on its own.

Skips when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset, when Docker is missing,
or when the Go toolchain is missing (CI provides all three).
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.vault import vault_client_for_operator
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from meho_backplane.mcp.tools import meho_status as meho_status_tool
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.integration.test_keycloak_bootstrap_mcp_login import (
    _BACKPLANE_AUDIENCE,
    _GROUP_USER_PASSWORD,
    _HTTP_TIMEOUT,
    _KC_ADMIN_PASSWORD,
    _KC_ADMIN_USER,
    _MCP_RESOURCE_URI,
    _OPERATOR_PASSWORD,
    _OPERATOR_USER,
    _REALM,
    _ROLE_GROUPS,
    _TENANT_ID,
    _admin_headers,
    _browser_login,
    _build_meho_cli,
    _client_mappers,
    _client_uuid,
    _create_realm_per_docs,
    _create_user,
    _plugin_default_scopes,
    _run_bootstrap,
)

#: The clients that ``bootstrap-clients`` gives the ``meho-tenants`` mapper.
_LOGIN_CLIENTS = ("meho-cli", "meho-cli-approve", "meho-mcp")
_TENANTS_MAPPER = "meho-tenants"
#: The per-tenant Vault login role the backplane is set up with.
_TENANT_VAULT_ROLE = "meho-tenant-main"


def _people() -> list[tuple[str, str, str]]:
    """(username, password, role group) of each person the test logs in.

    One person per role group, and the first admin that
    ``bootstrap-clients`` makes (in ``MEHO-Admins`` and
    ``MEHO-Platform-Admins``).
    """
    people = [
        (f"one-tenant-{group.lower()}", _GROUP_USER_PASSWORD, group) for group in _ROLE_GROUPS
    ]
    people.append((_OPERATOR_USER, _OPERATOR_PASSWORD, "MEHO-Admins"))
    return people


def _remove_tenants_mapper(base_url: str) -> None:
    """Make today's realm: take the ``meho-tenants`` mapper off every login client."""
    headers = _admin_headers(base_url)
    for client_id in _LOGIN_CLIENTS:
        mapper = _client_mappers(base_url, _REALM, client_id).get(_TENANTS_MAPPER)
        if mapper is None:
            continue
        uuid = _client_uuid(base_url, _REALM, client_id)
        httpx.delete(
            f"{base_url}/admin/realms/{_REALM}/clients/{uuid}/protocol-mappers/models/{mapper['id']}",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ).raise_for_status()


@pytest.fixture(scope="module")
def meho_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the ``meho`` CLI from this checkout once per module."""
    if not os.environ.get("MEHO_TEST_KEYCLOAK_IMAGE"):
        pytest.skip(
            "MEHO_TEST_KEYCLOAK_IMAGE not set; runs in CI where the Keycloak image is provisioned."
        )
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)
    if shutil.which("go") is None:
        pytest.skip("Go toolchain not on PATH; needed to build the meho CLI.")
    return _build_meho_cli(tmp_path_factory.mktemp("meho-cli"))


@pytest.fixture(scope="module")
def todays_keycloak(meho_binary: Path) -> Iterator[str]:
    """Start Keycloak with today's realm (no ``meho-tenants`` mapper); yield its base URL."""
    image = os.environ["MEHO_TEST_KEYCLOAK_IMAGE"]

    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    container = (
        DockerContainer(image)
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", _KC_ADMIN_USER)
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", _KC_ADMIN_PASSWORD)
        .with_command("start-dev")
        .with_exposed_ports(8080)
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Keycloak container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "Listening on:", timeout=120)
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        _create_realm_per_docs(base_url)
        _run_bootstrap(meho_binary, base_url)
        for username, _password, group in _people():
            if username != _OPERATOR_USER:
                _create_user(base_url, username, group)
        _remove_tenants_mapper(base_url)
        yield base_url
    finally:
        container.stop()


def _token_claims(access_token: str) -> dict[str, Any]:
    """The claims of *access_token* (already checked by Keycloak; read only)."""
    body = access_token.split(".")[1]
    body += "=" * (-len(body) % 4)
    return dict(json.loads(base64.urlsafe_b64decode(body)))


def _split_tenants(status: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    """Return a status answer without ``operator.tenants``, and that list."""
    operator = dict(status["operator"])
    tenants = operator.pop("tenants")
    return {**status, "operator": operator}, tenants


async def _mcp(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    msg_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", json=body, headers=auth)


async def _answers(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """Send the same requests a client sends, with no ``Meho-Tenant`` header."""
    auth = {"Authorization": f"Bearer {token}"}
    auth_v = {**auth, "MCP-Protocol-Version": PROTOCOL_VERSION}
    out: dict[str, Any] = {}

    live = await client.get("/api/v1/health/live", headers=auth)
    assert live.status_code == 200, live.text
    out["live"], out["live_tenants"] = _split_tenants(live.json())
    for name in ("operator", "admin"):
        resp = await client.get(f"/api/v1/rbac-test/{name}", headers=auth)
        out[f"rbac_{name}"] = (resp.status_code, resp.json())

    init = await _mcp(
        client,
        auth,
        1,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "one-tenant-test", "version": "0.0.1"},
        },
    )
    assert init.status_code == 200, init.text
    out["mcp_initialize"] = init.json()
    tools = await _mcp(client, auth_v, 2, "tools/list")
    assert tools.status_code == 200, tools.text
    out["mcp_tools"] = sorted(t["name"] for t in tools.json()["result"]["tools"])
    call = await _mcp(client, auth_v, 3, "tools/call", {"name": "meho_status", "arguments": {}})
    assert call.status_code == 200, call.text
    result = call.json()["result"]
    assert result["isError"] is False, result
    out["mcp_status"], out["mcp_status_tenants"] = _split_tenants(
        json.loads(result["content"][0]["text"])
    )
    return out


async def _audit_rows(async_pg_url: str) -> dict[str, tuple[Any, ...]]:
    """Every audit row by id: who, which tenant, what, the answer, and the role source."""
    engine = create_async_engine(async_pg_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id::text, operator_sub, tenant_id::text, method, path, "
                    "status_code, payload->>'tenant_role_source' FROM audit_log"
                )
            )
            rows = list(result.all())
    finally:
        await engine.dispose()
    return {str(row[0]): tuple(row[1:]) for row in rows}


async def _one_round(
    base_url: str,
    app: FastAPI,
    async_pg_url: str,
    vault_logins: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Log each person in, send the requests, and collect what MEHO answered."""
    seen_rows = set(await _audit_rows(async_pg_url))
    round_: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver", timeout=_HTTP_TIMEOUT
    ) as client:
        for username, password, _group in _people():
            token = _browser_login(base_url, _plugin_default_scopes(), username, password)
            operator = await verify_jwt_for_audience(
                f"Bearer {token}", expected_audience=_BACKPLANE_AUDIENCE
            )
            async with vault_client_for_operator(operator):
                pass
            vault_role = vault_logins[-1]["role"]
            before = set(await _audit_rows(async_pg_url))
            answers = await _answers(client, token)
            rows = await _audit_rows(async_pg_url)
            answers["audit"] = sorted(
                (row for rid, row in rows.items() if rid not in before), key=repr
            )
            answers["claims_have_list"] = "meho_tenants" in _token_claims(token)
            answers["operator"] = {
                "tenant_id": str(operator.tenant_id),
                "tenant_role": operator.tenant_role.value,
                "approver": operator.approver,
                "self_approver": operator.self_approver,
                "platform_admin": operator.platform_admin,
                "scopes": sorted(operator.scopes),
            }
            answers["vault_role"] = vault_role
            round_[username] = answers
    assert set(await _audit_rows(async_pg_url)) > seen_rows, "the round wrote no audit row"
    return round_


async def test_one_tenant_site_answers_the_same_before_and_after_the_tenant_list(
    todays_keycloak: str,
    meho_binary: Path,
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{todays_keycloak}/realms/{_REALM}")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _BACKPLANE_AUDIENCE)
    monkeypatch.setenv("MCP_RESOURCE_URI", _MCP_RESOURCE_URI)
    monkeypatch.setenv("VAULT_OIDC_ROLE_BY_TENANT", f"{_TENANT_ID}={_TENANT_VAULT_ROLE}")
    get_settings.cache_clear()
    clear_jwks_cache()
    vault_logins = install_fake_vault(monkeypatch).auth.jwt.login_calls
    # An earlier test in this process may have emptied the tool registry.
    if mcp_registry.get_tool("meho_status") is None:
        importlib.reload(meho_status_tool)

    before = await _one_round(todays_keycloak, integration_app, async_pg_url, vault_logins)

    # The upgrade step: run the setup again. It adds the mapper back.
    _run_bootstrap(meho_binary, todays_keycloak)
    for client_id in _LOGIN_CLIENTS:
        assert _TENANTS_MAPPER in _client_mappers(todays_keycloak, _REALM, client_id), client_id

    after = await _one_round(todays_keycloak, integration_app, async_pg_url, vault_logins)

    for username, _password, group in _people():
        old, new = before[username], after[username]
        role, approver = _ROLE_GROUPS[group]
        who = f"{username} ({group})"

        assert old["claims_have_list"] is False, who
        assert new["claims_have_list"] is True, who

        # Each round works in the one tenant, with the group's role, no header needed.
        for answers in (old, new):
            assert answers["operator"]["tenant_id"] == _TENANT_ID, who
            assert answers["operator"]["tenant_role"] == role, who
            assert answers["operator"]["approver"] is approver, who
            assert answers["vault_role"] == _TENANT_VAULT_ROLE, who
            assert answers["live"]["operator"]["tenant_id"] == _TENANT_ID, who
            assert answers["live"]["operator"]["tenant_role"] == role, who
            operator_ok = role in ("operator", "tenant_admin")
            assert answers["rbac_operator"][0] == (200 if operator_ok else 403), who
            assert answers["rbac_admin"][0] == (200 if role == "tenant_admin" else 403), who
            assert "meho_status" in answers["mcp_tools"], who
            assert answers["audit"], f"{who}: no audit row"
            for row in answers["audit"]:
                assert row[1] == _TENANT_ID, f"{who}: audit row in another tenant: {row}"
                assert row[5] is None, f"{who}: audit row says platform admin role: {row}"

        # The only new thing: the status answers list the one tenant.
        assert old["live_tenants"] == [] and old["mcp_status_tenants"] == [], who
        main = [{"tenant_id": _TENANT_ID, "name": "main", "tenant_role": role, "current": True}]
        assert new["live_tenants"] == main, who
        assert new["mcp_status_tenants"] == main, who

        # Everything else is the same.
        for key in (
            "operator",
            "vault_role",
            "live",
            "rbac_operator",
            "rbac_admin",
            "mcp_initialize",
            "mcp_tools",
            "mcp_status",
            "audit",
        ):
            assert new[key] == old[key], (
                f"{who}: {key} changed\nbefore: {old[key]}\nafter: {new[key]}"
            )
