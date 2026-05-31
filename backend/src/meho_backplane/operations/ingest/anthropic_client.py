# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Production :class:`LlmClient` for the spec-ingestion grouping pass.

The grouping pass (T3, :func:`run_llm_grouping`) needs an injected
:class:`~meho_backplane.operations.ingest.llm_groups.LlmClient` — a
``system_prompt + user_prompt -> raw text`` seam. The chassis shipped
only the fail-closed
:func:`~meho_backplane.operations.ingest.pipeline.default_llm_client_factory`
(raises :class:`LlmClientUnavailable` -> HTTP 503), so non-dry-run
``meho connector ingest --catalog <product>/<version>`` died on every
deployed backplane even though ``settings.anthropic_api_key`` was
already provisioned for the agent runtime.

This module closes that gap by **reusing the agent runtime's client
construction** rather than standing up a second provider integration:
the same :class:`anthropic.AsyncAnthropic` SDK client, the same
``settings.anthropic_api_key``, and the same
``_split_model_id(settings.agent_default_model)`` prefix handling that
:func:`meho_backplane.agent.models.anthropic_backend_builder` uses.
The only thing that differs is the *shape* — the agent runtime wants a
:class:`pydantic_ai.models.Model` for its tool-use loop, while the
grouping pass wants a one-shot Messages-API call returning raw text, so
this adapter talks to ``messages.create`` directly instead of through
the pydantic-ai ``Model`` wrapper.

:func:`build_anthropic_ingest_llm_client` is installed once at FastAPI
lifespan startup via
:func:`meho_backplane.api.v1.connectors_ingest.set_llm_client_factory`.
It stays **fail-closed**: a deploy with no ``ANTHROPIC_API_KEY`` still
raises :class:`LlmClientUnavailable` (-> 503) when the factory is
called, so a misconfigured chassis surfaces loudly instead of crashing
mid-grouping. A deploy that routes the agent runtime to a non-Anthropic
backend (Bedrock / vLLM / PAIF via G11.5) and sets no Anthropic key
gets the same 503 — wiring the grouping pass through the per-tenant
model resolver is a separate, larger change (it needs an ingest-time
tenant + egress story the build-time grouping pass does not have today).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from meho_backplane.operations.ingest.llm_groups import LlmClient
from meho_backplane.operations.ingest.pipeline import LlmClientUnavailable

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

__all__ = [
    "AnthropicMessagesLlmClient",
    "build_anthropic_ingest_llm_client",
]

_log = structlog.get_logger(__name__)


class AnthropicMessagesLlmClient:
    """:class:`LlmClient` backed by the Anthropic Messages API.

    Structurally satisfies the
    :class:`~meho_backplane.operations.ingest.llm_groups.LlmClient`
    Protocol — a single ``generate_json`` method. Retry/backoff and
    transport timeouts are owned by the injected
    :class:`anthropic.AsyncAnthropic` client (the SDK retries 429 /
    5xx / connection errors with exponential backoff by default), so
    this adapter holds no retry state of its own.

    One instance wraps one SDK client + one resolved model id; the
    grouping pipeline builds a fresh instance per ingest run (see
    :func:`build_anthropic_ingest_llm_client`), matching the
    per-resolve lifecycle of
    :func:`meho_backplane.agent.models.anthropic_backend_builder`.
    """

    def __init__(self, *, client: AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        """Return the model's raw text response (JSON validation is the caller's)."""
        from anthropic.types import MessageParam, TextBlock

        messages: list[MessageParam] = [{"role": "user", "content": user_prompt}]
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=max_output_tokens,
            system=system_prompt,
            messages=messages,
        )
        # The grouping prompts forbid tool use and ask for bare JSON, so
        # every content block is a TextBlock; concatenate their text and
        # let the T3 parsers (parse_proposal_response / parse_assignment_
        # response) own the JSON-shape validation and the LlmOutputInvalid
        # error envelope. A response with no text blocks yields "" — the
        # parser turns that into a clear LlmOutputInvalid rather than a
        # silent empty grouping.
        return "".join(block.text for block in message.content if isinstance(block, TextBlock))


def build_anthropic_ingest_llm_client() -> LlmClient:
    """Build the production grouping-pass :class:`LlmClient` from settings.

    The :data:`~meho_backplane.operations.ingest.pipeline.LlmClientFactory`
    installed at FastAPI lifespan startup. Reuses the agent runtime's
    Anthropic construction verbatim: ``settings.anthropic_api_key`` for
    auth and ``_split_model_id(settings.agent_default_model)`` to strip
    the pydantic-ai ``anthropic:`` provider prefix before the bare model
    id reaches the Messages API (which 404s on the prefixed form).

    Fail-closed: an empty ``anthropic_api_key`` raises
    :class:`LlmClientUnavailable`, which the REST route maps to HTTP 503
    and the CLI / MCP surfaces render as their own operator-facing
    error. This preserves the pre-wiring contract for deploys that never
    configured a key — they still fail loudly rather than constructing a
    client that 401s on the first grouping call.

    Imports are function-local, mirroring
    :func:`meho_backplane.agent.models.anthropic_backend_builder`: the
    ``anthropic`` SDK loads only when the factory is actually called,
    and the ``_split_model_id`` import avoids an import cycle between the
    ingest package and the agent package at module-load time.
    """
    from anthropic import AsyncAnthropic

    from meho_backplane.agent.invocation import _split_model_id
    from meho_backplane.settings import get_settings

    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise LlmClientUnavailable(
            "no ANTHROPIC_API_KEY configured for spec-ingestion grouping; "
            "the grouping pass reuses the agent runtime's Anthropic key "
            "(settings.anthropic_api_key). Set ANTHROPIC_API_KEY to run "
            "--catalog ingest grouping on this deploy, or route the agent "
            "runtime to an on-prem backend and accept that build-time-only "
            "grouping (a CI fixture injects a deterministic stub).",
        )
    _, model = _split_model_id(settings.agent_default_model)
    _log.info("ingest_llm_client_built", model=model)
    return AnthropicMessagesLlmClient(client=AsyncAnthropic(api_key=api_key), model=model)
