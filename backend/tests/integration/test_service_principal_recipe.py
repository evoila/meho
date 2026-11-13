# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The machine-login recipe works against a real Keycloak.

``docs/codebase/connectors-secret-broker.md`` ("Ops example -- a machine login
for an agent seat") gives the ``representation`` that ``keycloak.client.create``
takes. This test reads that JSON block from the page, fills the placeholders,
and posts it to the live realm the way the ``keycloak.client.create`` op does.
Then it reads the secret the way the ``keycloak`` store of ``secret.move``
does, signs in with the client-credentials grant, and checks that MEHO reads
the token as a ``service`` principal in the right tenant with the right role.

A second create of the same client answers 409, which the op reports as
``conflict`` (safe to run twice).

Skip rules are the ones of ``test_auth_keycloak_client_credentials.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from meho_backplane.auth.agent_token import get_client_credentials_token
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.settings import get_settings
from tests.integration.conftest import KeycloakBootstrap

pytestmark = pytest.mark.asyncio

_DOC = Path(__file__).resolve().parents[3] / "docs" / "codebase" / "connectors-secret-broker.md"
_RECIPE_MARKER = "<!-- recipe:keycloak-client-representation -->"


def _recipe_representation(*, client_id: str, tenant_id: str, audience: str) -> dict[str, Any]:
    """Read the JSON block under the recipe marker and fill its placeholders."""
    text = _DOC.read_text(encoding="utf-8")
    _, marker, after = text.partition(_RECIPE_MARKER)
    assert marker, "the recipe marker is missing from the secret broker page"
    match = re.search(r"```json\n(.*?)\n```", after, flags=re.DOTALL)
    assert match, "the recipe has no json block after its marker"
    raw = (
        match.group(1)
        .replace("<client-id>", client_id)
        .replace("<tenant-id>", tenant_id)
        .replace("<audience>", audience)
    )
    assert "<" not in raw, "a placeholder is left in the recipe"
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


async def _post_client(
    admin: KeycloakAdminClient, representation: dict[str, Any]
) -> httpx.Response:
    assert admin._http is not None
    return await admin._http.post(
        f"{admin._admin_url}/clients",
        content=json.dumps(representation),
        headers={**admin._auth_headers(), "Content-Type": "application/json"},
    )


async def test_recipe_client_signs_in_as_a_service_principal(
    keycloak_bootstrap: KeycloakBootstrap,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak_bootstrap.issuer_url)
    get_settings.cache_clear()
    clear_jwks_cache()

    tenant_id = uuid4()
    client_id = f"seat-recipe-{uuid4().hex[:8]}"
    representation = _recipe_representation(
        client_id=client_id,
        tenant_id=str(tenant_id),
        audience=keycloak_bootstrap.audience,
    )

    admin = KeycloakAdminClient(
        admin_url=keycloak_bootstrap.admin_url,
        token_url=f"{keycloak_bootstrap.issuer_url}/protocol/openid-connect/token",
        client_id=keycloak_bootstrap.admin_client_id,
        client_secret=keycloak_bootstrap.admin_client_secret,
    )
    async with admin:
        created = await _post_client(admin, representation)
        assert created.status_code == 201, created.status_code
        internal_id = created.headers["location"].rstrip("/").rsplit("/", 1)[-1]
        client_secret = await admin.get_client_secret(internal_id)

        # Safe to run twice: the second create is a conflict, not a new client.
        again = await _post_client(admin, representation)
        assert again.status_code == 409

    token = await get_client_credentials_token(
        issuer_url=keycloak_bootstrap.issuer_url,
        client_id=client_id,
        client_secret=client_secret,
        audience=keycloak_bootstrap.audience,
    )
    operator = await verify_jwt_for_audience(
        f"Bearer {token}",
        expected_audience=keycloak_bootstrap.audience,
    )

    assert operator.principal_kind == PrincipalKind.SERVICE
    assert operator.tenant_id == tenant_id
    assert operator.tenant_role == TenantRole.OPERATOR
    assert operator.sub
