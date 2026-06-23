# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector-ingest MCP tools (G3.5-T2 / #1531).

Covers ``meho.connector.ingest`` + ``meho.connector.ingest_status``,
the ingest-pipeline tools split out of ``connector_admin`` into
:mod:`meho_backplane.mcp.tools.connector_ingest`. The review / edit /
state-machine tools are tested in ``test_mcp_tools_connector_admin.py``.

Coverage matrix:

* ``tools/list`` visibility per role:
  - ``read_only`` sees neither ingest tool.
  - ``operator`` sees ``ingest_status`` (read) but NOT ``ingest`` (write).
  - ``tenant_admin`` sees both.
* Both tools' ``inputSchema`` is strict JSON-Schema 2020-12 with
  ``additionalProperties: false``; descriptions name when (not) to use.
* **Inline path** (``dry_run=true`` / ``async`` unset) returns the
  canonical ``IngestResponse`` synchronously — the pre-#1531 behaviour
  (no regression). Specs + flags + tenant_id thread through to the
  service.
* **Inline error envelopes**: every typed ``SpecError`` sibling
  (``VersionMismatchError`` / ``UncoveredVersionLabel`` /
  ``UpstreamNotSpecError`` / ``UnsupportedSpecError`` /
  ``InvalidSpecError`` / ``InvalidSchemaError`` / ``OpIdCollision`` /
  ``LlmOutputInvalid``) maps to JSON-RPC ``-32602`` with structured
  ``error.data`` — the #777 envelope pattern, completed for the sibling
  set in #1534 (the last six previously degraded to a bare ``-32603``
  with the message discarded).
