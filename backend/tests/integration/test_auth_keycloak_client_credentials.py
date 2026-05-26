# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G11.2-T7 (#1098) — live-Keycloak ``client_credentials`` integration test.

Closes the deferred live-IdP acceptance criterion of G11.2-T2 (#816):
T2 shipped :func:`~meho_backplane.auth.agent_token.get_client_credentials_token`
and exhaustively covered request shape + every error mode via a respx
contract test. What no respx test can prove is that the **real**
Keycloak token endpoint accepts the form encoding we send and returns a
token the **real** JWT-validation chain accepts. This suite anchors
that chain end to end against a stock upstream Keycloak realm:

1. The :func:`keycloak_bootstrap` fixture (in
   :mod:`tests.integration.conftest`) starts a Keycloak 26.x container
   with the ``meho-integration`` realm + the ``agent:test-bot``
   confidential client + hardcoded-claim protocol mappers imported on
   startup.
2. The test reaches for a token via the production
   :func:`~meho_backplane.auth.agent_token.get_client_credentials_token`,
   passing the realm's pinned client id + secret.
3. The token is then driven through the production
   :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` — the same
   chain every authenticated chassis route uses, against the same JWKS
   the production code path resolves. Audience / ``sub`` / ``tenant_id``
   / ``tenant_role`` / ``principal_kind`` are asserted on the resulting
   :class:`~meho_backplane.auth.operator.Operator`.

This is the integration suite that catches:

* A Keycloak release that breaks the ``application/x-www-form-urlencoded``
  body parsing of the token endpoint or renames the ``client_credentials``
  grant — the respx test would still pass.
* A real RS256 signing-key + JWKS-cache round trip — proves the
  ``_decode_with_kid_rotation`` chain works against a live ``kid``,
  not just a respx-fabricated key set.
* A protocol-mapper claim-name drift — the mappers stamp
  ``tenant_id`` / ``tenant_role`` / ``principal_kind`` exactly as the
  production chain's ``_extract_*`` helpers expect them.

Skip rules: the fixture skips when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset
(no docker.io fallback for the ~600 MB Keycloak image; CI provisions
the Harbor-mirror tag) and when the Docker socket is unreachable. CI
runs both unconditionally — the issue's AC ``pass — N passed`` (not a
vacuous skip) holds on the integration job's runner.

Vacuous-skip discipline: the test deliberately does **not** carry any
guard that turns the assertion path into a no-op when the fixture
skips — pytest naturally reports the test as ``skipped`` with the
fixture's reason, distinguishable in the result file from a passed
test. The integration-test CI gate enforces a non-empty
``passed`` count in the consuming job.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from meho_backplane.auth.agent_token import get_client_credentials_token
from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.settings import get_settings
from tests.integration.conftest import KeycloakBootstrap

pytestmark = pytest.mark.asyncio


async def test_client_credentials_grant_end_to_end(
    keycloak_bootstrap: KeycloakBootstrap,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token from the real ``client_credentials`` grant validates through the JWT chain.

    The autouse ``_integration_default_env`` fixture in
    :mod:`tests.integration.conftest` pins ``KEYCLOAK_ISSUER_URL`` to
    a synthetic value the unit-test JWT helpers use (so the chassis
    can construct :class:`~meho_backplane.settings.Settings` at module
    load). This test overrides it to the **live** Keycloak container's
    realm issuer so the JWT chain resolves the JWKS from the running
    container rather than from the unit-test fake; the autouse
    ``get_settings.cache_clear()`` + ``clear_jwks_cache()`` calls in
    the same fixture handle the cache invalidation on teardown, but
    a fresh ``clear_jwks_cache()`` is issued here to guarantee no
    sibling test's JWKS bleeds into the verification.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", keycloak_bootstrap.issuer_url)
    get_settings.cache_clear()
    clear_jwks_cache()

    # 1. The production primitive — same signature G11.3's scheduler
    # will use to authenticate autonomous agent runs.
    token = await get_client_credentials_token(
        issuer_url=keycloak_bootstrap.issuer_url,
        client_id=keycloak_bootstrap.client_id,
        client_secret=keycloak_bootstrap.client_secret,
        audience=keycloak_bootstrap.audience,
    )
    assert isinstance(token, str) and token, "client_credentials grant returned empty token"

    # 2. The production validation chain — same dependency every
    # authenticated chassis route uses. The Bearer prefix shape is the
    # function's documented entry point.
    operator = await verify_jwt_for_audience(
        f"Bearer {token}",
        expected_audience=keycloak_bootstrap.audience,
    )

    # 3. ``sub`` resolves to the service account's stable UUID — a
    # non-empty string. The exact UUID value is Keycloak-generated
    # per-fresh-import, so the assertion is on shape rather than
    # value; this is what makes the test stable across container
    # restarts.
    assert operator.sub, "Operator.sub must resolve from the client_credentials token"

    # 4. ``aud`` matches: ``verify_jwt_for_audience`` would have raised
    # 401 ``invalid_audience`` on a mismatch — reaching this assertion
    # already proves the audience mapper landed the right claim.
    # Recorded explicitly for the result-file evidence trail.
    assert operator.raw_jwt == token

    # 5. The hardcoded-claim mappers stamp the agent-principal shape
    # T1 (#815) introduced.
    assert operator.principal_kind == PrincipalKind.AGENT
    assert operator.tenant_role == TenantRole.TENANT_ADMIN
    assert operator.tenant_id == UUID(keycloak_bootstrap.expected_tenant_id)
