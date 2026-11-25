# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The set-aside count and time in the answers of ``/api/v1/runner-principals``.

With ``RUNNER_CHECKS_ENABLED`` on, the list and get answers hold
``set_aside_count`` and ``set_aside_last_at`` of each runner of the tenant.
With the switch off, the answers have neither key.

The helpers come from :mod:`tests.test_api_v1_runner_principals`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerAssignmentRow
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from .test_api_v1_runner_principals import (
    _TENANT_A,
    _TENANT_B,
    _seed_runner_row,
    _seed_tenants,
    _settings_env,  # noqa: F401  (autouse)
    _token,
    client,  # noqa: F401  (fixture)
)

_LAST_AT = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)
_PATH = "/api/v1/runner-principals"
_KEY = make_rsa_keypair("kid-set-aside")
_KEYS = {"set_aside_count", "set_aside_last_at"}


def _set_switch(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("RUNNER_CHECKS_ENABLED", value)
    get_settings.cache_clear()


async def _add_row(
    name: str,
    *,
    count: int,
    tenant_id: uuid.UUID = _TENANT_A,
    last_at: datetime | None = _LAST_AT,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            RunnerAssignmentRow(
                tenant_id=tenant_id,
                runner_name=name,
                items=[],
                set_aside_count=count,
                set_aside_last_at=last_at,
            )
        )
        await session.commit()


async def _seed() -> None:
    """``with-count`` has a stored count, ``no-row`` has no assignment row.

    ``never-set-aside`` has an assignment row with the count 0 and no time.
    """
    await _seed_tenants()
    await _seed_runner_row("with-count")
    await _seed_runner_row("no-row")
    await _seed_runner_row("never-set-aside")
    await _add_row("with-count", count=3)
    await _add_row("never-set-aside", count=0, last_at=None)
    await _add_row("no-row", count=9, tenant_id=_TENANT_B)  # another tenant's runner


def _get(test_client: TestClient, path: str) -> Any:
    headers = {"Authorization": f"Bearer {_token(_KEY, role=TenantRole.OPERATOR)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEY))
        resp = test_client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _time_of(entry: dict[str, Any]) -> datetime:
    value = datetime.fromisoformat(entry["set_aside_last_at"])
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_list_and_get_hold_the_set_aside_count_and_time_with_the_switch_on(
    client: TestClient,  # noqa: F811  (fixture)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_switch(monkeypatch, "true")
    await _seed()

    listed = {p["name"]: p for p in _get(client, _PATH)["principals"]}

    for entry in (listed["with-count"], _get(client, f"{_PATH}/with-count")):
        assert entry["set_aside_count"] == 3
        assert _time_of(entry) == _LAST_AT
    for name in ("no-row", "never-set-aside"):
        for entry in (listed[name], _get(client, f"{_PATH}/{name}")):
            assert entry["set_aside_count"] == 0
            assert entry["set_aside_last_at"] is None


@pytest.mark.asyncio
async def test_list_and_get_have_no_set_aside_key_with_the_switch_off(
    client: TestClient,  # noqa: F811  (fixture)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_switch(monkeypatch, "false")
    await _seed()

    entries = [*_get(client, _PATH)["principals"], _get(client, f"{_PATH}/with-count")]

    assert len(entries) == 4
    for entry in entries:
        assert not _KEYS & entry.keys()


def test_the_openapi_schema_holds_both_fields_as_optional_values() -> None:
    schemas = app.openapi()["components"]["schemas"]
    principal = schemas["RunnerPrincipalRead"]

    assert principal["properties"].keys() >= _KEYS
    assert not _KEYS & set(principal["required"])
