# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit contract for the production spec-ingestion LLM client (#1386).

Covers the two pieces ``build_anthropic_ingest_llm_client`` adds:

* the factory's settings reuse + fail-closed posture (mirrors the agent
  runtime's ``anthropic_backend_builder`` — same key, same
  ``_split_model_id`` prefix handling), and
* the ``AnthropicMessagesLlmClient`` adapter's mapping of the
  ``generate_json`` Protocol onto an Anthropic Messages-API call.

No network: the factory tests construct a real (but unused)
``AsyncAnthropic`` with a fake key, and the adapter tests mock the SDK
client so ``messages.create`` never leaves the process. The real-SDK
tests point the SDK at a ``.invalid`` host and replace only its
``httpx2`` transport, so they read the request the SDK really built.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx2
import pytest
from anthropic.types import TextBlock

from meho_backplane.operations.ingest import (
    AnthropicMessagesLlmClient,
    build_anthropic_ingest_llm_client,
)
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable
from meho_backplane.settings import get_settings

from ._anthropic_request_capture import _TEXT_REPLY, point_anthropic_sdk_at_nowhere
from ._httpx2_capture import (
    ca_common_names,
    capture_httpx2_requests,
    client_ssl_context,
    write_test_ca,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires + clear the cache.

    Same shape as ``test_agent_model_resolver``'s fixture: each test
    mutates ``ANTHROPIC_API_KEY`` / ``AGENT_DEFAULT_MODEL`` and relies on
    a fresh ``get_settings()`` read.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Factory: settings reuse + fail-closed
# ---------------------------------------------------------------------------


def test_factory_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``ANTHROPIC_API_KEY`` -> ``LlmClientUnavailable`` (route maps to 503)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(LlmClientUnavailable, match="ANTHROPIC_API_KEY"):
        build_anthropic_ingest_llm_client()


def test_factory_strips_provider_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec-form ``anthropic:`` prefix is stripped before the Messages API.

    Reuses the agent runtime's ``_split_model_id`` handling — the bare id
    is what reaches ``messages.create`` (the prefixed form 404s).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-ingest-test")
    monkeypatch.delenv("AGENT_DEFAULT_MODEL", raising=False)
    get_settings.cache_clear()

    client = build_anthropic_ingest_llm_client()
    assert isinstance(client, AnthropicMessagesLlmClient)
    # Default agent_default_model is "anthropic:claude-sonnet-4-6".
    assert client._model == "claude-sonnet-4-6"


def test_factory_accepts_bare_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploy-supplied bare model id passes through unchanged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-ingest-test")
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", "claude-haiku-4-5")
    get_settings.cache_clear()

    client = build_anthropic_ingest_llm_client()
    assert isinstance(client, AnthropicMessagesLlmClient)
    assert client._model == "claude-haiku-4-5"


def test_factory_constructs_client_with_explicit_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grouping-pass ``AsyncAnthropic`` is built with explicit bounds (#2275).

    The SDK defaults (10-min read timeout, retried) let a hung grouping
    call pend ~30 min and outlive the ingest-job watchdog. The factory
    must pass explicit ``timeout`` + ``max_retries`` so a stuck LLM call
    fails fast instead of silently consuming the watchdog budget. Patch
    the SDK constructor (function-local ``from anthropic import
    AsyncAnthropic``) and assert the kwargs.
    """
    import anthropic

    from meho_backplane.operations.ingest.anthropic_client import (
        _INGEST_LLM_MAX_RETRIES,
        _INGEST_LLM_TIMEOUT_SECONDS,
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-ingest-test")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)

    client = build_anthropic_ingest_llm_client()

    assert isinstance(client, AnthropicMessagesLlmClient)
    # Explicit bounds pinned to the module constants (not the SDK defaults).
    assert captured["timeout"] == _INGEST_LLM_TIMEOUT_SECONDS
    assert captured["max_retries"] == _INGEST_LLM_MAX_RETRIES
    # The fail-closed key contract is unchanged: the key is still forwarded.
    assert captured["api_key"] == "fake-key-for-ingest-test"


# ---------------------------------------------------------------------------
# Adapter: generate_json -> Messages API
# ---------------------------------------------------------------------------


async def test_generate_json_calls_messages_api_with_mapped_args() -> None:
    """``generate_json`` maps the Protocol kwargs onto ``messages.create``.

    The grouping path passes no ``response_format``, so the request must
    carry the exact kwarg set it always has — no ``output_config`` member
    (#1999: the structured-output param defaults off so this caller is
    byte-for-byte unchanged).
    """
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[TextBlock(type="text", text="grouped-json", citations=None)],
            stop_reason="end_turn",
        ),
    )
    adapter = AnthropicMessagesLlmClient(client=mock_client, model="claude-sonnet-4-6")

    result = await adapter.generate_json(
        system_prompt="you group ops",
        user_prompt="here are the ops",
        max_output_tokens=4096,
    )

    assert result == "grouped-json"
    mock_client.messages.create.assert_awaited_once_with(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system="you group ops",
        messages=[{"role": "user", "content": "here are the ops"}],
    )


async def test_generate_json_concatenates_only_text_blocks() -> None:
    """Multiple text blocks join; non-text blocks are filtered out."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[
                TextBlock(type="text", text="[part-1]", citations=None),
                # A non-TextBlock block (e.g. a thinking block) must be skipped
                # even though it carries a ``.text`` attribute.
                SimpleNamespace(type="thinking", text="[ignored]"),
                TextBlock(type="text", text="[part-2]", citations=None),
            ],
            stop_reason="end_turn",
        ),
    )
    adapter = AnthropicMessagesLlmClient(client=mock_client, model="claude-sonnet-4-6")

    result = await adapter.generate_json(
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=512,
    )

    assert result == "[part-1][part-2]"


