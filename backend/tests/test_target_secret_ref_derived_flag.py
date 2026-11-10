# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The REST target answers show the stored ``secret_ref`` and whether MEHO built it.

A target added without a ``secret_ref`` gets ``tenants/<tenant_id>/<name>``.
The create and read answers carry that ref and ``secret_ref_derived``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401  (autouse fixture)
    _isolated_jwks_cache,  # noqa: F401  (autouse fixture)
    _settings_env,  # noqa: F401  (autouse fixture)
)


@pytest.fixture
def rest_client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.mark.parametrize(
    ("extra", "expected_ref", "expected_derived"),
    [
        ({}, f"tenants/{DEFAULT_TENANT_ID}/linux-01", True),
        (
            {"secret_ref": f"tenants/{DEFAULT_TENANT_ID}/linux"},
            f"tenants/{DEFAULT_TENANT_ID}/linux",
            False,
        ),
        ({"secret_ref": None}, None, False),
    ],
    ids=["omitted", "set", "null"],
)
def test_rest_create_and_read_show_secret_ref_and_derived(
    rest_client: TestClient,
    extra: dict[str, Any],
    expected_ref: str | None,
    expected_derived: bool,
) -> None:
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = rest_client.post(
            "/api/v1/targets",
            json={"name": "linux-01", "product": "ssh", "host": "linux-01.example", **extra},
            headers=headers,
        )
        read = rest_client.get("/api/v1/targets/linux-01", headers=headers)

    assert created.status_code == 201
    for body in (created.json(), read.json()):
        assert body["secret_ref"] == expected_ref
        assert body["secret_ref_derived"] is expected_derived
