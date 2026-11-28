# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Vault login keys of a target belong to the platform admin, in every form.

A tenant admin who is not a platform admin cannot touch ``vault_role`` or
``vault_mount``: not with a new value, not with a removal, not with a new
key, and not with a change of only the JSON type (``1`` to ``true``, which
Python counts as equal). Each is refused before anything is stored, by
``extras`` and by ``extras_changes``, in the web console (REST ``PATCH``)
and in the run of ``targets.update``. A platform admin may make each change.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import TenantRole
from meho_backplane.targets.extras_changes import apply_extras_changes
from meho_backplane.targets.vault_login_extras import VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN
from tests.test_targets_extras_changes import (
    _KEY,
    _NAME,
    _app,
    _extras,
    _operator,
    _registered_op,  # noqa: F401 — autouse fixture
    _run,
)

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, mint_token, mock_discovery_and_jwks, public_jwks
from ._rest_write_helpers import run_writes_at_once
from ._targets_helpers import (
    _admin_token,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_CASES = [
    pytest.param({"vault_role": 1}, {"vault_role": True}, id="role-1-to-true"),
    pytest.param({"vault_mount": 1}, {"vault_mount": True}, id="mount-1-to-true"),
    pytest.param({"vault_role": 1}, {"vault_role": 1.0}, id="role-1-to-1.0"),
    pytest.param({"vault_role": "r1"}, {"vault_role": "r2"}, id="new-value"),
    pytest.param({"vault_role": "r1"}, {"vault_role": None}, id="removal"),
    pytest.param({}, {"vault_mount": "m1"}, id="new-key"),
]


@pytest.fixture
def console() -> Iterator[TestClient]:
    yield TestClient(run_writes_at_once(_app()))


def _platform_admin_token() -> str:
    return mint_token(
        _KEY,
        sub="padmin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=DEFAULT_TENANT_ID,
        platform_admin=True,
    )


def _body(form: str, start: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    if form == "extras_changes":
        return {"extras_changes": changes}
    return {"extras": apply_extras_changes(start, changes)}


def _patch(console: TestClient, body: dict[str, Any], token: str) -> Any:
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return console.patch(
            f"/api/v1/targets/{_NAME}", json=body, headers={"Authorization": f"Bearer {token}"}
        )


def _as_stored(extras: dict[str, Any]) -> str:
    """The map as JSON text, so a value and its JSON type are compared."""
    return json.dumps(extras, sort_keys=True)


@pytest.mark.parametrize("form", ["extras_changes", "extras"])
@pytest.mark.parametrize(("stored", "changes"), _CASES)
async def test_a_tenant_admin_cannot_touch_a_vault_login_key_in_the_console(
    console: TestClient, stored: dict[str, Any], changes: dict[str, Any], form: str
) -> None:
    start = {**stored, "a": 1}
    row = await _insert_target(name=_NAME, product="vault", extras=dict(start))

    refused = _patch(console, _body(form, start, changes), _admin_token(_KEY))

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN
    assert _as_stored(await _extras(row.id)) == _as_stored(start)

    allowed = _patch(console, _body(form, start, changes), _platform_admin_token())

    assert allowed.status_code == 200, allowed.text
    assert _as_stored(await _extras(row.id)) == _as_stored(apply_extras_changes(start, changes))


@pytest.mark.parametrize("form", ["extras_changes", "extras"])
@pytest.mark.parametrize(("stored", "changes"), _CASES)
async def test_a_tenant_admin_cannot_touch_a_vault_login_key_in_the_run(
    stored: dict[str, Any], changes: dict[str, Any], form: str
) -> None:
    start = {**stored, "a": 1}
    row = await _insert_target(name=_NAME, product="vault", extras=dict(start))

    refused = await _run(_operator(), row.id, **_body(form, start, changes))

    assert refused.status != "ok", refused
    assert VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN in str(refused.extras), refused
    assert _as_stored(await _extras(row.id)) == _as_stored(start)

    allowed = await _run(_operator(platform_admin=True), row.id, **_body(form, start, changes))

    assert allowed.status == "ok", allowed
    assert _as_stored(await _extras(row.id)) == _as_stored(apply_extras_changes(start, changes))
