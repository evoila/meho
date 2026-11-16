# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A preview of a typed write shows what it will change (#5074).

With the ``PREVIEW_TYPED_WRITES`` switch off (the default), a typed write
that does not carry ``requires_approval`` (``targets.register``) and a
credential write with its own safe preview (``keycloak.user.reset_password``)
both answer ``status="unavailable"``, as before. With the switch on, both
preview. A credential write shows only its own plan: the params are never
copied, and a secret value never shows.

Most tests here run with the switch on. The tests named ``*_switch_off_*``
prove that with it off the answers, the hashes and the tool texts stay as
they were.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

import meho_backplane.connectors.keycloak.ops_write_preview as keycloak_preview
import meho_backplane.mcp.tools.operations  # noqa: F401 — registers the tool
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import tool_for_settings
from meho_backplane.mcp.tools._preview_typed_writes_text import (
    PREVIEW_OPERATION_TYPED_WRITES_SHORT,
    TYPED_WRITE_TEXT_SWAPS,
)
from meho_backplane.mcp.tools._short_tool_texts import PREVIEW_OPERATION_SHORT
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    has_preview_builder,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.redaction import clear_overrides
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import build_operator
from tests.test_operations_request_preview import (
    _FakeTarget,
    _handler_returning_ok,
    _make_operator,
    _RecordingHttpConnector,
)

_SECRET = "Pa55w0rd-made-up-value"


@pytest.fixture(autouse=True)
def _env_and_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("PREVIEW_TYPED_WRITES", "true")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    snapshot = dict(_PREVIEW_BUILDERS)
    if not has_preview_builder("keycloak.user.reset_password"):
        keycloak_preview._register_keycloak_preview_builders()
    yield
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(snapshot)
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _register(
    service: AsyncMock,
    op_id: str,
    *,
    safety_level: str,
    requires_approval: bool = False,
    schema: dict[str, Any] | None = None,
) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_RecordingHttpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_handler_returning_ok,
        summary="A typed op.",
        description="Typed.",
        parameter_schema=schema or {"type": "object"},
        safety_level=safety_level,
        requires_approval=requires_approval,
        when_to_use=None,
        embedding_service=service,
    )


async def _preview(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return await preview_dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id=op_id,
        target=_FakeTarget(product="demo", version="1.x", name="demo-prod"),
        params=params,
    )


@pytest.mark.asyncio
async def test_caution_write_without_requires_approval_previews(
    stub_embedding_service: AsyncMock,
) -> None:
    """``targets.register`` runs at once only from the web interface, and it still previews."""
    await _register(stub_embedding_service, "targets.register", safety_level="caution")
    params = {"name": "lab-a", "product": "demo"}

    envelope = await _preview("targets.register", params)

    assert envelope["status"] == "ok"
    assert envelope["method"] == "COMPOSITE"
    assert envelope["redacted_body"] == params
    assert envelope["requires_approval"] is False
    assert isinstance(envelope["preview_hash"], str)
    assert envelope["plan_shown"] is True
    assert envelope["proposed_effect"]["params_echo"]["name"] == "lab-a"


@pytest.mark.asyncio
async def test_different_params_give_different_hashes(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service, "targets.register", safety_level="caution")

    first = await _preview("targets.register", {"name": "lab-a"})
    second = await _preview("targets.register", {"name": "lab-b"})
    again = await _preview("targets.register", {"name": "lab-a"})

    assert first["preview_hash"] != second["preview_hash"]
    assert first["preview_hash"] == again["preview_hash"]


@pytest.mark.asyncio
async def test_safe_typed_read_stays_unavailable(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service, "demo.typed.read", safety_level="safe")

    envelope = await _preview("demo.typed.read", {})

    assert envelope["status"] == "unavailable"
    assert "redacted_body" not in envelope


