# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runner logs in to a live Keycloak with a key pair it made itself.

The runner client is made over the Admin API like ``runner-principal
register`` does. Its public key is put on the client, then the runner signs
its own login request (signed JWT client login), gets a token that the
production JWT chain accepts, and gets a new token on demand. After the key is
removed and the client is disabled, no new token comes.

Skips without ``MEHO_TEST_KEYCLOAK_IMAGE`` or Docker, like the other live
Keycloak tests.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.auth.runner_enrollment import SIGNING_ALG, _public_jwks
from meho_backplane.runner.login import (
    RunnerLoginError,
    SignedJwtTokenProvider,
    _LoginRecord,
    load_or_create_private_key,
    public_jwk,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import KeycloakBootstrap

pytestmark = pytest.mark.asyncio


async def test_runner_signs_its_own_login_and_renews(
    keycloak_bootstrap: KeycloakBootstrap,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak_bootstrap.issuer_url)
    get_settings.cache_clear()
    clear_jwks_cache()

    tenant_id = uuid4()
    runner_id = uuid4()
    client_id = f"runner:key-login-{uuid4().hex[:8]}"
    admin = KeycloakAdminClient(
        admin_url=keycloak_bootstrap.admin_url,
        token_url=f"{keycloak_bootstrap.issuer_url}/protocol/openid-connect/token",
        client_id=keycloak_bootstrap.admin_client_id,
        client_secret=keycloak_bootstrap.admin_client_secret,
    )
    key = load_or_create_private_key(str(tmp_path))
    jwks, key_id = _public_jwks(public_jwk(key))
    async with admin:
        internal_id = await admin.create_client(
            client_id=client_id,
            name=client_id.removeprefix("runner:"),
            tenant_id=str(tenant_id),
            owner_sub="integration-test-owner",
            audience=keycloak_bootstrap.audience,
            tenant_role=TenantRole.READ_ONLY.value,
            principal_kind="runner",
            kind_attribute="runner",
            extra_hardcoded_claims={"runner_id": str(runner_id)},
        )
        await admin.set_client_signed_jwt_key(internal_id, jwks=jwks, signing_alg=SIGNING_ALG)

    provider = SignedJwtTokenProvider(
        key=key,
        record=_LoginRecord(
            client_id=client_id,
            issuer=keycloak_bootstrap.issuer_url,
            token_endpoint=f"{keycloak_bootstrap.issuer_url}/protocol/openid-connect/token",
            key_id=key_id,
            signing_alg=SIGNING_ALG,
        ),
    )
    first = await provider.get_token()
    operator = await verify_jwt_for_audience(
        f"Bearer {first}", expected_audience=keycloak_bootstrap.audience
    )
    assert operator.principal_kind == PrincipalKind.RUNNER
    assert operator.tenant_id == tenant_id
    assert operator.tenant_role == TenantRole.READ_ONLY

    second = await provider.refresh()
    assert second and second != first

    async with admin:
        await admin.clear_client_signed_jwt_key(internal_id)
        await admin.disable_client(internal_id)
    with pytest.raises(RunnerLoginError):
        await provider.refresh()
