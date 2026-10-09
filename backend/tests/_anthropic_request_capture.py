# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Capture the Messages API request an Anthropic agent model sends.

Used by the tests that pin the agent output-token cap
(``_AGENT_MAX_OUTPUT_TOKENS``) on both Anthropic builders. The request
goes through the real ``anthropic`` SDK. Only the HTTP transport is
replaced, so the tests see the exact JSON body the SDK would send.

The ``anthropic`` 1.x SDK sends with ``httpx2``, which ``respx`` cannot
see. So we patch ``httpx2.AsyncHTTPTransport.handle_async_request``.

No network is used. :func:`point_anthropic_sdk_at_nowhere` also sets
``ANTHROPIC_BASE_URL`` to a ``.invalid`` host, so a request that ever
got past the patch could not reach a real API.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.models import Model

#: A minimal non-streamed Messages API reply: one text block.
_TEXT_REPLY: dict[str, Any] = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def point_anthropic_sdk_at_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``AsyncAnthropic`` built after this call target a ``.invalid`` host.

    Call it before the builder runs: the SDK reads the variable when the
    client is built.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://anthropic.invalid")


async def capture_first_request(model: Model, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run one agent turn on ``model`` and return the JSON body it sent."""
    bodies: list[dict[str, Any]] = []

    async def _fake_send(
        self: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(200, json=_TEXT_REPLY)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _fake_send)
    try:
        result = await Agent(model).run("hi")
    except Exception as exc:
        # A streamed request cannot parse the plain reply above. Name what
        # was sent, so the failure says why instead of a parse error.
        sent = bodies[-1] if bodies else {}
        raise AssertionError(
            f"agent turn failed; the request had max_tokens={sent.get('max_tokens')}, "
            f"stream={sent.get('stream')}"
        ) from exc
    assert result.output == "ok"
    assert len(bodies) == 1
    return bodies[0]
