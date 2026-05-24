# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for ``initialize.instructions`` per G7.1-T4 (#316).

Acceptance criteria from the issue body:

* **Seeded tenant returns non-empty ``instructions``** -- a tenant
  with one or more ``kind='operational'`` conventions sees the
  assembled preamble in the ``instructions`` field of the
  ``initialize`` response.
* **Empty tenant returns ``instructions: None``** -- the wire
  serializer omits the field entirely (consistent with every
  other ``str | None`` optional MCP field).
* **Over-budget drop logs a WARNING** -- the dropped slugs are
  surfaced loudly per the issue body's safety contract.

The tests use the shared ``mcp_test_fixtures`` constellation (fixture
operator pinned to :data:`OPERATOR_TENANT_ID`, the chassis env vars,
the registry reload). The fixture seeds a known tenant_id in the
``tenant`` table; the tests directly insert the ``TenantConvention``
rows whose preamble we expect to surface on ``initialize``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator
from meho_backplane.conventions.preamble import BLOCK_END, BLOCK_START, GUARD_PREFIX
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantConvention
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


def _initialize_envelope() -> dict[str, object]:
    """Build a valid ``initialize`` JSON-RPC request envelope.

    Centralised so each test reads the assertion shape without
    re-spelling the request body inline -- the test focus is the
    *response* not the request.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-harness", "version": "0.0.0"},
        },
    }


@pytest.mark.asyncio
async def test_initialize_instructions_omitted_for_empty_tenant(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """Empty tenant -> the ``instructions`` field is absent from the response.

    Acceptance criterion: empty tenant -> ``PreambleResult("", [])``;
    the handler maps the empty string to ``None`` and the wire
    serializer drops ``None`` optional fields (per the
    :func:`~meho_backplane.mcp.server._build_success_response`
    ``exclude_none=True`` policy).
    """
    client, _op = client_with_operator

    response = post_mcp(client, _initialize_envelope())
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    # The wire shape omits ``instructions`` entirely when the
    # handler returns ``None`` for it (mirrors the v0.5-T1 behaviour
    # the existing test_mcp_initialize tests assert).
    assert "instructions" not in body["result"]


@pytest.mark.asyncio
async def test_initialize_instructions_populated_for_seeded_tenant(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """Seeded tenant -> ``instructions`` carries the assembled preamble.

    Acceptance criterion: "MCP ``initialize`` integration tested:
    client connects, receives non-empty ``instructions`` field for a
    seeded tenant." The test asserts the assembled text contains
    the convention's body, the guard prefix, and the delimiter
    envelope -- the load-bearing shape contract.
    """
    client, op = client_with_operator
    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=op.tenant_id,
                slug="rbac-canonical",
                title="RBAC is canonical",
                body="Every operation runs through MEHO's RBAC layer.",
                kind="operational",
                priority=100,
                created_by_sub="test:user",
                created_at=now,
                updated_at=now,
            ),
        )
        await session.commit()

    response = post_mcp(client, _initialize_envelope())
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    instructions = body["result"]["instructions"]
    assert isinstance(instructions, str)
    assert instructions  # non-empty
    # Block delimiters + guard prefix present per the issue body.
    assert instructions.startswith(BLOCK_START)
    assert instructions.endswith(BLOCK_END)
    assert GUARD_PREFIX in instructions
    # Convention content is included verbatim.
    assert "RBAC is canonical" in instructions
    assert "Every operation runs through MEHO's RBAC layer." in instructions


@pytest.mark.asyncio
async def test_initialize_logs_warning_on_dropped_slugs(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Over-budget drop emits a WARNING naming the dropped slugs.

    Acceptance criterion: "``_initialize`` logs a warning naming
    ``dropped_slugs`` when the preamble is over budget (loud, not
    silent)." The test seeds two conventions sized so only the
    higher-priority one fits the default budget; the lower-priority
    slug must surface in a structlog WARNING.

    The default budget is :data:`DEFAULT_MAX_PREAMBLE_TOKENS = 600`;
    we use bodies large enough that exactly one fits in the default.
    """
    client, op = client_with_operator
    # Each body is ~1500 chars (~455 tokens at 3.3 chars/token).
    # The default 600-token budget fits ONE block (~455 tokens) +
    # the header (~25 tokens); the second block would push past
    # 910 tokens, so it drops.
    big_body = "x" * 1500
    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add_all(
            [
                TenantConvention(
                    id=uuid.uuid4(),
                    tenant_id=op.tenant_id,
                    slug="high-priority-rule",
                    title="High priority",
                    body=big_body,
                    kind="operational",
                    priority=100,
                    created_by_sub="test:user",
                    created_at=now,
                    updated_at=now,
                ),
                TenantConvention(
                    id=uuid.uuid4(),
                    tenant_id=op.tenant_id,
                    slug="low-priority-rule",
                    title="Low priority",
                    body=big_body,
                    kind="operational",
                    priority=1,
                    created_by_sub="test:user",
                    created_at=now,
                    updated_at=now,
                ),
            ],
        )
        await session.commit()

    # capfd captures the OS-fd-level stdout that structlog's
    # :class:`PrintLogger` writes to (the conftest secret-leak
    # sweep uses the same capfd surface for the same reason: the
    # chassis configures structlog with PrintLoggerFactory in
    # tests). The handler emits the warning under the event name
    # ``mcp_preamble_over_budget`` with the ``dropped_slugs``
    # field populated.
    response = post_mcp(client, _initialize_envelope())
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body

    # Scan caplog warning records AND captured stdout for the
    # event name + dropped slug. The conftest's xdist-aware secret-
    # leak sweep already proves capfd is the correct surface for
    # structlog output in this project.
    warning_text = "\n".join(
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    )
    captured_stdout = capfd.readouterr().out
    haystack = warning_text + "\n" + captured_stdout
    assert "low-priority-rule" in haystack and "mcp_preamble_over_budget" in haystack, (
        f"expected over-budget warning naming dropped slug; got caplog={warning_text!r}, "
        f"stdout={captured_stdout!r}"
    )
