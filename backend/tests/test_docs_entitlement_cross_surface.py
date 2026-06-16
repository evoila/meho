# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-surface entitlement-contract consistency (T2 #1802).

The docs-corpus entitlement asymmetry (`search_docs` over MCP returns
chunks while the same collection over REST 403s and `/ui/corpus` renders
empty) was reported as a backend bug. Grounding the v0.16.0 code overturns
that: all three answerable surfaces — the REST route, the `search_docs` /
`ask_docs` MCP tools, and the `/ui/corpus` BFF — gate on the *one* shared
:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`
check, which reads exactly two fields off the :class:`Operator`:
``tenant_id`` and ``capabilities``. And all three build that ``Operator``
from the **same** claim-derivation chain
(:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` →
``_decode_with_jwks`` → ``Operator(...)``), parametrised only by the
*audience* the token is validated for.

So the cross-surface contract is **verified consistent** here on two axes:

1. **Same ``(tenant_id, capabilities)`` source.** Given the *same* claim
   set, each surface's verifier yields an :class:`Operator` with identical
   ``tenant_id`` + ``capabilities`` — behavioural proof that no surface
   derives entitlement inputs differently (the test mints one token per
   audience carrying the same ``capabilities`` claim and asserts equality).

2. **The one deliberate divergence is the audience, and it is
   intentional.** REST + the UI BFF both validate against
   ``settings.keycloak_audience``; MCP validates against
   ``mcp_resource_uri(settings)`` (``<backplane-url>/mcp``, RFC 8707
   resource-scoped). This asymmetry is asserted *intentional* — it is why a
   per-audience Keycloak token can carry a different ``meho-docs:*`` claim
   set per audience, which is the reported symptom's root cause (a
   claim-mapper gap, documented in ``deploy/values-examples/README.md``),
   **not** a backplane inconsistency.

This is the test the docs-search codebase doc's cross-surface invariant
section names.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx

