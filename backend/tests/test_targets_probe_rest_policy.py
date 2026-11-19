# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/targets/{name}/probe`` follows the per-target rules of ``targets.probe``.

* An agent or service login that is denied on the target gets 403. The
  connector is not called and the stored detected product and version do
  not change. The agent is denied by a deny grant for the target, the
  service login by a read scope that does not name the target.
* The same login still probes a target it is not denied on.
* A person probes as before.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.targets.probe import TARGETS_PROBE_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ServicePrincipalReadScope

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import _build_app
from .test_targets_probe_op import (  # noqa: F401  (fixtures)
    _deny_on_target,
    _fingerprint,
    _registered_op,
    _seed,
    _settings_env,
    _stored_fingerprint,
    _use_connector,
)

_LAST = {"vendor": "example-vendor", "product": "probeop", "version": "1.0.0"}
_KEY = make_rsa_keypair("kid-probe-rest-policy")


async def _deny(kind: str, sub: str, target_id: uuid.UUID) -> None:
    """Deny *sub* the probe of *target_id*: a deny grant, or a read scope without it."""
    if kind == "agent":
        await _deny_on_target(sub, target_id)
        return
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalReadScope(
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                principal_sub=sub,
                entries=[{"target_name_pattern": "other-system"}],
                set_by_sub="person-admin",
                set_at=datetime.now(UTC),
            )
        )
        await session.commit()


def _rest_probe(name: str, *, sub: str, kind: str | None) -> Any:
    key = _KEY
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind=kind,
        extra_claims={"azp": f"{sub}-client"} if kind else None,
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return TestClient(_build_app()).post(
            f"/api/v1/targets/{name}/probe",
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.parametrize("kind", ["agent", "service"])
async def test_a_machine_login_denied_on_the_target_gets_403(kind: str) -> None:
    asked = _use_connector(_fingerprint())
    denied = await _seed("denied-system", fingerprint=_LAST)
    other = await _seed("other-system")
    sub = f"{kind}-denied"
    await _deny(kind, sub, denied.id)

    response = _rest_probe("denied-system", sub=sub, kind=kind)

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "denied"
    assert detail["op_id"] == TARGETS_PROBE_OP_ID
    assert "denied-system" in detail["message"]
    assert asked == []
    assert await _stored_fingerprint(denied.id) == _LAST

    allowed = _rest_probe("other-system", sub=sub, kind=kind)
    assert allowed.status_code == 200, allowed.text
    assert asked == ["other-system"]
    assert await _stored_fingerprint(other.id) is not None


async def test_a_person_probes_through_rest() -> None:
    asked = _use_connector(_fingerprint())
    row = await _seed("person-system")

    response = _rest_probe("person-system", sub="person-1", kind=None)

    assert response.status_code == 200, response.text
    assert asked == ["person-system"]
    assert (await _stored_fingerprint(row.id) or {})["version"] == "2.4.1"