@pytest.mark.asyncio
async def test_credential_write_with_builder_shows_the_plan_never_the_params(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(
        stub_embedding_service,
        "keycloak.user.reset_password",
        safety_level="dangerous",
        requires_approval=True,
    )
    params = {
        "username": "alice",
        "realm": "lab",
        "password_secret_ref": "secret/keycloak/alice",
        "password": _SECRET,
    }

    envelope = await _preview("keycloak.user.reset_password", params)

    assert envelope["status"] == "ok"
    assert envelope["redacted_body"] is None
    assert envelope["plan_shown"] is True
    preview = envelope["proposed_effect"]["preview"]
    assert preview["username"] == "alice"
    assert preview["realm"] == "lab"
    assert preview["password_secret_ref"] == "secret/keycloak/alice"
    assert _SECRET not in str(envelope)


@pytest.mark.asyncio
async def test_credential_write_hash_follows_every_param(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(
        stub_embedding_service,
        "keycloak.user.reset_password",
        safety_level="dangerous",
        requires_approval=True,
    )
    base = {"username": "alice", "realm": "lab", "password_secret_ref": "secret/a"}

    one = await _preview("keycloak.user.reset_password", {**base, "extra": _SECRET})
    two = await _preview("keycloak.user.reset_password", {**base, "extra": _SECRET + "x"})

    assert one["preview_hash"] != two["preview_hash"]
    assert _SECRET not in str(one) + str(two)


@pytest.mark.asyncio
async def test_credential_write_without_builder_stays_unavailable(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(
        stub_embedding_service,
        "k8s.secret.create",
        safety_level="dangerous",
        requires_approval=True,
    )
    _PREVIEW_BUILDERS.pop("k8s.secret.create", None)

    envelope = await _preview("k8s.secret.create", {"data": {"password": _SECRET}})

    assert envelope["status"] == "unavailable"
    assert "redacted_body" not in envelope
    assert _SECRET not in str(envelope)


def _switch(monkeypatch: pytest.MonkeyPatch, name: str, on: bool) -> None:
    monkeypatch.setenv(name, "true" if on else "false")
    get_settings.cache_clear()


def test_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREVIEW_TYPED_WRITES", raising=False)
    get_settings.cache_clear()
    assert get_settings().preview_typed_writes is False


@pytest.mark.asyncio
async def test_switch_off_caution_write_without_requires_approval_stays_unavailable(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", False)
    await _register(stub_embedding_service, "targets.register", safety_level="caution")

    envelope = await _preview("targets.register", {"name": "lab-a"})

    assert envelope["status"] == "unavailable"
    assert envelope["extras"]["reason"] == "not_ingested"
    assert "redacted_body" not in envelope
    assert "preview_hash" not in envelope


@pytest.mark.asyncio
async def test_switch_off_credential_write_with_builder_stays_unavailable(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", False)
    await _register(
        stub_embedding_service,
        "keycloak.user.reset_password",
        safety_level="dangerous",
        requires_approval=True,
    )
    params = {"username": "alice", "realm": "lab", "password_secret_ref": "secret/a"}

    envelope = await _preview("keycloak.user.reset_password", {**params, "password": _SECRET})

    assert envelope["status"] == "unavailable"
    assert "redacted_body" not in envelope
    assert "proposed_effect" not in envelope
    assert _SECRET not in str(envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "safety_level", "requires_approval"),
    [
        ("demo.typed.gated", "caution", True),
        ("demo.typed.delete", "destructive", True),
    ],
)
async def test_switch_does_not_change_gated_or_destructive_previews(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
) -> None:
    """An op that previewed before gives the same answer and hash in both states."""
    await _register(
        stub_embedding_service,
        op_id,
        safety_level=safety_level,
        requires_approval=requires_approval,
    )
    params = {"name": "lab-a", "size": 3}

    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", False)
    off = await _preview(op_id, params)
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", True)
    on = await _preview(op_id, params)

    assert off["status"] == "ok"
    assert off == on


def _shown_preview_tool() -> dict[str, Any]:
    definition = mcp_registry._TOOLS["preview_operation"][0]
    return tool_for_settings(definition, build_operator()).to_wire()


@pytest.mark.parametrize("short", [False, True], ids=["full-text", "short-text"])
def test_switch_off_tool_text_is_unchanged(monkeypatch: pytest.MonkeyPatch, short: bool) -> None:
    _switch(monkeypatch, "MCP_SHORT_TOOL_TEXTS", short)
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", False)
    registered = mcp_registry._TOOLS["preview_operation"][0]

    description = _shown_preview_tool()["description"]

    if short:
        assert description.endswith(PREVIEW_OPERATION_SHORT.description)
    else:
        assert description == registered.description
    assert "except a write" not in description
    assert "keycloak.user.reset_password" not in description


@pytest.mark.parametrize("short", [False, True], ids=["full-text", "short-text"])
def test_switch_on_tool_text_says_the_new_rule(
    monkeypatch: pytest.MonkeyPatch, short: bool
) -> None:
    _switch(monkeypatch, "MCP_SHORT_TOOL_TEXTS", short)
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", True)
    registered = mcp_registry._TOOLS["preview_operation"][0]

    shown = _shown_preview_tool()

    if short:
        assert shown["description"].endswith(PREVIEW_OPERATION_TYPED_WRITES_SHORT.description)
        assert "except a write" in shown["description"]
    else:
        for old, new in TYPED_WRITE_TEXT_SWAPS:
            assert old in registered.description
            assert old not in shown["description"]
            assert new in shown["description"]
    # Only the text changes: the input schema is the same in both states.
    _switch(monkeypatch, "PREVIEW_TYPED_WRITES", False)
    assert shown["inputSchema"] == _shown_preview_tool()["inputSchema"]
