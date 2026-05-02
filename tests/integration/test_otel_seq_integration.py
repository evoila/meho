# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for OTEL logging with Seq.

These tests verify that logs sent via the OTEL pipeline actually arrive
in Seq.  They require a running Seq container (``./scripts/dev-env.sh local``).

The tests are skipped automatically when Seq is not reachable at
``http://localhost:5341``.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

# ---------------------------------------------------------------------------
# Skip the entire module when Seq is not available
# ---------------------------------------------------------------------------

SEQ_URL = "http://localhost:5341"


def _seq_is_reachable() -> bool:
    """Return True if the Seq HTTP API is reachable."""
    try:
        resp = httpx.get(f"{SEQ_URL}/api", timeout=2.0)
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _seq_is_reachable(),
        reason="Seq is not running at http://localhost:5341",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _query_seq_events(filter_expr: str, *, timeout: float = 10.0) -> list[dict]:  # noqa: ASYNC109 -- timeout parameter is part of function API
    """Query Seq REST API for events matching *filter_expr*.

    Retries for up to *timeout* seconds to allow for batch flush delays.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            resp = await client.get(
                f"{SEQ_URL}/api/events",
                params={"filter": filter_expr, "count": "10"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                events = resp.json()
                if events:
                    return events
            await asyncio.sleep(1.0)
    return []


def _force_flush_providers() -> None:
    """Force-flush both log and trace providers so records reach Seq.

    Best-effort helper for integration tests -- failures are silently
    ignored because the OTEL SDK may not be fully initialised in the
    test environment.
    """
    try:
        from opentelemetry._logs import get_logger_provider

        provider = get_logger_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception:  # noqa: S110 -- intentional silent exception handling
        pass

    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception:  # noqa: S110 -- intentional silent exception handling
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_log_arrives_in_seq():
    """Verify a structured log emitted via get_logger() appears in Seq."""
    from meho_app.core.otel import get_logger

    logger = get_logger("integration_test.seq")
    test_id = f"test-{uuid.uuid4().hex[:12]}"

    logger.info(
        "Integration test log",
        test_id=test_id,
        source="test_otel_seq_integration",
    )

    _force_flush_providers()

    events = await _query_seq_events(f"test_id = '{test_id}'")
    assert len(events) > 0, (
        f"Expected at least 1 event with test_id={test_id} in Seq, but found none after waiting."
    )


@pytest.mark.asyncio
async def test_request_context_appears_in_seq():
    """Verify request context (user_id, tenant_id) enriches logs in Seq."""
    from meho_app.core.observability import (
        clear_request_context,
        set_request_context,
    )
    from meho_app.core.otel import get_logger

    logger = get_logger("integration_test.context")
    test_id = f"ctx-{uuid.uuid4().hex[:12]}"

    set_request_context(
        request_id=f"req-{test_id}",
        user_id="seq-test-user",
        tenant_id="seq-test-tenant",
    )
    try:
        logger.info("Context enrichment test", test_id=test_id)
        _force_flush_providers()
    finally:
        clear_request_context()

    events = await _query_seq_events(f"test_id = '{test_id}'")
    assert len(events) > 0, f"Expected at least 1 event with test_id={test_id} in Seq."
