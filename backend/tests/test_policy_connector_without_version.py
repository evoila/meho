# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector with no version is named by its product in every policy answer.

Two products, ``cowx`` and ``cowy``, each have a row of the operation
``cowshared.restart`` with no implementation id and no version. A call
names such a connector by its product (``cowx``), so the dry run of the
"always wait" carry-over, ``POST /api/v1/policy/explain`` and the
overrides report name it that way too. A policy entry matches it by the
product, and also by the form with the dash that it had before
(``-``). The last test checks that no reader builds the id itself.
All names and values are made up.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.main import app
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.store import classify_rows
from meho_backplane.settings import get_settings

from ._carry_over_helpers import TENANT, auth, dry_run, override, policy, token
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

OP = "cowshared.restart"
PRODUCTS = ("cowx", "cowy")
SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"


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


@pytest.fixture(autouse=True)
def classification_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
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


def _operator_call(client: TestClient, key: Any, connector_id: str) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/operations/call",
            json={"connector_id": connector_id, "op_id": OP, "params": {}},
            headers=auth(token(key, sub="op-a", tenant_role=TenantRole.OPERATOR.value)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _explain(client: TestClient, key: Any, connector_id: str) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/policy/explain",
            json={"connector_id": connector_id, "op_id": OP, "me": True},
            headers=auth(token(key)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _overrides_report(client: TestClient, key: Any) -> list[dict[str, Any]]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/connectors/operation-overrides", headers=auth(token(key)))
    assert resp.status_code == 200, resp.text
    rows: list[dict[str, Any]] = resp.json()["overrides"]
    return rows


def _op(connector_id: str) -> dict[str, Any]:
    return {"connector_id": connector_id, "op_id": OP, "step_of": []}


@pytest.mark.asyncio
async def test_each_product_is_its_own_operation_in_every_answer(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _register(embedding)
    await policy(TENANT, [{"match": {"op": OP}, "always_wait": True}])
    await _override_on("cowx")
    key = make_rsa_keypair("kid-cnv-1")

    body = dry_run(client, key).json()

    by_source = {s["source"]: s for s in body["settings"]}
    assert by_source["override"]["match"] == {"connector": "cowx", "op": OP}
    assert by_source["override"]["operations"] == [_op("cowx")]
    assert by_source["policy_file"]["operations"] == [_op("cowx"), _op("cowy")]
    for product in PRODUCTS:
        answer = _explain(client, key, product)
        assert answer["connector_id"] == product
        assert answer["answer"] == "wait"
        assert answer["classification"]["requires_approval"] is True
    (row,) = _overrides_report(client, key)
    assert row["connector_id"] == "cowx"


async def _override_on(product: str) -> None:
    await override(OP, TENANT, approval=True, product=product)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OperationPolicyOverride)
            .where(OperationPolicyOverride.product == product)
            .values(version="", impl_id="")
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern", ["cowx", "cow*", "*-*", "-"], ids=["product", "product-glob", "dash-glob", "dash"]
)
async def test_an_entry_by_either_form_makes_the_call_wait_and_is_listed(
    client: TestClient, embedding: AsyncMock, pattern: str
) -> None:
    await _register(embedding)
    await policy(TENANT, [{"match": {"connector": pattern}, "always_wait": True}])
    key = make_rsa_keypair(f"kid-cnv-2-{pattern}")

    result = _operator_call(client, key, "cowx")
    (setting,) = dry_run(client, key).json()["settings"]

    assert result["status"] == "awaiting_approval", result
    assert _op("cowx") in setting["operations"]
    if pattern == "cowx":
        assert setting["operations"] == [_op("cowx")]
        assert _operator_call(client, key, "cowy")["status"] == "ok"


@pytest.mark.asyncio
async def test_an_entry_for_another_product_does_not_match(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _register(embedding)
    await policy(TENANT, [{"match": {"connector": "cowy"}, "always_wait": True}])
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.product == "cowx")
            )
        ).scalar_one()
        (classified,) = await classify_rows(session, tenant_id=TENANT, rows=[row], overrides=[[]])

    assert classified.always_wait is False


# ---------------------------------------------------------------------------
# No reader builds the connector id itself
# ---------------------------------------------------------------------------

#: The readers of the connector id of an operation row or an override row.
READERS = (
    "policy/store.py",
    "policy/bulk.py",
    "policy/explain.py",
    "policy/validate.py",
    "policy/carry_over.py",
    "api/v1/operation_overrides.py",
)

#: The one function that may put the parts of a connector id together.
ALLOWED = {("policy/store.py", "_dashed_id")}


def _builds_connector_id(node: ast.JoinedStr) -> bool:
    """True for an f-string that formats both an ``impl_id`` and a ``version``."""
    names: set[str] = set()
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            for sub in ast.walk(part.value):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
    return {"impl_id", "version"} <= names


def id_builders(source: str, path: str) -> list[str]:
    """Each function of *source* outside :data:`ALLOWED` that builds a connector id."""
    found: list[str] = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if (path, func.name) in ALLOWED:
            continue
        if any(
            isinstance(node, ast.JoinedStr) and _builds_connector_id(node)
            for node in ast.walk(func)
        ):
            found.append(f"{path}:{func.name}")
    return found


def test_no_reader_builds_the_connector_id_itself() -> None:
    found = [p for path in READERS for p in id_builders((SRC / path).read_text(), path)]

    assert found == [], f"Use connector_id_of or coordinates_of in {found}"


def test_the_check_finds_a_reader_that_builds_the_id_itself() -> None:
    source = (SRC / "policy/bulk.py").read_text() + (
        "\n\ndef _own(row):\n    return f'{row.impl_id}-{row.version}'\n"
    )

    assert id_builders(source, "policy/bulk.py") == ["policy/bulk.py:_own"]


def test_every_reader_uses_the_shared_function() -> None:
    for path in READERS:
        text = (SRC / path).read_text()
        assert "connector_id_of(" in text or "coordinates_of(" in text, path