async def test_generate_json_empty_content_returns_empty_string() -> None:
    """No text blocks -> "" (the T3 parser turns that into LlmOutputInvalid)."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=SimpleNamespace(content=[], stop_reason="end_turn"),
    )
    adapter = AnthropicMessagesLlmClient(client=mock_client, model="claude-sonnet-4-6")

    result = await adapter.generate_json(
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=512,
    )

    assert result == ""


async def test_generate_structured_json_threads_stop_reason_and_text() -> None:
    """``generate_structured_json`` returns text + ``stop_reason`` (#1999).

    Without a ``response_format`` the request is identical to the grouping
    call (no ``output_config`` kwarg), but the richer result carries the
    ``stop_reason`` the answer legs need to split a truncation fault.
    """
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[TextBlock(type="text", text='{"answer": "x"}', citations=None)],
            stop_reason="max_tokens",
        ),
    )
    adapter = AnthropicMessagesLlmClient(client=mock_client, model="claude-sonnet-4-6")

    result = await adapter.generate_structured_json(
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=2048,
    )

    assert result.text == '{"answer": "x"}'
    assert result.stop_reason == "max_tokens"
    mock_client.messages.create.assert_awaited_once_with(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="s",
        messages=[{"role": "user", "content": "u"}],
    )


async def test_generate_structured_json_passes_output_config_when_schema_given() -> None:
    """A ``response_format`` is forwarded as the Messages-API ``output_config`` (#1999)."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[TextBlock(type="text", text='{"answer": "x"}', citations=None)],
            stop_reason="end_turn",
        ),
    )
    adapter = AnthropicMessagesLlmClient(client=mock_client, model="claude-sonnet-4-6")
    schema = {"type": "json_schema", "schema": {"type": "object"}}

    await adapter.generate_structured_json(
        system_prompt="s",
        user_prompt="u",
        max_output_tokens=2048,
        response_format=schema,
    )

    mock_client.messages.create.assert_awaited_once_with(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="s",
        messages=[{"role": "user", "content": "u"}],
        output_config={"format": schema},
    )


# ---------------------------------------------------------------------------
# Real SDK requests: the pinned values reach the wire
# ---------------------------------------------------------------------------


