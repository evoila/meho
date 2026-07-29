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

import json
import logging
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.preamble import (
    BLOCK_END,
    BLOCK_START,
    BROADCAST_BLOCK_START,
    BROADCAST_DISCIPLINE_BAND,
    GUARD_PREFIX,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant, TenantConvention
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
    """Empty tenant -> ``instructions`` carries the broadcast-discipline band.

    Since G6.5-T6 (#2546) the static broadcast-discipline band is
    injected into every assembled preamble, so even a tenant with no
    operational conventions receives it. The ``instructions`` field is
    therefore present and equals the band alone (no conventions block).
    """
    client, _op = client_with_operator

    response = post_mcp(client, _initialize_envelope())
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    instructions = body["result"]["instructions"]
    assert instructions == BROADCAST_DISCIPLINE_BAND
    assert BLOCK_START not in instructions  # no conventions block


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
    # Since G6.5-T6 (#2546) the always-on broadcast-discipline band
    # leads the preamble; the conventions block follows and (with no
    # priming / catalogue) closes the text.
    assert instructions.startswith(BROADCAST_BLOCK_START)
    assert BLOCK_START in instructions
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


# ---------------------------------------------------------------------------
# G0.13-T7 #1137 — signal-12 verification: default-seeded instructions carry
# no consumer-specific tokens
# ---------------------------------------------------------------------------

#: Tokens that MUST NOT appear in the ``initialize.instructions`` text
#: when the operator's tenant is the seeded ``default`` tenant. These
#: identify content sourced from one specific consumer's ``CLAUDE.md``
#: (the rdc-internal seed migration ``0018`` shipped). The data-layer
#: scan lives in
#: :mod:`tests.test_alembic_seed_0028_supersede`; this scan exercises
#: the same contract end-to-end through the MCP wire surface so a
#: regression in either the seed or the preamble assembler surfaces
#: here. The bare ``rdc-`` prefix subsumes ``rdc-internal`` and
#: ``rdc-hetzner``; ``meho-internal`` and ``claude-rdc-`` are added per
#: the #1137 acceptance criterion to cover the full set of internal-repo
#: and consumer-identity tokens.
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "evoila/meho",
    "evoila-bosnia",
    "meho-internal",
    "claude-rdc-",
    "rdc-",
    "Holodeck",
)


@pytest.mark.asyncio
async def test_initialize_against_default_tenant_carries_no_consumer_tokens(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """MCP ``initialize`` against the seeded ``default`` tenant -> no consumer-specific tokens.

    Acceptance criterion (#1137 AC, signal-12 verification): "After
    migration lands, ``initialize`` against a fresh-DB MCP server
    returns ``instructions`` text that carries the generic illustrative
    conventions only -- zero references to ``evoila/meho``,
    ``evoila-bosnia/meho-internal``, ``rdc-internal``, Holodeck-claude,
    or any other consumer-specific tokens. Test exercises a fresh MCP
    initialize round-trip and asserts the absence of those tokens."

    The migration chain (run by the conftest schema-template builder)
    seeds the ``default`` tenant + 2 illustrative conventions. This
    test rebinds the fixture operator's tenant to the seeded
    ``default`` row's id so the ``_initialize`` handler's
    ``assemble_preamble(operator.tenant_id, operator.sub)`` resolves
    the seeded conventions (per G12.4-T2 #1316, the signature now
    requires the operator's sub for runbook priming); it then issues
    a real ``initialize`` JSON-RPC call
    through the FastAPI TestClient and scans the returned
    ``instructions`` text for the forbidden token list.
    """
    client, op = client_with_operator

    # Resolve the seeded default tenant id (the schema-template
    # builder ran ``alembic upgrade head``, so migration 0028 has
    # seeded the ``default`` row).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        default_tenant_id = await session.scalar(
            select(Tenant.id).where(Tenant.slug == "default"),
        )
    assert default_tenant_id is not None, (
        "migration 0028 must seed the default tenant for the signal-12 "
        "verification to be meaningful"
    )

    # Rebind the test client's operator to the default tenant id
    # so the MCP initialize handler's preamble assembler resolves
    # the seeded conventions.
    from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind

    rebound_operator = Operator(
        sub=op.sub,
        name=op.name,
        email=op.email,
        raw_jwt=op.raw_jwt,
        tenant_id=default_tenant_id,
        tenant_role=TenantRole.READ_ONLY,
    )

    def _fake_verify_default() -> Operator:
        return rebound_operator

    client.app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify_default
    try:
        response = post_mcp(client, _initialize_envelope())
    finally:
        # Restore the original override -- the fixture's teardown
        # will pop it from the dict on exit, but a mid-test rebind
        # should leave behaviour as the fixture set it.
        client.app.dependency_overrides[verify_mcp_jwt_and_bind] = (
            lambda: op  # type: ignore[no-any-return]
        )

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    instructions = body["result"].get("instructions") or ""
    assert isinstance(instructions, str)

    # Either: instructions is empty (no seeded conventions reached
    # the operator's tenant_id -- unexpected here because we just
    # bound to the seeded default tenant), OR instructions is
    # non-empty AND carries no forbidden tokens.
    assert instructions, (
        "default tenant should carry seeded illustrative conventions in instructions; got empty"
    )
    haystack = instructions.lower()
    for token in _FORBIDDEN_TOKENS:
        assert token.lower() not in haystack, (
            f"forbidden token {token!r} found in MCP initialize.instructions for the "
            "default tenant -- the seed must contain no references to a specific "
            "consumer's operational discipline or repo identifiers"
        )


