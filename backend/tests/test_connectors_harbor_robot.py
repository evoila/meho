# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for Harbor robot lifecycle typed ops (G3.5-T9 #621).

Covers:
* ``harbor.robot.create`` handler — respx mock against Harbor API.
* ``harbor.robot.delete`` handler — respx mock against Harbor API.
* ``classify_op`` maps both op-ids to the right sensitivity classes.
* ``redact_payload("credential_mint", ...)`` — aggregate-only, no secret.
* No regression to ``credential_read`` classification for existing vault ops.

Auth: HTTP Basic (shared service account) — handlers pass ``raw_jwt=""``
to :meth:`HarborConnector.auth_headers`. Per-target credentials are
injected via the ``credentials_loader`` seam so the Vault stub is never
reached.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import respx

from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.harbor import HarborConnector, HarborTargetLike
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel

# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    """Re-register HarborConnector before each test, clear after.

    Mirrors the pattern in ``test_connectors_harbor_auth.py``.
    """
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="kv/data/harbor/harbor-test",
)


async def _stub_loader(_target: HarborTargetLike) -> dict[str, str]:
    return {"username": "admin", "password": "test-password"}


def _make_connector() -> HarborConnector:
    return HarborConnector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# harbor.robot.create — handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_create_posts_to_harbor_and_returns_id_name_secret() -> None:
    """robot_create() calls POST /api/v2.0/projects/{project}/robots and returns
    {id, name, secret} from the Harbor response."""
    connector = _make_connector()
    harbor_response = {
        "id": 42,
        "name": "robot$myproject+ci-push",
        "secret": "minted-secret-value",
        "creation_time": "2026-05-20T12:00:00.000Z",
        "expiration_time": -1,
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/myproject/robots").mock(
            return_value=respx.MockResponse(201, json=harbor_response)
        )
        result = await connector.robot_create(
            _TARGET,
            {"name": "ci-push", "project": "myproject", "duration": -1},
        )

    assert result == {"id": 42, "name": "robot$myproject+ci-push", "secret": "minted-secret-value"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_sends_basic_auth_header() -> None:
    """robot_create() includes the Authorization: Basic header on the POST request."""
    connector = _make_connector()
    captured_headers: dict[str, str] = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        captured_headers.update(dict(request.headers))
        return respx.MockResponse(
            201,
            json={"id": 1, "name": "robot$proj+bot", "secret": "s", "expiration_time": -1},
        )

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/proj/robots").mock(
            side_effect=_capture
        )
        await connector.robot_create(
            _TARGET,
            {"name": "bot", "project": "proj", "duration": 90},
        )

    assert "authorization" in captured_headers
    assert captured_headers["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_sends_correct_permission_body() -> None:
    """robot_create() sends the Harbor permission structure with push+pull access."""
    connector = _make_connector()
    captured_body: dict = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        import json as _json

        captured_body.update(_json.loads(request.content))
        return respx.MockResponse(
            201,
            json={"id": 10, "name": "robot$alpha+deployer", "secret": "x", "expiration_time": 90},
        )

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/alpha/robots").mock(
            side_effect=_capture
        )
        await connector.robot_create(
            _TARGET,
            {"name": "deployer", "project": "alpha", "duration": 90},
        )

    assert captured_body["name"] == "deployer"
    assert captured_body["duration"] == 90
    assert captured_body["level"] == "project"
    perms = captured_body["permissions"]
    assert len(perms) == 1
    assert perms[0]["namespace"] == "alpha"
    accesses = {a["action"] for a in perms[0]["access"]}
    assert "push" in accesses
    assert "pull" in accesses
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_raises_on_http_error() -> None:
    """robot_create() propagates httpx.HTTPStatusError on 4xx/5xx responses."""
    import httpx

    connector = _make_connector()
    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/myproject/robots").mock(
            return_value=respx.MockResponse(409, json={"errors": [{"code": "CONFLICT"}]})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.robot_create(
                _TARGET,
                {"name": "ci-push", "project": "myproject", "duration": -1},
            )
    await connector.aclose()


# ---------------------------------------------------------------------------
# harbor.robot.delete — handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_delete_sends_delete_to_harbor_and_returns_synthetic_result() -> None:
    """robot_delete() sends DELETE /api/v2.0/projects/{project}/robots/{id} and
    returns {id, deleted: True} since Harbor returns HTTP 200 with empty body."""
    connector = _make_connector()
    with respx.mock(assert_all_called=True) as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/projects/myproject/robots/42").mock(
            return_value=respx.MockResponse(200)
        )
        result = await connector.robot_delete(
            _TARGET,
            {"project": "myproject", "id": 42},
        )

    assert result == {"id": 42, "deleted": True}
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_sends_basic_auth_header() -> None:
    """robot_delete() includes the Authorization: Basic header on the DELETE request."""
    connector = _make_connector()
    captured_headers: dict[str, str] = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        captured_headers.update(dict(request.headers))
        return respx.MockResponse(200)

    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/projects/proj/robots/7").mock(
            side_effect=_capture
        )
        await connector.robot_delete(_TARGET, {"project": "proj", "id": 7})

    assert "authorization" in captured_headers
    assert captured_headers["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_raises_on_http_error() -> None:
    """robot_delete() propagates httpx.HTTPStatusError on 4xx/5xx responses."""
    import httpx

    connector = _make_connector()
    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/projects/myproject/robots/99").mock(
            return_value=respx.MockResponse(404)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.robot_delete(_TARGET, {"project": "myproject", "id": 99})
    await connector.aclose()


# ---------------------------------------------------------------------------
# G6 classifier — classify_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id, expected",
    [
        ("harbor.robot.create", "credential_mint"),
        ("harbor.robot.delete", "write"),
        # No regression to credential_read
        ("vault.kv.read", "credential_read"),
        ("vault.kv.list", "credential_read"),
        # Suffix-based write ops still work
        ("vsphere.vm.create", "write"),
        ("vsphere.vm.delete", "write"),
    ],
)
def test_classify_op_maps_harbor_robot_ops_correctly(op_id: str, expected: str) -> None:
    """classify_op returns credential_mint for harbor.robot.create, write for delete."""
    assert classify_op(op_id) == expected


def test_classify_op_credential_mint_precedes_write_suffix() -> None:
    """harbor.robot.create ends with .create but must classify as credential_mint,
    not write. Verifies the allowlist check precedes the suffix check."""
    assert classify_op("harbor.robot.create") == "credential_mint"
    # A hypothetical non-minting create still falls through to write
    assert classify_op("vsphere.vm.create") == "write"


# ---------------------------------------------------------------------------
# G6 redaction — redact_payload for credential_mint
# ---------------------------------------------------------------------------


def test_redact_payload_credential_mint_removes_secret_field() -> None:
    """redact_payload('credential_mint', ...) returns aggregate shape — no secret."""
    raw = {"id": 42, "name": "robot$proj+bot", "secret": "minted-secret", "other": "keep"}
    result = redact_payload("credential_mint", raw, "ok")

    assert "secret" not in str(result)
    assert "minted-secret" not in str(result)
    assert result["op_class"] == "credential_mint"
    assert result["result_status"] == "ok"


def test_redact_payload_credential_mint_preserves_only_aggregate_fields() -> None:
    """credential_mint aggregate payload is exactly {op_class, result_status}."""
    raw = {"id": 1, "name": "robot$p+n", "secret": "S3cr3t!"}
    result = redact_payload("credential_mint", raw, "ok")

    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_no_regression_on_credential_read() -> None:
    """credential_read still produces aggregate payload after the credential_mint
    extension — no regression to full-detail broadcast."""
    raw = {"data": {"db_password": "super-secret"}, "version": 3}
    result = redact_payload("credential_read", raw, "ok")

    assert "db_password" not in str(result)
    assert result["op_class"] == "credential_read"
    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_aggregate_on_error_result() -> None:
    """credential_mint aggregate applies regardless of result_status value."""
    raw = {"secret": "leaked-if-full", "error": "timeout"}
    result = redact_payload("credential_mint", raw, "error")

    assert "leaked-if-full" not in str(result)
    assert result["result_status"] == "error"


def test_redact_payload_write_class_broadcasts_full_detail() -> None:
    """harbor.robot.delete is write-classified — full detail (no secret in payload
    anyway, but the redaction path is full, not aggregate)."""
    raw = {"id": 42, "deleted": True}
    result = redact_payload("write", raw, "ok")

    assert result == {"op_class": "write", "params": raw, "result_status": "ok"}


def test_redact_payload_credential_mint_with_explicit_aggregate_detail() -> None:
    """When detail='aggregate' is passed explicitly, credential_mint stays aggregate."""
    raw = {"secret": "s", "id": 1}
    result = redact_payload("credential_mint", raw, "ok", detail="aggregate")

    assert "secret" not in str(result)
    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_with_explicit_full_detail() -> None:
    """When detail='full' is passed (G6.3 operator opt-in), credential_mint broadcasts
    the full payload. The G6.3 resolver is responsible for deciding this — the
    redact_payload function just renders the decided detail level."""
    raw = {"secret": "s", "id": 1, "name": "robot$p+n"}
    result = redact_payload("credential_mint", raw, "ok", detail="full")

    assert result == {"op_class": "credential_mint", "params": raw, "result_status": "ok"}