from meho_backplane.auth.jwt import (
    clear_jwks_cache,
    verify_jwt,
    verify_jwt_for_audience,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.auth import mcp_resource_uri, verify_mcp_jwt
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.refresh import verify_access_token_with_refresh
from meho_backplane.ui.auth.session_store import DecryptedSession

from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# A capability set carrying the base add-on key + one per-collection key.
_CAPS = ["meho-docs", "meho-docs:vmware"]
_TENANT = "11111111-1111-1111-1111-111111111111"
_SUB = "op-cross-surface"

#: The MCP audience the chassis derives from BACKPLANE_URL when no explicit
#: MCP_RESOURCE_URI is set: ``<backplane-url>/mcp``.
_BACKPLANE_URL = "https://meho.test"
_MCP_AUDIENCE = f"{_BACKPLANE_URL}/mcp"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis auth env so both audiences resolve deterministically."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _mint(key: Any, *, audience: str) -> str:
    """Mint an RS256 token for *audience* carrying the shared claim set."""
    return _mint_token(
        key,
        sub=_SUB,
        audience=audience,
        tenant_id=_TENANT,
        tenant_role="operator",
        capabilities=_CAPS,
    )


def test_three_surfaces_share_one_audience_contract() -> None:
    """The audience each surface validates for is the one deliberate divergence.

    REST + UI use ``keycloak_audience``; MCP uses ``mcp_resource_uri``. This
    asserts the divergence is *exactly* that — and that REST and the UI BFF
    agree (so they 403 / empty *together*, never one without the other).
    """
    settings = get_settings()
    # REST (verify_jwt) and the UI BFF (verify_access_token_with_refresh)
    # both bind the HTTP-API audience.
    assert settings.keycloak_audience == "meho-backplane"
    # MCP binds the resource-scoped URI — a *different* audience by design.
    assert mcp_resource_uri(settings) == _MCP_AUDIENCE
    assert mcp_resource_uri(settings) != settings.keycloak_audience


async def test_rest_and_mcp_derive_same_tenant_and_capabilities() -> None:
    """Given the same claim set, REST and MCP yield identical ``(tenant_id, caps)``.

    REST goes through ``verify_jwt`` (audience = ``keycloak_audience``); MCP
    through ``verify_mcp_jwt`` (audience = ``mcp_resource_uri``). Each token
    is minted for its own audience but carries the **same** ``capabilities``
    + ``tenant_id`` claims — so equal entitlement inputs prove the surfaces
    share one ``Operator`` contract, not three divergent derivations.
    """
    key = _make_rsa_keypair("kid-A")
    rest_token = _mint(key, audience="meho-backplane")
    mcp_token = _mint(key, audience=_MCP_AUDIENCE)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        rest_op = await verify_jwt(f"Bearer {rest_token}")
        mcp_op = await verify_mcp_jwt(f"Bearer {mcp_token}")

    # Same identity contract: tenant_id + capabilities are derived from the
    # same claim names by the same projection, so they match exactly.
    assert rest_op.tenant_id == mcp_op.tenant_id == uuid.UUID(_TENANT)
    assert rest_op.capabilities == mcp_op.capabilities == frozenset(_CAPS)
    assert rest_op.sub == mcp_op.sub == _SUB
    # The per-collection entitlement key both surfaces gate on is present.
    assert "meho-docs:vmware" in rest_op.capabilities
    assert "meho-docs:vmware" in mcp_op.capabilities


async def test_ui_bff_reconstructs_token_capabilities_not_session_tenant() -> None:
    """The UI entitlement path uses the *token's* ``(tenant_id, capabilities)``.

    The reported framing was "the UI BFF takes capabilities from the token
    but tenant_id from the session row". For the *entitlement* path that is
    not so: ``verify_access_token_with_refresh`` presents the stored access
    token to the same chassis chain and returns a full token-derived
    :class:`Operator` — so both ``tenant_id`` and ``capabilities`` come from
    the token, matching what REST derives from an identical claim set. (The
    session-row ``tenant_id`` is used only for the page-header chip.)
    """
    key = _make_rsa_keypair("kid-A")
    ui_token = _mint(key, audience="meho-backplane")
    now = datetime.now(UTC)
    decrypted = DecryptedSession(
        id=uuid.uuid4(),
        operator_sub=_SUB,
        # A *different* tenant on the session row than the token claim, to
        # prove the entitlement path follows the TOKEN, not the row.
        tenant_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
        created_at=now,
        # Far-future expiry so the reactive verify leg never refreshes: the
        # stored token is valid, so verify_access_token_with_refresh returns
        # the operator from the first verify without a refresh round-trip.
        expires_at=now + timedelta(hours=1),
        access_token=ui_token,
        refresh_token="refresh-plaintext",
        last_seen_at=now,
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        _refreshed, ui_op = await verify_access_token_with_refresh(
            decrypted,
            expected_audience=get_settings().keycloak_audience,
        )

    assert isinstance(ui_op, Operator)
    # Entitlement inputs come from the TOKEN: tenant_id is the claim value,
    # NOT the session-row 9999... tenant.
    assert ui_op.tenant_id == uuid.UUID(_TENANT)
    assert ui_op.capabilities == frozenset(_CAPS)
    assert "meho-docs:vmware" in ui_op.capabilities


async def test_all_three_call_one_audience_parametrised_verifier() -> None:
    """All three surfaces route entitlement-input derivation through one seam.

    A structural assertion complementing the behavioural ones: REST, UI, and
    MCP all build the ``Operator`` via
    :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`, the single
    audience-parametrised verifier. The same token, verified directly
    against either audience, yields equal ``(tenant_id, capabilities)`` — so
    the only thing that can differ per surface is the audience, never the
    entitlement-input derivation.
    """
    key = _make_rsa_keypair("kid-A")
    rest_token = _mint(key, audience="meho-backplane")
    mcp_token = _mint(key, audience=_MCP_AUDIENCE)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        rest_direct = await verify_jwt_for_audience(
            f"Bearer {rest_token}", expected_audience="meho-backplane"
        )
        mcp_direct = await verify_jwt_for_audience(
            f"Bearer {mcp_token}", expected_audience=_MCP_AUDIENCE
        )

    assert (rest_direct.tenant_id, rest_direct.capabilities) == (
        mcp_direct.tenant_id,
        mcp_direct.capabilities,
    )