# ---------------------------------------------------------------------------
# G0.14-T13 #1202 — initialize protocolVersion mismatch observability
# ---------------------------------------------------------------------------


def _initialize_envelope_with_version(version: str) -> dict[str, object]:
    """Build a valid ``initialize`` request pinning a specific protocol version.

    Mirrors :func:`_initialize_envelope` but lets the caller force the
    client-side ``protocolVersion`` to an older (or arbitrary) revision
    so the mismatch path is exercised end-to-end through the MCP wire
    surface.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test-harness", "version": "0.0.0"},
        },
    }


def _parse_structlog_events(
    captured_stdout: str,
    *,
    event_name: str,
) -> list[dict[str, object]]:
    """Filter structlog JSON-lines stdout for events matching ``event_name``.

    The chassis configures structlog with :class:`PrintLoggerFactory`
    rendering each event as a single JSON object on stdout. The test
    surface for those events is ``capfd``'s OS-fd-level stdout
    capture; this helper turns that raw text into a list of decoded
    event dicts so the assertions can read attributes directly.
    Lines that aren't valid JSON (rare interleaved warnings from
    non-structlog sources) are silently skipped — they cannot match
    by event name anyway, and including them would only widen the
    parser's failure modes.
    """
    matches: list[dict[str, object]] = []
    for line in captured_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == event_name:
            matches.append(event)
    return matches


@pytest.mark.asyncio
async def test_initialize_logs_warning_on_protocol_version_mismatch(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Older client ``protocolVersion`` emits the mismatch WARNING + unchanged response.

    Acceptance criterion (G0.14-T13 #1202): an MCP ``initialize`` call
    with ``protocolVersion: "2025-03-26"`` (any non-``PROTOCOL_VERSION``
    value) emits a single ``mcp_initialize_protocol_version_mismatch``
    WARNING log line carrying ``client_protocol_version`` +
    ``server_protocol_version``, and the response still returns
    HTTP 200 with ``InitializeResponse.protocolVersion == PROTOCOL_VERSION``
    (behaviour unchanged).

    Capture surface: the chassis configures structlog with
    :class:`PrintLoggerFactory`, so structured events are written to
    stdout at the OS-fd level rather than through Python's ``logging``
    module. ``capfd`` is the matching capture surface — same pattern
    the existing ``test_initialize_logs_warning_on_dropped_slugs``
    test uses on this file for the ``mcp_preamble_over_budget``
    event. ``capture_logs()`` from ``structlog.testing`` only
    intercepts when the project's logger factory is the testing
    factory, which MEHO doesn't install (PrintLogger keeps
    production parity for the test suite's wire-format assertions).
    """
    client, op = client_with_operator
    older_revision = "2025-03-26"
    assert older_revision != PROTOCOL_VERSION, (
        "test premise broken: pinned client revision matches server"
    )

    response = post_mcp(
        client,
        _initialize_envelope_with_version(older_revision),
    )

    # Behaviour unchanged: HTTP 200, response echoes the server version.
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSION

    # Observability: parse PrintLogger's JSON-lines stdout for the
    # mismatch event. Each structlog event is a single JSON document
    # per line — scan for the expected event name + assert payload
    # shape.
    captured = capfd.readouterr().out
    mismatch_events = _parse_structlog_events(
        captured,
        event_name="mcp_initialize_protocol_version_mismatch",
    )
    assert len(mismatch_events) == 1, (
        "expected exactly one mcp_initialize_protocol_version_mismatch event; "
        f"got captured stdout={captured!r}"
    )
    event = mismatch_events[0]
    assert event["level"] == "warning"
    assert event["client_protocol_version"] == older_revision
    assert event["server_protocol_version"] == PROTOCOL_VERSION
    assert event["operator_sub"] == op.sub


@pytest.mark.asyncio
async def test_initialize_does_not_log_mismatch_when_versions_match(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Matching client ``protocolVersion`` → no mismatch warning.

    Negative complement of the mismatch test: the WARNING fires only
    on mismatch, so clients sending the server's pinned version (the
    typical case) leave the log stream clean. Important for log-volume
    discipline: a noisy "every initialize logs a warning" surface
    would train operators to ignore the event.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        _initialize_envelope_with_version(PROTOCOL_VERSION),
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSION

    captured = capfd.readouterr().out
    mismatch_events = _parse_structlog_events(
        captured,
        event_name="mcp_initialize_protocol_version_mismatch",
    )
    assert mismatch_events == [], (
        "matching client protocolVersion must not emit the mismatch warning; "
        f"got events={mismatch_events!r}"
    )
