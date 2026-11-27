# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` off, a connector with no version has the id it had before.

Two products, ``cowx`` and ``cowy``, each have ``wild.thing.run``
(``safe``, group ``compute``) with no implementation id and no version.
With ``CUSTOM_POLICY`` off, the policy names such a connector ``-`` and an
entry for the product ``cowx`` does not match it. Each test compares the
answer with the answer of the same set-up on the code before the product
form existed; that answer is recorded in the test. All names and values
are made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, OperationPolicyOverride
from meho_backplane.main import app
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

from ._carry_over_helpers import TENANT, auth, dry_run, override, policy, token
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

OP = "wild.thing.run"
PRODUCTS = ("cowx", "cowy")
_SWITCHES = (
    "POLICY_CLASSIFICATION",
    "POLICY_ENGINE_SHADOW",
    "POLICY_APPROVER_ROUTING",
    "CUSTOM_POLICY",
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _switches(monkeypatch: pytest.MonkeyPatch, *on: str) -> None:
    for name in _SWITCHES:
        monkeypatch.setenv(name, "true" if name in on else "false")
    get_settings.cache_clear()


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


async def _register(embedding: AsyncMock) -> None:
    for product in PRODUCTS:
        await register_typed_operation(
            product=product,
            version="",
            impl_id="",
            op_id=OP,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level="safe",
            embedding_service=embedding,
        )


async def _override_on(product: str) -> None:
    await override(OP, TENANT, approval=True, product=product)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OperationPolicyOverride)
            .where(OperationPolicyOverride.product == product)
            .values(version="", impl_id="")
        )
        await session.commit()


def _post(client: TestClient, key: Any, route: str, product: str) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            route,
            json={"connector_id": product, "op_id": OP, "params": {}},
            headers=auth(token(key, sub="op-a", tenant_role=TenantRole.OPERATOR.value)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


#: Case 9 on the code before the product form: the overrides report row.
MAIN_OVERRIDE_ROW: dict[str, Any] = {
    "connector_id": "",
    "op_id": OP,
    "scope": "tenant",
    "tenant_id": str(TENANT),
    "safety_level": "safe",
    "requires_approval": True,
    "reason": None,
    "set_by_sub": "admin-a",
    "category": None,
    "labels": {},
    "always_wait": False,
    "rule_ref": None,
}


@pytest.mark.asyncio
async def test_case_9_the_overrides_report_row_is_as_before(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch)
    await _register(embedding)
    await _override_on("cowx")
    key = make_rsa_keypair("kid-cnvoff-9")

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/connectors/operation-overrides", headers=auth(token(key)))
    assert resp.status_code == 200, resp.text
    (row,) = resp.json()["overrides"]
    row.pop("set_at", None)

    assert row == MAIN_OVERRIDE_ROW


#: Case 10 on the code before the product form: the whole dry run.
MAIN_DRY_RUN: dict[str, Any] = {
    "scope": "tenant",
    "sandbox_mode": False,
    "settings": [
        {
            "source": "policy_file",
            "scope": "tenant",
            "ref": "tenant:classifications[0]",
            "match": {"op": OP},
            "operations": [{"connector_id": "-", "op_id": OP, "step_of": []}],
            "warnings": [
                "As `destructive`, a call needs a matching preview first. "
                "A call without one is refused."
            ],
        }
    ],
    "message": "Dry run: nothing was changed.",
}


@pytest.mark.asyncio
async def test_case_10_the_dry_run_is_as_before(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch)
    await _register(embedding)
    await policy(TENANT, [{"match": {"op": OP}, "always_wait": True}])
    key = make_rsa_keypair("kid-cnvoff-10")

    resp = dry_run(client, key)

    assert resp.status_code == 200, resp.text
    assert resp.json() == MAIN_DRY_RUN


@pytest.mark.asyncio
async def test_case_11_an_entry_by_the_product_does_not_make_the_call_wait(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch, "POLICY_CLASSIFICATION")
    await _register(embedding)
    await policy(TENANT, [{"match": {"connector": "cowx"}, "always_wait": True}])
    key = make_rsa_keypair("kid-cnvoff-11")

    result = _post(client, key, "/api/v1/operations/call", "cowx")

    assert result["status"] == "ok", result


#: Case 12 on the code before the product form: the preview's policy keys.
MAIN_PREVIEW: dict[str, Any] = {
    "safety_level": "safe",
    "requires_approval": True,
    "policy_verdict": "needs-approval",
}
#: Case 12 on the code before the product form: the approval card's effect.
MAIN_EFFECT: dict[str, Any] = {
    "preview_populated": False,
    "preview_reason": "connector_did_not_populate",
    "op_id": OP,
    "connector_id": "cowx",
    "op_class": "other",
    "safety_level": "safe",
    "category": "other",
    "labels": {},
    "classification_rule": "taxonomy.default",
}


@pytest.mark.asyncio
async def test_case_12_the_preview_and_the_approval_card_are_as_before(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch, "POLICY_CLASSIFICATION")
    await _register(embedding)
    await _override_on("cowx")
    await policy(
        TENANT,
        [{"match": {"connector": "cowx"}, "category": "network", "labels": {"team": "made-up"}}],
    )
    key = make_rsa_keypair("kid-cnvoff-12")

    preview = _post(client, key, "/api/v1/operations/preview", "cowx")
    shown = {k: preview.get(k) for k in ("safety_level", "requires_approval", "policy_verdict")}
    call = _post(client, key, "/api/v1/operations/call", "cowx")
    async with get_sessionmaker()() as session:
        (request,) = (await session.execute(select(ApprovalRequest))).scalars()
    effect = {k: v for k, v in request.proposed_effect.items() if k not in {"params"}}

    assert shown == MAIN_PREVIEW
    assert call["status"] == "awaiting_approval", call
    assert effect == MAIN_EFFECT
