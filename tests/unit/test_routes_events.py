# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for routes_events.py -- status code contracts and dedup suppressed counter."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from meho_app.api.routes_events import router
from meho_app.modules.connectors.event_service import EventService as _RealEventService


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)
    return a


def _make_registration(
    *,
    active: bool = True,
    require_signature: bool = True,
    rate_limit: int = 100,
):
    reg = MagicMock()
    reg.id = uuid4()
    reg.tenant_id = "tenant-1"
    reg.is_active = active
    reg.require_signature = require_signature
    reg.rate_limit_per_hour = rate_limit
    reg.connector_id = uuid4()
    reg.connector = MagicMock()
    reg.connector.name = "test-connector"
    reg.prompt_template = "Investigate: {{ payload }}"
    reg.encrypted_secret = "enc-secret"
    return reg


def _make_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def patched_deps():
    """Patch all external dependencies for routes_events."""
    reg = _make_registration()
    secret = "test-secret-32-chars-xxxxxxxxxxx"
    body = json.dumps({"alert": "disk full"}).encode()

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_maker = MagicMock(return_value=mock_session)

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # not a duplicate by default
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)
    mock_redis.get = AsyncMock(return_value=None)

    mock_event_service = AsyncMock()
    mock_event_service.get_event_registration = AsyncMock(return_value=reg)
    mock_event_service.decrypt_secret = MagicMock(return_value=secret)
    mock_event_service.log_event = AsyncMock()

    config = MagicMock()
    config.redis_url = "redis://localhost:6379"

    return {
        "reg": reg,
        "secret": secret,
        "body": body,
        "mock_db": mock_db,
        "mock_session_maker": mock_session_maker,
        "mock_redis": mock_redis,
        "mock_event_service": mock_event_service,
        "config": config,
    }


@pytest.mark.asyncio
async def test_receive_event_returns_202_on_success(app, patched_deps):
    """Happy path: valid signature, not a dup, not rate-limited → 202."""
    d = patched_deps
    sig = _make_signature(d["body"], d["secret"])

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ),
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
        patch(
            "meho_app.api.routes_events.EventService.is_duplicate",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "meho_app.api.routes_events.EventService.is_rate_limited",
            new=AsyncMock(return_value=False),
        ),
        patch("meho_app.api.routes_events.execute_event_investigation"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={"X-Webhook-Signature": sig, "Content-Type": "application/json"},
            )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_receive_event_duplicate_returns_202(app, patched_deps):
    """Duplicate payload → 202 with status=duplicate_suppressed."""
    d = patched_deps
    sig = _make_signature(d["body"], d["secret"])
    d["mock_redis"].get = AsyncMock(return_value=b"3")

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ),
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
        patch(
            "meho_app.api.routes_events.EventService.is_duplicate", new=AsyncMock(return_value=True)
        ),
        patch(
            "meho_app.api.routes_events.EventService.is_rate_limited",
            new=AsyncMock(return_value=False),
        ),
        patch("meho_app.api.routes_events.execute_event_investigation"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={"X-Webhook-Signature": sig, "Content-Type": "application/json"},
            )

    assert resp.status_code == 202
    assert resp.json()["status"] == "duplicate_suppressed"


@pytest.mark.asyncio
async def test_duplicate_branch_passes_suppressed_count_to_log_event(app, patched_deps):
    """Dedup branch reads GET and passes suppressed_count to log_event."""
    d = patched_deps
    sig = _make_signature(d["body"], d["secret"])
    d["mock_redis"].get = AsyncMock(return_value=b"5")

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ),
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
        patch(
            "meho_app.api.routes_events.EventService.is_duplicate", new=AsyncMock(return_value=True)
        ),
        patch(
            "meho_app.api.routes_events.EventService.is_rate_limited",
            new=AsyncMock(return_value=False),
        ),
        patch("meho_app.api.routes_events.execute_event_investigation"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={"X-Webhook-Signature": sig, "Content-Type": "application/json"},
            )

    d["mock_event_service"].log_event.assert_awaited_once()
    call_kwargs = d["mock_event_service"].log_event.call_args.kwargs
    assert call_kwargs["duplicates_suppressed"] == 5


@pytest.mark.asyncio
async def test_duplicate_branch_suppressed_count_defaults_to_zero(app, patched_deps):
    """If Redis has no suppressed counter, duplicates_suppressed=0."""
    d = patched_deps
    sig = _make_signature(d["body"], d["secret"])
    d["mock_redis"].get = AsyncMock(return_value=None)

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ),
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
        patch(
            "meho_app.api.routes_events.EventService.is_duplicate", new=AsyncMock(return_value=True)
        ),
        patch(
            "meho_app.api.routes_events.EventService.is_rate_limited",
            new=AsyncMock(return_value=False),
        ),
        patch("meho_app.api.routes_events.execute_event_investigation"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={"X-Webhook-Signature": sig, "Content-Type": "application/json"},
            )

    call_kwargs = d["mock_event_service"].log_event.call_args.kwargs
    assert call_kwargs["duplicates_suppressed"] == 0


@pytest.mark.asyncio
async def test_receive_event_missing_signature_returns_401(app, patched_deps):
    """No X-Webhook-Signature header -> 401 with the new error detail string.

    Patches EventService as a class mock that preserves the real
    verify_hmac_signature staticmethod so the 401 code path actually
    executes against production HMAC logic.
    """
    d = patched_deps

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ) as mock_es_cls,
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
    ):
        mock_es_cls.verify_hmac_signature = _RealEventService.verify_hmac_signature
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={"Content-Type": "application/json"},  # no signature header
            )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing X-Webhook-Signature header"


@pytest.mark.asyncio
async def test_receive_event_invalid_signature_returns_401(app, patched_deps):
    """Wrong X-Webhook-Signature -> 401 with 'Invalid event signature' detail."""
    d = patched_deps

    with (
        patch("meho_app.api.routes_events.get_session_maker", return_value=d["mock_session_maker"]),
        patch("meho_app.api.routes_events.get_redis_client", return_value=d["mock_redis"]),
        patch(
            "meho_app.api.routes_events.EventService",
            return_value=d["mock_event_service"],
        ) as mock_es_cls,
        patch("meho_app.api.routes_events.get_api_config", return_value=d["config"]),
    ):
        mock_es_cls.verify_hmac_signature = _RealEventService.verify_hmac_signature
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/events/{d['reg'].id}",
                content=d["body"],
                headers={
                    "X-Webhook-Signature": "deadbeef",  # wrong digest
                    "Content-Type": "application/json",
                },
            )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid event signature"
