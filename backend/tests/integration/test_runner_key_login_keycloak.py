# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runner logs in to a live Keycloak with a key pair it made itself.

The runner client is made over the Admin API like ``runner-principal
register`` does, and its row holds a one-time enrollment code. The runner
sends its public key with the code to ``POST /api/v1/runner-principals/enroll``
(the real route, which puts the key on the client). Then the runner signs its
own login request (signed JWT client login), gets a token that the production
JWT chain accepts, and gets the second token from its renew timer: a test
clock moves to 5 seconds before the first token ends. After the client is disabled and deleted, no new
token comes, and a key write finds no client.

Skips without ``MEHO_TEST_KEYCLOAK_IMAGE`` or Docker, like the other live
Keycloak tests.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import FastAPI

from meho_backplane.api.v1.runner_enrollment import router as enroll_router
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient, KeycloakClientNotFoundError
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.auth.runner_enrollment import SIGNING_ALG, new_enrollment_code
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.runner import login as runner_login
from meho_backplane.runner.login import (
    RunnerLoginError,
    SignedJwtTokenProvider,
    enroll_with_code,
    load_or_create_private_key,
)
from meho_backplane.settings import get_settings
from tests.integration.conftest import KeycloakBootstrap

pytestmark = pytest.mark.asyncio


async def test_runner_signs_its_own_login_and_renews(
    keycloak_bootstrap: KeycloakBootstrap,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak_bootstrap.issuer_url)
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", keycloak_bootstrap.admin_url)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", keycloak_bootstrap.admin_client_id)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", keycloak_bootstrap.admin_client_secret)
    get_settings.cache_clear()
    clear_jwks_cache()
    # A test clock for the renew timer only. The signed login request keeps
    # the real time, because Keycloak checks its times.
    moved = 0.0
    monkeypatch.setattr(
        runner_login,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic() + moved),
    )

    tenant_id = uuid4()
    runner_id = uuid4()
    name = f"key-login-{uuid4().hex[:8]}"
    client_id = f"runner:{name}"
    admin = KeycloakAdminClient(
        admin_url=keycloak_bootstrap.admin_url,
        token_url=f"{keycloak_bootstrap.issuer_url}/protocol/openid-connect/token",
        client_id=keycloak_bootstrap.admin_client_id,
        client_secret=keycloak_bootstrap.admin_client_secret,
    )
    async with admin:
        internal_id = await admin.create_client(
            client_id=client_id,
            name=name,
            tenant_id=str(tenant_id),
            owner_sub="integration-test-owner",
            audience=keycloak_bootstrap.audience,
            tenant_role=TenantRole.READ_ONLY.value,
            principal_kind="runner",
            kind_attribute="runner",
            extra_hardcoded_claims={"runner_id": str(runner_id)},
        )

    code, code_hash, expires_at = new_enrollment_code(runner_id)
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"t-{name}", name=name))
        session.add(
            RunnerPrincipal(
                id=runner_id,
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=client_id,
                keycloak_internal_id=internal_id,
                owner_sub="integration-test-owner",
                created_by_sub="integration-test-owner",
                enrollment_code_hash=code_hash,
                enrollment_code_expires_at=expires_at,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    central = FastAPI()
    central.include_router(enroll_router)
    key = load_or_create_private_key(str(tmp_path))
    record = await enroll_with_code(
        central_url="http://127.0.0.1",
        code=code,
        runner_id=name,
        key=key,
        key_dir=str(tmp_path),
        transport=httpx.ASGITransport(app=central),
    )
    assert record.client_id == client_id
    assert record.signing_alg == SIGNING_ALG

    provider = SignedJwtTokenProvider(key=key, record=record)
    first = await provider.get_token()
    assert await provider.get_token() == first
    operator = await verify_jwt_for_audience(
        f"Bearer {first}", expected_audience=keycloak_bootstrap.audience
    )
    assert operator.principal_kind == PrincipalKind.RUNNER
    assert operator.tenant_id == tenant_id
    assert operator.tenant_role == TenantRole.READ_ONLY

    # Move the clock to 5 seconds before the first token ends. The renew time
    # the runner set itself must lie before that point.
    claims = jwt.decode(first, options={"verify_signature": False})
    lifetime = float(claims["exp"]) - float(claims["iat"])
    assert lifetime > 5
    moved = lifetime - 5
    second = await provider.get_token()
    assert second and second != first
    assert time.time() < float(claims["exp"])

    async with admin:
        await admin.disable_client(internal_id)
        await admin.delete_client(internal_id)
        assert admin._http is not None
        stored = await admin._http.get(
            f"{keycloak_bootstrap.admin_url}/clients/{internal_id}",
            headers=admin._auth_headers(),
        )
        assert stored.status_code == 404
        with pytest.raises(KeycloakClientNotFoundError):
            await admin.set_client_signed_jwt_key(
                internal_id, jwks={"keys": []}, signing_alg=SIGNING_ALG
            )
    moved += lifetime
    with pytest.raises(RunnerLoginError):
        await provider.get_token()