* **Async path** (``async=true``, ``dry_run=false``) returns a job
  handle *before* the pipeline finishes (AC #1: prompt return, no block
  on the grouping pass), and ``ingest_status`` polls the job through to
  a terminal ``succeeded`` carrying the final counts (AC #2). A failure
  surfaces via ``error`` / ``error_class`` on the poll response.
* ``ingest_status`` tenant-isolation: a cross-tenant / unknown / built-in
  handle returns ``ingest_job_not_found`` (``-32602``).

The async / dispatch behaviour is unit-tested by calling the handler
coroutines directly (``asyncio_mode = "auto"`` runs them on the test's
event loop) with a controllable pipeline stub — the sync ``TestClient``
portal tears the background task down on response return, which would
defeat the "returns before the pipeline completes" contract these
tests pin. The visibility / schema checks drive the real MCP transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools.connector_ingest as ci_mod
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.ingest import (
    GroupingResult,
    IngestionPipelineResult,
    IngestionResult,
    InvalidSchemaError,
    InvalidSpecError,
    LlmOutputInvalid,
    OpIdCollision,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    UpstreamNotSpecError,
    VersionMismatchError,
    get_job_registry,
    reset_job_registry_for_tests,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_INGEST_TOOL = "meho.connector.ingest"
_INGEST_STATUS_TOOL = "meho.connector.ingest_status"


@pytest.fixture(autouse=True)
def _isolated_job_registry() -> Iterator[None]:
    """Reset the process-local ingest job registry around each test.

    The handler drives the real :class:`IngestJobRegistry` singleton via
    :func:`get_job_registry`; resetting it before and after keeps async
    job rows from one test from leaking into the next.
    """
    reset_job_registry_for_tests()
    yield
    reset_job_registry_for_tests()


def _canned_result(
    *,
    dry_run: bool,
    connector_id: str = "vmware-rest-9.0",
) -> IngestionPipelineResult:
    """Build a representative pipeline result (grouping omitted on dry-run)."""
    ingestion = IngestionResult(
        inserted_count=10,
        updated_count=0,
        skipped_count=0,
        connector_registered=True,
        operations_grouped=not dry_run,
    )
    grouping = (
        None
        if dry_run
        else GroupingResult(
            connector_id=connector_id,
            groups_created=3,
            operations_assigned=10,
            operations_unassigned=0,
            llm_call_count=2,
            llm_duration_ms=123.4,
        )
    )
    return IngestionPipelineResult(
        connector_id=connector_id,
        ingestion=ingestion,
        grouping=grouping,
    )


class _FakeIngestionPipelineService:
    """Records every ingest call + returns a canned result.

    Reused as a factory: the production handler calls
    ``IngestionPipelineService(operator, llm_client_factory=...)`` which
    resolves to ``__call__`` here, returning the same instance so a
    single fixture can capture the call.
    """

    def __init__(self) -> None:
        self.init_kwargs: dict[str, Any] = {}
        self.ingest_calls: list[dict[str, Any]] = []

    def __call__(self, operator: Operator, **kwargs: Any) -> _FakeIngestionPipelineService:
        self.operator = operator
        self.init_kwargs = kwargs
        return self

    async def ingest(self, **kwargs: Any) -> IngestionPipelineResult:
        self.ingest_calls.append(kwargs)
        return _canned_result(dry_run=bool(kwargs.get("dry_run")))


class _BlockingIngestionPipelineService:
    """A pipeline stub whose ``ingest`` blocks until a gate is released.

    Used by the async-path test to prove the tool returns a handle
    *before* the pipeline finishes: the handler fires the pipeline off
    the request via ``asyncio.create_task`` and returns the handle while
    ``ingest`` is still parked on :attr:`gate`. Releasing the gate lets
    the background task complete, after which the poll tool reports
    ``succeeded``.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.ingest_calls: list[dict[str, Any]] = []

    def __call__(self, operator: Operator, **_kwargs: Any) -> _BlockingIngestionPipelineService:
        return self

    async def ingest(self, **kwargs: Any) -> IngestionPipelineResult:
        self.ingest_calls.append(kwargs)
        self.started.set()
        await self.gate.wait()
        return _canned_result(dry_run=False)


class _RaisingIngestionPipelineService:
    """A pipeline stub whose ``ingest`` raises a pinned exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, operator: Operator, **_kwargs: Any) -> _RaisingIngestionPipelineService:
        return self

    async def ingest(self, **_kwargs: Any) -> IngestionPipelineResult:
        raise self._exc


# ---------------------------------------------------------------------------
# tools/list visibility + schema strictness (real MCP transport)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_ingest_tools_hidden_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A read_only operator sees neither ingest tool."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _INGEST_TOOL not in names
    assert _INGEST_STATUS_TOOL not in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_ingest_status_visible_to_operator_but_ingest_is_not(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``ingest_status`` is operator-read; ``ingest`` stays tenant_admin-write."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _INGEST_STATUS_TOOL in names
    assert _INGEST_TOOL not in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_both_ingest_tools_visible_to_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A tenant_admin sees both the ingest tool and its poll companion."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _INGEST_TOOL in names
    assert _INGEST_STATUS_TOOL in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_ingest_tool_schemas_are_strict_and_describe_async(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Both schemas are strict 2020-12; ingest advertises async + the poll pairing."""
    import jsonschema

    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    for name in (_INGEST_TOOL, _INGEST_STATUS_TOOL):
        schema = tools[name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, f"{name}: not strict"
        jsonschema.Draft202012Validator.check_schema(schema)
        assert "required_role" not in tools[name]
        assert "op_class" not in tools[name]
        desc = tools[name]["description"].lower()
        assert "use" in desc
        assert "do not" in desc or "don't" in desc or "pair" in desc or "after" in desc

    # The ingest schema carries the async flag and points at the poll tool.
    ingest_schema = tools[_INGEST_TOOL]["inputSchema"]
    assert ingest_schema["properties"]["async"]["type"] == "boolean"
    assert _INGEST_STATUS_TOOL in tools[_INGEST_TOOL]["description"]
    # The poll tool takes a job_id handle.
    assert "job_id" in tools[_INGEST_STATUS_TOOL]["inputSchema"]["properties"]
    # #1699: the ingest description documents the tenant-scope contract —
    # omitted tenant_id targets the global scope, the REST endpoint never
    # exposes the parameter, and a cross-scope re-ingest shadow-copies
    # (the cross-surface behaviour itself is pinned in
    # tests/test_api_v1_connectors_ingest.py).
    ingest_desc = tools[_INGEST_TOOL]["description"]
    assert "tenant_id" in ingest_desc
    assert "/api/v1/connectors/ingest" in ingest_desc
    assert "shadow copy" in ingest_desc


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_operator_cannot_call_ingest_write_tool(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator guessing the hidden ``ingest`` name is rejected at the dispatcher."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _INGEST_TOOL,
                "arguments": {
                    "product": "vmware",
                    "version": "9.0",
                    "impl_id": "vmware-rest",
                    "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
                },
            },
        },
    )
    body = response.json()
    assert "error" in body, body
    assert body["error"]["code"] == -32602
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Inline path (dry_run / async unset) — no regression
# ---------------------------------------------------------------------------


async def test_inline_dry_run_returns_ingest_response_no_grouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dry_run=true`` runs inline and returns the canonical IngestResponse."""
    fake = _FakeIngestionPipelineService()
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", fake)
    op = build_operator(TenantRole.TENANT_ADMIN)

    payload = await ci_mod._ingest_handler(
        op,
        {
            "product": "vmware",
            "version": "9.0",
            "impl_id": "vmware-rest",
            "specs": [
                {"uri": "docs:vcenter-9.0/vcenter.yaml"},
                {"uri": "docs:vcenter-9.0/vi-json.yaml"},
            ],
            "dry_run": True,
            "async": True,  # ignored on the dry-run path
            "tenant_id": str(OPERATOR_TENANT_ID),
        },
    )

    # Inline (not a handle): nested ingestion + grouping=None.
    assert payload["ingestion"]["connector_id"] == "vmware-rest-9.0"
    assert payload["ingestion"]["inserted_count"] == 10
    assert payload["grouping"] is None
    assert "job_id" not in payload

    [call] = fake.ingest_calls
    assert call["product"] == "vmware"
    assert call["dry_run"] is True
    assert call["tenant_id"] == OPERATOR_TENANT_ID
    assert [s.uri for s in call["specs"]] == [
        "docs:vcenter-9.0/vcenter.yaml",
        "docs:vcenter-9.0/vi-json.yaml",
    ]
    # No background job was created — dry-run never offloads even with
    # ``async=true`` set (the inline payload above already proves the
    # inline shape; this pins that no job row leaked into the registry).
    assert len(get_job_registry()._jobs) == 0


async def test_inline_default_returns_grouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async`` unset (default false) runs inline and returns ingestion + grouping."""
    fake = _FakeIngestionPipelineService()
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", fake)
    op = build_operator(TenantRole.TENANT_ADMIN)

    payload = await ci_mod._ingest_handler(
        op,
        {
            "product": "vmware",
            "version": "9.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
        },
    )
    assert payload["ingestion"]["connector_id"] == "vmware-rest-9.0"
    assert payload["grouping"]["groups_created"] == 3
    assert "job_id" not in payload
    [call] = fake.ingest_calls
    assert call["dry_run"] is False


async def test_inline_version_mismatch_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline VersionMismatchError surfaces as -32602 with structured data."""
    exc = VersionMismatchError(
        kind="spec_label_mismatch",
        requested_version="8.0",
        spec_info_versions=[("docs:vcenter-9.0/vcenter.yaml", "9.0.3")],
    )
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", _RaisingIngestionPipelineService(exc))
    op = build_operator(TenantRole.TENANT_ADMIN)

    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_handler(
            op,
            {
                "product": "vmware",
                "version": "8.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
            },
        )
    assert "9.0.3" in str(caught.value)
    data = caught.value.data
    assert data is not None
    assert data["kind"] == "spec_label_mismatch"
    assert data["requested_version"] == "8.0"


async def test_inline_uncovered_version_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline UncoveredVersionLabel surfaces as -32602 with the registered ranges."""
    exc = UncoveredVersionLabel(
        product="t9-vmware",
        version="7.0",
        impl_id="t9-vmware-rest",
        candidates=[("9.0", "t9-vmware-rest", "_RangedTestConnector", ">=8.5,<10.0")],
    )
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", _RaisingIngestionPipelineService(exc))
    op = build_operator(TenantRole.TENANT_ADMIN)

    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_handler(
            op,
            {
                "product": "t9-vmware",
                "version": "7.0",
                "impl_id": "t9-vmware-rest",
                "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
            },
        )
    data = caught.value.data
    assert data is not None
    assert data["product"] == "t9-vmware"
    assert data["version"] == "7.0"


async def test_inline_divergent_product_with_handrolled_impl_fails_closed() -> None:
    """The MCP ingest tool rejects a divergent product whose impl_id is hand-coded.

    G0.27 / T3 (#1817) — the hole #1851 closes. The
    ``meho.connector.ingest`` tool calls
    :meth:`IngestionPipelineService.ingest` directly, never the REST 422
    guard. Before #1851, ``--product vcf-logs --impl-id vrli-rest`` (where
    ``VcfLogsConnector`` is registered under the canonical ``vrli``)
    persisted rows under ``vcf-logs`` with no exception: the auto-register
    deferral branch returns ``False`` without reaching
    ``register_connector_v2``'s #1816 hard-fail. Moving the round-trip
    enforcement into the service layer makes this tool fail closed —
    surfacing the divergence as a structured JSON-RPC ``-32602`` (an
    actionable agent-facing error) instead of a silent non-dispatchable
    shadow row.

    Drives the **real** ``_ingest_handler`` (no service stub) so the guard
    is exercised on the production path the MCP tool actually takes; the
    guard fires before any spec fetch or DB write, so no DB is needed.
    """
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector

    # Register the hand-coded class only if the eager-import / a prior
    # test in this process hasn't already placed it (the autouse registry
    # isolation snapshots/restores ``_REGISTRY_V2`` contents, so the key
    # may already be present) — re-registering the same triple raises.
    if ("vrli", "9.0", "vrli-rest") not in all_connectors_v2():
        register_connector_v2(
            product="vrli",
            version="9.0",
            impl_id="vrli-rest",
            cls=VcfLogsConnector,
        )
    op = build_operator(TenantRole.TENANT_ADMIN)

    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_handler(
            op,
            {
                "product": "vcf-logs",  # diverges from the parser-derived "vrli"
                "version": "9.0",
                "impl_id": "vrli-rest",
                "specs": [{"uri": "https://example.test/vrli.yaml"}],
                "tenant_id": str(OPERATOR_TENANT_ID),
            },
        )
    data = caught.value.data
    assert data is not None
    assert data["kind"] == "product_impl_id_mismatch"
    assert data["product"] == "vcf-logs"
    assert data["derived_product"] == "vrli"
    # The message names both spellings so the driving agent can self-correct.
    assert "vcf-logs" in str(caught.value)
    assert "vrli" in str(caught.value)


# ---------------------------------------------------------------------------
# Inline error envelopes — SpecError siblings (#1534, completing #777)
#
# Before #1534 these six classes fell through the inline handler to the
# dispatcher's generic ``except Exception`` and surfaced as a bare
# ``-32603 "internal error: <ClassName>"`` with the diagnostic message
# discarded; the REST surface always carried the detail. Each test pins
# that the inline handler now raises ``McpInvalidParamsError`` (the
# ``-32602`` sentinel) carrying the rendered message plus a structured
# ``data`` envelope that names the detected-vs-expected shape + remedy.
# ---------------------------------------------------------------------------


async def _expect_inline_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> McpInvalidParamsError:
    """Drive the inline ingest handler against a raising pipeline stub.

    Returns the caught :class:`McpInvalidParamsError` so each sibling
    test asserts on its own ``str`` / ``data`` shape without repeating
    the handler-invocation boilerplate.
    """
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", _RaisingIngestionPipelineService(exc))
    op = build_operator(TenantRole.TENANT_ADMIN)
    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_handler(
            op,
            {
                "product": "vmware",
                "version": "9.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
            },
        )
    return caught.value


async def test_inline_unsupported_spec_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline UnsupportedSpecError surfaces as -32602 with the remedy message."""
    exc = UnsupportedSpecError(
        "Swagger 2.0 is not supported; convert to OpenAPI 3.x via "
        "swagger2openapi / converter.swagger.io and re-ingest"
    )
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "Swagger 2.0" in str(err)
    assert err.data is not None
    assert err.data["detail"] == "unsupported_spec"
    assert "swagger2openapi" in err.data["message"]


async def test_inline_invalid_spec_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline InvalidSpecError surfaces as -32602, not a bare -32603."""
    exc = InvalidSpecError("document is missing the required 'paths' key")
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "paths" in str(err)
    assert err.data is not None
    assert err.data["detail"] == "invalid_spec"
    assert err.data["message"] == str(exc)


async def test_inline_invalid_schema_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline InvalidSchemaError surfaces as -32602 with the schema fault."""
    exc = InvalidSchemaError("dangling $ref '#/components/schemas/Missing'")
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "$ref" in str(err)
    assert err.data is not None
    assert err.data["detail"] == "invalid_schema"
    assert err.data["message"] == str(exc)


async def test_inline_op_id_collision_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline OpIdCollision surfaces as -32602 with structured op-id detail."""
    exc = OpIdCollision(
        op_ids=["get:/widgets", "post:/widgets"],
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        existing_spec_source="spec:vcenter.yaml",
        incoming_spec_source="spec:vi-json.yaml",
    )
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "get:/widgets" in str(err)
    assert err.data is not None
    assert err.data["detail"] == "op_id_collision"
    # Machine-resolvable fields — the agent names the colliding ops and
    # the two specs fighting over them without re-parsing the message.
    assert err.data["op_ids"] == ["get:/widgets", "post:/widgets"]
    assert err.data["product"] == "vmware"
    assert err.data["existing_spec_source"] == "spec:vcenter.yaml"
    assert err.data["incoming_spec_source"] == "spec:vi-json.yaml"


async def test_inline_upstream_not_spec_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline UpstreamNotSpecError reuses the shared #1211 envelope on -32602."""
    exc = UpstreamNotSpecError(
        upstream_url="https://developer.broadcom.com/xapis/vmware",
        content_type="text/html; charset=utf-8",
    )
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "non-spec content" in str(err)
    assert err.data is not None
    # The explicit-quadruple builder (no catalog_entry) — the MCP path is
    # always explicit-quadruple, so it never echoes a catalog reference.
    assert err.data["detail"] == "upstream_not_spec"
    assert err.data["upstream_url"] == "https://developer.broadcom.com/xapis/vmware"
    assert err.data["content_type"] == "text/html; charset=utf-8"
    assert "catalog_entry" not in err.data


async def test_inline_llm_output_invalid_maps_to_invalid_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline LlmOutputInvalid surfaces as -32602 naming the failing pass."""
    exc = LlmOutputInvalid(
        pass_name="propose_groups",
        raw_output="not json",
        parse_error=ValueError("expecting value: line 1 column 1"),
    )
    err = await _expect_inline_invalid_params(monkeypatch, exc)
    assert "propose_groups" in str(err)
    assert err.data is not None
    assert err.data["detail"] == "llm_output_invalid"
    assert err.data["pass_name"] == "propose_groups"
    # The raw LLM output is debug-log material, not response material —
    # only the (truncated) message preview travels on the wire.
    assert "raw_output" not in err.data


# ---------------------------------------------------------------------------
# Async path — AC #1 (prompt return) + AC #2 (poll to terminal)
# ---------------------------------------------------------------------------


async def test_async_returns_handle_before_pipeline_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #1: ``async=true`` returns a job handle while the pipeline is still running.

    The blocking stub parks ``ingest`` on a gate; the handler must
    return the handle without awaiting completion (the whole point of
    the offload — a real vendor-spec grouping pass blocks past the
    agent's tool-call deadline). The returned handle is poll-able.
    """
    blocking = _BlockingIngestionPipelineService()
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", blocking)
    op = build_operator(TenantRole.TENANT_ADMIN)

    handle = await asyncio.wait_for(
        ci_mod._ingest_handler(
            op,
            {
                "product": "sddc-manager",
                "version": "9.0",
                "impl_id": "sddc-manager-rest",
                "specs": [{"uri": "docs:sddc-9.0/sddc.yaml"}],
                "async": True,
            },
        ),
        timeout=2.0,
    )

    # The handle came back even though ``ingest`` is still parked.
    assert handle["status"] == "running"
    assert "job_id" in handle
    assert handle["poll_url"] == f"/api/v1/connectors/ingest/jobs/{handle['job_id']}"
    # The background task did start the pipeline (it's blocked on the gate).
    await asyncio.wait_for(blocking.started.wait(), timeout=2.0)

    # Polling now shows ``running`` (the pipeline hasn't finished).
    running = await ci_mod._ingest_status_handler(op, {"job_id": handle["job_id"]})
    assert running["status"] == "running"
    assert running["ingestion"] is None
    assert running["product"] == "sddc-manager"

    # Release the gate; the background task completes and the job flips
    # to ``succeeded`` carrying the final counts (AC #2).
    blocking.gate.set()
    terminal = await _poll_until_terminal(op, handle["job_id"])
    assert terminal["status"] == "succeeded"
    assert terminal["ingestion"]["inserted_count"] == 10
    assert terminal["grouping"]["groups_created"] == 3
    assert terminal["error"] is None


async def test_async_failure_surfaces_on_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline failure on the async path lands on the poll response, not inline.

    The async handler has already returned a handle by the time the
    pipeline raises, so the diagnostic surfaces via ``error`` /
    ``error_class`` on ``ingest_status`` (same trade-off the REST async
    path documents) rather than as an inline ``-32602``.
    """
    exc = VersionMismatchError(
        kind="spec_label_mismatch",
        requested_version="8.0",
        spec_info_versions=[("docs:vcenter-9.0/vcenter.yaml", "9.0.3")],
    )
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", _RaisingIngestionPipelineService(exc))
    op = build_operator(TenantRole.TENANT_ADMIN)

    handle = await ci_mod._ingest_handler(
        op,
        {
            "product": "vmware",
            "version": "8.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
            "async": True,
        },
    )
    assert handle["status"] == "running"

    terminal = await _poll_until_terminal(op, handle["job_id"])
    assert terminal["status"] == "failed"
    assert terminal["error_class"] == "VersionMismatchError"
    assert terminal["error"] is not None
    assert terminal["ingestion"] is None


async def test_ingest_status_unknown_handle_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown job id returns ``ingest_job_not_found`` (-32602)."""
    op = build_operator(TenantRole.TENANT_ADMIN)
    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_status_handler(
            op,
            {"job_id": "00000000-0000-0000-0000-0000000000ff"},
        )
    assert "ingest_job_not_found" in str(caught.value)


async def test_ingest_status_malformed_handle_is_invalid_params() -> None:
    """A non-UUID handle is rejected with an invalid-params diagnostic."""
    op = build_operator(TenantRole.TENANT_ADMIN)
    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_status_handler(op, {"job_id": "not-a-uuid"})
    assert "valid job id" in str(caught.value).lower()


async def test_ingest_status_cross_tenant_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator cannot poll a job that belongs to another tenant.

    The async ingest runs under the originating operator's tenant; a
    different-tenant operator polling the same handle sees the same
    ``ingest_job_not_found`` a missing id returns (the cross-tenant 404
    conflation the registry enforces).
    """
    blocking = _BlockingIngestionPipelineService()
    blocking.gate.set()  # let it complete immediately
    monkeypatch.setattr(ci_mod, "IngestionPipelineService", blocking)
    owner = build_operator(TenantRole.TENANT_ADMIN)

    # Scope the job to the owner's tenant by passing ``tenant_id``
    # explicitly — the MCP path defaults to the built-in (None) scope
    # when the arg is omitted, so an explicit tenant id is what makes
    # the job tenant-bound and the cross-tenant probe meaningful.
    handle = await ci_mod._ingest_handler(
        owner,
        {
            "product": "vmware",
            "version": "9.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "docs:vcenter-9.0/vcenter.yaml"}],
            "async": True,
            "tenant_id": str(OPERATOR_TENANT_ID),
        },
    )
    await _poll_until_terminal(owner, handle["job_id"])

    # A second operator in a different tenant probes the same handle.
    from uuid import UUID

    intruder = Operator(
        sub="intruder",
        name="Intruder",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000bb"),
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    with pytest.raises(McpInvalidParamsError) as caught:
        await ci_mod._ingest_status_handler(intruder, {"job_id": handle["job_id"]})
    assert "ingest_job_not_found" in str(caught.value)


async def _poll_until_terminal(
    operator: Operator,
    job_id: str,
    *,
    attempts: int = 50,
) -> dict[str, Any]:
    """Drive ``ingest_status`` until the job leaves the ``running`` state.

    Yields control between polls so the background ``asyncio.create_task``
    pipeline coroutine gets scheduled. Fails the test if the job never
    reaches a terminal state within the attempt budget rather than
    hanging.
    """
    for _ in range(attempts):
        payload = await ci_mod._ingest_status_handler(operator, {"job_id": job_id})
        if payload["status"] != "running":
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached a terminal state")  # pragma: no cover


def test_job_registry_accessor_is_importable() -> None:
    """Sanity: the handler module wires the shared registry accessor.

    Guards against a refactor that re-points the MCP path at a private
    registry instance instead of the process-wide singleton the REST
    poll endpoint reads — the cross-surface poll property (#811-style)
    depends on both surfaces sharing :func:`get_job_registry`.
    """
    assert ci_mod.get_job_registry is get_job_registry
