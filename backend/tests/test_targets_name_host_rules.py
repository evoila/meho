# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Name and host format rules for new and changed targets (#5244).

* ``name`` and each ``aliases`` entry: no blank value, no space, tab, line
  break or other control character, no ``/``.
* ``host`` and ``fqdn``: the host part MEHO connects to must be a valid
  IPv4 address, IPv6 address or DNS host name. Port and path shapes stay
  allowed.
* A row saved before the rules existed still resolves and can be deleted.

The suite-wide SSRF allowlist (``_default_target_ssrf_allowlist`` in
``conftest.py``) plus the documentation ranges added below let the example
addresses through the SSRF guard, so a refusal here comes from the format
check.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from meho_backplane.targets import TargetCreate, TargetUpdate
from meho_backplane.targets.ssrf_guard import TARGET_SSRF_ALLOWLIST_ENV

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _operator_token,
    _settings_env,  # noqa: F401
)

BAD_NAMES = [
    pytest.param("node-01 157", id="space"),
    pytest.param(" node-01", id="leading-space"),
    pytest.param("node-01\t157", id="tab"),
    pytest.param("node-01\n157", id="line-break"),
    pytest.param("node\x0001", id="control-char"),
    pytest.param("node/24", id="slash"),
    pytest.param("   ", id="only-spaces"),
]

BAD_HOSTS = [
    pytest.param("10.0.0.", id="ipv4-trailing-dot"),
    pytest.param("10.0.0", id="ipv4-three-parts"),
    pytest.param("10.0.0.300", id="ipv4-out-of-range"),
    pytest.param("not a host", id="spaces"),
    pytest.param("-bad-.example", id="label-starts-with-dash"),
]

GOOD_HOSTS = [
    "10.0.0.5",
    "node-01.example.com",
    "node-01.example.com.",
    "2001:db8::1",
    "db-01:27017",
    "owner/repo",
    "api.github.com/repos/owner/repo",
]


@pytest.fixture(autouse=True)
def _allow_documentation_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    current = os.environ.get(TARGET_SSRF_ALLOWLIST_ENV, "")
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, f"{current},192.0.2.0/24,2001:db8::/32")


def _create(**overrides: Any) -> TargetCreate:
    body: dict[str, Any] = {"name": "node-01", "product": "ssh", "host": "192.0.2.10"}
    body.update(overrides)
    return TargetCreate(**body)


def _error_loc(exc: pytest.ExceptionInfo[ValidationError]) -> tuple[Any, ...]:
    errors = exc.value.errors()
    assert len(errors) == 1
    return tuple(errors[0]["loc"])


# ---------------------------------------------------------------------------
# Model rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_create_refuses_bad_name(name: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _create(name=name)
    assert _error_loc(exc) == ("name",)
    assert "name must not" in str(exc.value)


@pytest.mark.parametrize("alias", BAD_NAMES)
def test_create_and_update_refuse_bad_alias(alias: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _create(aliases=["ok-alias", alias])
    assert _error_loc(exc) == ("aliases",)
    with pytest.raises(ValidationError) as exc:
        TargetUpdate(aliases=[alias])
    assert _error_loc(exc) == ("aliases",)
    assert "aliases must not" in str(exc.value)


@pytest.mark.parametrize("field", ["host", "fqdn"])
@pytest.mark.parametrize("value", BAD_HOSTS)
def test_create_and_update_refuse_bad_host(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _create(**{field: value})
    assert _error_loc(exc) == (field,)
    with pytest.raises(ValidationError) as exc:
        TargetUpdate(**{field: value})
    assert _error_loc(exc) == (field,)


@pytest.mark.parametrize("field", ["host", "fqdn"])
@pytest.mark.parametrize("value", GOOD_HOSTS)
def test_create_and_update_accept_good_host(field: str, value: str) -> None:
    assert getattr(_create(**{field: value}), field) == value
    assert getattr(TargetUpdate(**{field: value}), field) == value


def test_good_names_and_aliases_pass() -> None:
    body = _create(name="node-01.example_A", aliases=["vc01", "vc-01.example.com"])
    assert body.name == "node-01.example_A"
    assert TargetUpdate(aliases=[]).aliases == []


# ---------------------------------------------------------------------------
# REST API: POST / PATCH answer 422 naming the field
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _failed_fields(response: Any) -> set[str]:
    assert response.status_code == 422, response.text
    return {str(err["loc"][-1]) for err in response.json()["detail"]}


def test_post_issue_example_names_name_and_host(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "node-01 157", "product": "linux", "host": "10.0.0."},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert _failed_fields(response) == {"name", "host"}


@pytest.mark.parametrize("name", BAD_NAMES)
def test_post_bad_name_is_422_not_500(client: TestClient, name: str) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": name, "product": "ssh", "host": "192.0.2.10"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert _failed_fields(response) == {"name"}


@pytest.mark.parametrize("field", ["host", "fqdn"])
@pytest.mark.parametrize("value", BAD_HOSTS)
def test_post_bad_host_is_422(client: TestClient, field: str, value: str) -> None:
    body = {"name": "node-01", "product": "ssh", "host": "192.0.2.10", field: value}
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json=body,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert _failed_fields(response) == {field}


@pytest.mark.parametrize("value", GOOD_HOSTS)
def test_post_good_host_is_201(client: TestClient, value: str) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "node-01", "product": "ssh", "host": value},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201, response.text
    assert response.json()["host"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"aliases": ["node-01 157"]}, "aliases"),
        ({"aliases": ["node/24"]}, "aliases"),
        ({"host": "10.0.0."}, "host"),
        ({"host": "-bad-.example"}, "host"),
        ({"fqdn": "not a host"}, "fqdn"),
        ({"fqdn": "10.0.0"}, "fqdn"),
    ],
)
async def test_patch_bad_value_is_422(client: TestClient, body: dict[str, Any], field: str) -> None:
    await _insert_target(tenant_id=uuid.UUID(DEFAULT_TENANT_ID), name="node-01")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/node-01",
            json=body,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert _failed_fields(response) == {field}


# ---------------------------------------------------------------------------
# Rows saved before the rules existed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_style_row_still_resolves_and_deletes(client: TestClient) -> None:
    """A row written straight to the database keeps working by name."""
    await _insert_target(
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        name="node-01 157",
        aliases=["old alias"],
        host="10.0.0.",
    )
    key = make_rsa_keypair("kid-A")
    path = "/api/v1/targets/node-01%20157"
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        read = client.get(path, headers={"Authorization": f"Bearer {_operator_token(key)}"})
        assert read.status_code == 200, read.text
        assert read.json()["host"] == "10.0.0."
        by_alias = client.get(
            "/api/v1/targets/old%20alias",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
        assert by_alias.status_code == 200, by_alias.text
        deleted = client.delete(path, headers={"Authorization": f"Bearer {_admin_token(key)}"})
        assert deleted.status_code == 204, deleted.text
        gone = client.get(path, headers={"Authorization": f"Bearer {_operator_token(key)}"})
    assert gone.status_code == 404