def _build_real_client(monkeypatch: pytest.MonkeyPatch) -> AnthropicMessagesLlmClient:
    """Build the production client with a real ``AsyncAnthropic`` on a ``.invalid`` host."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-ingest-test")
    point_anthropic_sdk_at_nowhere(monkeypatch)
    get_settings.cache_clear()
    client = build_anthropic_ingest_llm_client()
    assert isinstance(client, AnthropicMessagesLlmClient)
    return client


async def test_real_sdk_request_carries_max_tokens_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request the SDK really sends has our ``max_tokens`` and 120-second timeout.

    The other tests here mock the SDK, so they only show what we pass to
    it. This one replaces only the ``httpx2`` transport and reads the
    request the SDK built.
    """
    from meho_backplane.operations.ingest.anthropic_client import _INGEST_LLM_TIMEOUT_SECONDS

    sent = capture_httpx2_requests(
        monkeypatch, lambda _request: httpx2.Response(200, json=_TEXT_REPLY)
    )
    client = _build_real_client(monkeypatch)

    text = await client.generate_json(
        system_prompt="sys", user_prompt="usr", max_output_tokens=2048
    )

    assert text == "ok"
    assert len(sent) == 1
    request = sent[0]
    assert str(request.url) == "http://anthropic.invalid/v1/messages"
    body = json.loads(request.content)
    assert body["max_tokens"] == 2048
    assert body["model"] == "claude-sonnet-4-6"
    assert "stream" not in body
    # httpx2 carries the timeout each request uses in its extensions.
    assert request.extensions["timeout"] == {
        "connect": _INGEST_LLM_TIMEOUT_SECONDS,
        "read": _INGEST_LLM_TIMEOUT_SECONDS,
        "write": _INGEST_LLM_TIMEOUT_SECONDS,
        "pool": _INGEST_LLM_TIMEOUT_SECONDS,
    }


async def test_real_sdk_retries_a_failed_request_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server error is retried once, then fails: two requests in total.

    ``retry-after-ms`` keeps the SDK's wait before the retry at 1 ms, so
    the test stays fast.
    """
    from meho_backplane.operations.ingest.anthropic_client import _INGEST_LLM_MAX_RETRIES

    sent = capture_httpx2_requests(
        monkeypatch,
        lambda _request: httpx2.Response(
            500,
            headers={"retry-after-ms": "1"},
            json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        ),
    )
    client = _build_real_client(monkeypatch)

    with pytest.raises(anthropic.InternalServerError):
        await client.generate_json(system_prompt="sys", user_prompt="usr", max_output_tokens=16)

    assert len(sent) == 1 + _INGEST_LLM_MAX_RETRIES == 2


def test_real_sdk_client_trusts_ssl_cert_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``SSL_CERT_FILE`` set, the SDK client trusts that CA file.

    ``httpx2`` reads the variable when the SDK builds its client. Without
    it, the client still checks certificates, against the system store
    only, so the test CA is not trusted.
    """
    ca_file = write_test_ca(tmp_path, common_name="MEHO ingest test CA")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    sdk_client = _build_real_client(monkeypatch)._client
    ssl_context = client_ssl_context(sdk_client._client)
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert "MEHO ingest test CA" not in ca_common_names(ssl_context)

    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    sdk_client = _build_real_client(monkeypatch)._client
    assert "MEHO ingest test CA" in ca_common_names(client_ssl_context(sdk_client._client))


# ---------------------------------------------------------------------------
# Lifespan wiring (#1386): startup installs the production factory
# ---------------------------------------------------------------------------


def test_lifespan_helper_installs_production_factory() -> None:
    """``_wire_ingest_llm_client`` replaces the holder with the production factory.

    This is the crux of #1386: before the wire-up the holder is the
    fail-closed default; after it, every surface that reads
    ``get_llm_client_factory()`` (REST route, MCP tool, CLI via REST)
    resolves ``build_anthropic_ingest_llm_client``.
    """
    from meho_backplane.api.v1.connectors_ingest import (
        default_llm_client_factory,
        get_llm_client_factory,
        set_llm_client_factory,
    )
    from meho_backplane.main import _wire_ingest_llm_client

    previous = set_llm_client_factory(default_llm_client_factory)
    try:
        assert get_llm_client_factory() is default_llm_client_factory
        _wire_ingest_llm_client()
        assert get_llm_client_factory() is build_anthropic_ingest_llm_client
    finally:
        set_llm_client_factory(previous)
