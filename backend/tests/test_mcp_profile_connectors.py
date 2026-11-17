# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tool profiles with a connector list (#6099).

With ``MCP_PROFILE_CONNECTOR_LISTS`` on, an admin adds profiles in
``MCP_CONNECTOR_PROFILES``. These tests check:

* the setting is read strictly, and a mistake stops the start;
* every tool that takes a ``connector_id`` refuses a connector outside the
  profile (also a raw ``METHOD:/path`` operation and a wrong type), and
  the handler does not run; every version of a listed family is allowed;
* ``meho_connector_list`` shows only the profile's connectors;
* a read-only profile has no MEHO write tools, and the policy gate refuses
  a change; the stricter rule wins;
* a stored result made under one profile is not readable under another
  profile, or without a profile;
* the audit row names the profile in a field of its own;
* with the switch off, or with a built-in profile, nothing changes, for
  every registered tool.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import meho_backplane.operations.result_query as rq
import meho_backplane.operations.result_read_admission as admission
from meho_backplane.auth.client_identity import apply_client_block
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    result_profile_scope,
    result_profile_var,
)
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.profiles import (
    INVESTIGATE_TOOLS,
    MEHO_WRITE_TOOLS,
    OPERATE_TOOLS,
    TOOL_PROFILES,
    UnknownToolProfileError,
    connector_in_families,
    parse_tool_profile,
    profile_admits,
    profile_admits_connector,
    profile_denial,
)
from meho_backplane.mcp.registry import get_tool, registered_tool_names
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import connector_admin
from meho_backplane.mcp_profile_config import parse_connector_profiles
from meho_backplane.operations._validate import policy_gate
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_PROFILES = {
    "k8s": {"tools": "investigate", "connectors": ["kubernetes"]},
    "k8s-ops": {"tools": "operate", "connectors": ["kubernetes"], "read_only": False},
    "k8s-watch": {"tools": "operate", "connectors": ["Kubernetes"]},
}

#: The tools that take a ``connector_id``.
_CONNECTOR_TOOLS = ("call_operation", "preview_operation", "search_operations")


@pytest.fixture
def profiles_on(
    monkeypatch: pytest.MonkeyPatch,
    required_settings_env: None,  # noqa: F811
) -> Iterator[None]:
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true")
    monkeypatch.setenv("MCP_CONNECTOR_PROFILES", json.dumps(_PROFILES))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(profile: str | None = None, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub="op-profile",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        mcp_tool_profile=profile,
    )


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_profiles_are_read_and_cleaned() -> None:
    profiles = parse_connector_profiles(json.dumps(_PROFILES))
    assert profiles["k8s"].read_only is True
    assert profiles["k8s-ops"].read_only is False
    assert profiles["k8s-watch"].connectors == ("kubernetes",)
    assert parse_connector_profiles("") == {}
    assert parse_connector_profiles("  ") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        json.dumps({"investigate": {"tools": "investigate", "connectors": ["x"]}}),
        json.dumps({"Bad Name": {"tools": "investigate", "connectors": ["x"]}}),
        json.dumps({"k": {"tools": "investigate", "connectors": []}}),
        json.dumps({"k": {"tools": "investigate", "connectors": [" "]}}),
        json.dumps({"k": {"tools": "everything", "connectors": ["x"]}}),
        json.dumps({"k": {"tools": "investigate", "connectors": ["x"], "extra": 1}}),
        json.dumps({"k": {"tools": "investigate"}}),
    ],
)
def test_a_bad_setting_stops_the_start(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        parse_connector_profiles(raw)
    monkeypatch.setenv("MCP_CONNECTOR_PROFILES", raw)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()


@pytest.mark.usefixtures("profiles_on")
def test_a_configured_profile_is_known_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert parse_tool_profile(["K8S"]) == "k8s"
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "false")
    get_settings.cache_clear()
    with pytest.raises(UnknownToolProfileError):
        parse_tool_profile(["k8s"])
    assert profile_admits("k8s", "meho_status") is False


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector_id", "expected"),
    [
        ("kubernetes", True),
        ("kubernetes-1.x", True),
        ("kubernetes-1.28", True),
        ("KUBERNETES-1.x", True),
        ("kubernetes-rest-1.30", True),
        ("kubernetesx-1.x", False),
        ("vault-1.x", False),
        ("", False),
    ],
)
def test_every_version_of_a_family_counts(connector_id: str, expected: bool) -> None:
    assert connector_in_families(connector_id, ("kubernetes",)) is expected


# ---------------------------------------------------------------------------
# Tool lists
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("profiles_on")
def test_profile_tools_follow_the_base_list_and_read_only() -> None:
    assert {t for t in OPERATE_TOOLS if profile_admits("k8s-ops", t)} == OPERATE_TOOLS
    assert {t for t in INVESTIGATE_TOOLS if profile_admits("k8s", t)} == INVESTIGATE_TOOLS
    watch = {t for t in OPERATE_TOOLS if profile_admits("k8s-watch", t)}
    assert watch == OPERATE_TOOLS - MEHO_WRITE_TOOLS
    assert "meho_broadcast_announce" not in watch
    for tool in MEHO_WRITE_TOOLS:
        assert profile_admits("k8s", tool) is False


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_a_write_tool_in_a_read_only_profile_is_refused() -> None:
    entry = get_tool("meho_broadcast_announce")
    assert entry is not None
    handler = AsyncMock(return_value={})
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
        pytest.raises(McpInvalidParamsError) as caught,
    ):
        await handle_tools_call(
            _operator("k8s-watch"),
            {"name": "meho_broadcast_announce", "arguments": {"message": "x"}},
        )
    handler.assert_not_awaited()
    assert caught.value.data == {"reason": "not_in_profile", "profile": "k8s-watch"}


# ---------------------------------------------------------------------------
# The connector check at call time
# ---------------------------------------------------------------------------


async def _call(profile: str | None, tool: str, arguments: dict[str, Any]) -> tuple[Any, Any]:
    """Call *tool* with a fake handler; return (handler, audit mock)."""
    entry = get_tool(tool)
    assert entry is not None
    handler = AsyncMock(return_value={"ok": True})
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()) as audit,
    ):
        try:
            await handle_tools_call(_operator(profile), {"name": tool, "arguments": arguments})
        except McpInvalidParamsError as exc:
            return handler, (audit, exc)
    return handler, (audit, None)


def _args(tool: str, connector_id: Any, op_id: str = "kubernetes.pod.list") -> dict[str, Any]:
    if tool == "search_operations":
        return {"connector_id": connector_id, "query": "pods"}
    if tool == "list_operation_groups":
        return {"connector_id": connector_id}
    return {"connector_id": connector_id, "op_id": op_id, "params": {}}


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize("tool", [*_CONNECTOR_TOOLS, "list_operation_groups"])
@pytest.mark.parametrize("connector_id", ["vault-1.x", "kubernetesx-1.x", 42, None])
@pytest.mark.asyncio
async def test_a_connector_outside_the_profile_is_refused(tool: str, connector_id: Any) -> None:
    handler, (audit, exc) = await _call("k8s-ops", tool, _args(tool, connector_id))
    handler.assert_not_awaited()
    assert exc is not None
    assert "is not in your 'k8s-ops' profile" in str(exc)
    assert "kubernetes" in str(exc)
    assert exc.data == {"reason": "connector_not_in_profile", "profile": "k8s-ops"}
    assert audit.await_args.kwargs["status_code"] == 403


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize("tool", [*_CONNECTOR_TOOLS, "list_operation_groups"])
@pytest.mark.parametrize("connector_id", ["kubernetes-1.x", "kubernetes-1.28"])
@pytest.mark.asyncio
async def test_a_connector_of_the_family_runs(tool: str, connector_id: str) -> None:
    handler, (_audit, exc) = await _call("k8s-ops", tool, _args(tool, connector_id))
    assert exc is None
    handler.assert_awaited_once()


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize("op_id", ["GET:/api/v1/secrets", "no.such.operation"])
@pytest.mark.asyncio
async def test_raw_or_unknown_operation_on_a_wrong_connector_is_refused(op_id: str) -> None:
    handler, (_audit, exc) = await _call(
        "k8s-ops", "call_operation", _args("call_operation", "vault-1.x", op_id)
    )
    handler.assert_not_awaited()
    assert exc is not None
    assert exc.data["reason"] == "connector_not_in_profile"


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_the_session_profile_is_bound_while_the_handler_runs() -> None:
    seen: list[str | None] = []

    async def _handler(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(result_profile_var.get())
        return {"ok": True}

    entry = get_tool("meho_status")
    assert entry is not None
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], _handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
    ):
        await handle_tools_call(_operator("k8s"), {"name": "meho_status", "arguments": {}})
    assert seen == ["k8s"]
    assert result_profile_var.get() is None


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_a_built_in_profile_binds_no_result_profile() -> None:
    seen: list[str | None] = []

    async def _handler(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(result_profile_var.get())
        return {"ok": True}

    entry = get_tool("meho_status")
    assert entry is not None
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], _handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
    ):
        await handle_tools_call(_operator("investigate"), {"name": "meho_status", "arguments": {}})
    assert seen == [None]


# ---------------------------------------------------------------------------
# meho_connector_list
# ---------------------------------------------------------------------------


_ROWS = {
    "connectors": [
        {"connector_id": "kubernetes-1.x", "product": "kubernetes"},
        {"connector_id": "vault-1.x", "product": "vault"},
    ]
}


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize(
    ("profile", "expected"),
    [("k8s", ["kubernetes-1.x"]), ("investigate", ["kubernetes-1.x", "vault-1.x"]), (None, None)],
)
@pytest.mark.asyncio
async def test_connector_list_shows_only_the_profile_connectors(
    profile: str | None, expected: list[str] | None
) -> None:
    with patch.object(connector_admin, "list_connector_rows", new=AsyncMock(return_value=_ROWS)):
        result = await connector_admin._list_handler(_operator(profile), {})
    ids = [row["connector_id"] for row in result["connectors"]]
    assert ids == (expected or ["kubernetes-1.x", "vault-1.x"])


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_connector_list_target_fit_says_no_connector_for_the_profile() -> None:
    rows = {
        "connectors": [{"connector_id": "vault-1.x"}],
        "target_fit": {"result": "compatible", "hint": "x"},
    }
    with patch.object(connector_admin, "list_connector_rows", new=AsyncMock(return_value=rows)):
        result = await connector_admin._list_handler(_operator("k8s"), {"target": "t"})
    assert result["connectors"] == []
    assert result["target_fit"]["result"] == "no_connector"
    assert "profile" in result["target_fit"]["hint"]


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_connector_list_product_filter_names_only_profile_products() -> None:
    """A product filter with no match never names a product outside the profile."""
    rows = {
        "connectors": [],
        "product_filter": {
            "query": "nope",
            "matches": [],
            "known_products": ["kubernetes", "vault"],
        },
        "_listed_products": {"kubernetes-1.x": "kubernetes", "vault-1.x": "vault"},
    }
    with patch.object(connector_admin, "list_connector_rows", new=AsyncMock(return_value=rows)):
        result = await connector_admin._list_handler(_operator("k8s"), {"product": "nope"})
    assert result["product_filter"]["known_products"] == ["kubernetes"]


# ---------------------------------------------------------------------------
# The policy gate: the stricter of the person's and the profile's rules
# ---------------------------------------------------------------------------


def _descriptor(method: str | None, safety_level: str = "safe") -> EndpointDescriptor:
    return EndpointDescriptor(
        product="kubernetes",
        version="1.x",
        impl_id="kubernetes",
        op_id="kubernetes.pod.delete" if method != "GET" else "kubernetes.pod.list",
        method=method,
        path="/x" if method else None,
        safety_level=safety_level,
        requires_approval=False,
        source_kind="ingested",
    )


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize(
    ("profile", "connector_id", "method", "safety", "refused"),
    [
        ("k8s", "kubernetes-1.x", "GET", "safe", False),
        ("k8s", "kubernetes-1.x", "DELETE", "dangerous", True),
        ("k8s", "kubernetes-1.x", None, "caution", True),
        ("k8s-ops", "kubernetes-1.x", "DELETE", "dangerous", False),
        ("k8s-ops", "vault-1.x", "GET", "safe", True),
        ("k8s-ops", None, "GET", "safe", True),
        ("investigate", "vault-1.x", "DELETE", "dangerous", False),
        (None, "vault-1.x", "DELETE", "dangerous", False),
    ],
)
def test_profile_denial(
    profile: str | None, connector_id: str | None, method: str | None, safety: str, refused: bool
) -> None:
    mutating = method not in {"GET", "HEAD"} if method else safety != "safe"
    reason = profile_denial(profile, connector_id, mutating=mutating)
    assert (reason is not None) is refused


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.parametrize(
    ("profile", "connector_id", "method"),
    [("k8s", "kubernetes-1.x", "DELETE"), ("k8s-ops", "vault-1.x", "GET")],
)
@pytest.mark.asyncio
async def test_policy_gate_denies_before_the_person_rules(
    profile: str, connector_id: str, method: str
) -> None:
    """The profile's deny wins, also for an admin, and no grant is read."""
    with patch(
        "meho_backplane.operations._validate._verdict_with_shadow", new=AsyncMock()
    ) as person_rules:
        verdict, reason = await policy_gate(
            operator=_operator(profile, TenantRole.TENANT_ADMIN),
            descriptor=_descriptor(method, "dangerous" if method != "GET" else "safe"),
            target=None,
            connector_id=connector_id,
        )
    assert verdict is PermissionVerdict.DENY
    assert reason is not None and profile in reason
    person_rules.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stored results
# ---------------------------------------------------------------------------


class _Valkey:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


async def _spill_under(store: ResultHandleStore, profile: str | None) -> uuid.UUID:
    handle_id = uuid.uuid4()
    token = result_profile_var.set(profile)
    try:
        stored = await store.spill(
            tenant_id=OPERATOR_TENANT_ID,
            operator_sub="op-profile",
            handle_id=handle_id,
            op_id="kubernetes.pod.list",
            rows=[{"name": f"pod-{i}", "ns": "default"} for i in range(5)],
            total_rows=5,
            ttl_seconds=60,
            max_rows=100,
            document={"kind": "PodList"},
        )
    finally:
        result_profile_var.reset(token)
    assert stored.stored_rows == 5
    return handle_id


@pytest.fixture
def result_store(monkeypatch: pytest.MonkeyPatch, profiles_on: None) -> Iterator[ResultHandleStore]:
    admission.shutdown_result_read_admission()
    store = ResultHandleStore(_Valkey(), dedicated_client=_Valkey())  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    yield store
    admission.shutdown_result_read_admission()


async def _read(operator: Operator, handle_id: uuid.UUID, kind: str) -> Any:
    if kind == "paging":
        return await rq.read_result_window(operator, handle_id, 0, 10)
    if kind == "query":
        return await rq.run_result_query(operator, handle_id, rq.ResultQuerySpec())
    return await rq.run_result_query(operator, handle_id, rq.ResultQuerySpec(path="/kind"))


@pytest.mark.parametrize("kind", ["paging", "query", "path"])
@pytest.mark.parametrize(
    ("written", "read"),
    [("k8s", "k8s-ops"), ("k8s", None), (None, "k8s"), ("k8s", "investigate")],
)
@pytest.mark.asyncio
async def test_a_result_is_not_readable_under_another_profile(
    result_store: ResultHandleStore, kind: str, written: str | None, read: str | None
) -> None:
    handle_id = await _spill_under(result_store, written)
    with pytest.raises(rq.ResultHandleNotFoundError):
        await _read(_operator(read), handle_id, kind)


@pytest.mark.parametrize("kind", ["paging", "query", "path"])
@pytest.mark.parametrize("profile", ["k8s", None])
@pytest.mark.asyncio
async def test_a_result_is_readable_under_the_same_profile(
    result_store: ResultHandleStore, kind: str, profile: str | None
) -> None:
    handle_id = await _spill_under(result_store, profile)
    assert await _read(_operator(profile), handle_id, kind)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["k8s", "investigate"])
def test_audit_payload_names_the_profile(profile: str) -> None:
    payload: dict[str, Any] = {}
    apply_client_block(payload, _operator(profile))
    assert payload["mcp_profile"] == profile


def test_audit_payload_without_a_profile_is_unchanged() -> None:
    payload: dict[str, Any] = {"op_id": "x"}
    apply_client_block(payload, _operator(None))
    assert payload == {"op_id": "x"}


@pytest.mark.usefixtures("profiles_on")
@pytest.mark.asyncio
async def test_the_tool_call_row_names_the_profile_and_not_the_actor() -> None:
    """The refused call's row: the profile in ``mcp_profile``, never ``actor_sub``."""
    captured: dict[str, Any] = {}

    async def _fake_write(**kwargs: Any) -> uuid.UUID:
        payload = dict(kwargs["payload"])
        apply_client_block(payload, kwargs["operator"])
        captured.update(payload)
        return uuid.uuid4()

    entry = get_tool("call_operation")
    assert entry is not None
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], AsyncMock())),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=_fake_write),
        patch(
            "meho_backplane.mcp.handlers.wrapper_row_class",
            return_value="legacy_request",
        ),
        pytest.raises(McpInvalidParamsError),
    ):
        await handle_tools_call(
            _operator("k8s"),
            {"name": "call_operation", "arguments": _args("call_operation", "vault-1.x")},
        )
    assert captured["mcp_profile"] == "k8s"
    assert "actor_sub" not in captured


# ---------------------------------------------------------------------------
# Parity: switch off, or a built-in profile, changes nothing
# ---------------------------------------------------------------------------


def _gates(profile: str | None, tools: list[str]) -> list[tuple[Any, ...]]:
    return [
        (
            tool,
            profile_admits(profile, tool),
            profile_admits_connector(profile, "vault-1.x"),
            profile_denial(profile, "vault-1.x", mutating=True),
        )
        for tool in tools
    ]


@pytest.mark.parametrize("switch", [False, True])
def test_only_a_profile_with_a_connector_list_keeps_results_apart(
    switch: bool,
    monkeypatch: pytest.MonkeyPatch,
    required_settings_env: None,  # noqa: F811
) -> None:
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true" if switch else "false")
    monkeypatch.setenv("MCP_CONNECTOR_PROFILES", json.dumps(_PROFILES))
    get_settings.cache_clear()
    assert result_profile_scope(None) is None
    for name in TOOL_PROFILES:
        assert result_profile_scope(name) is None
    assert result_profile_scope("k8s") == ("k8s" if switch else None)
    get_settings.cache_clear()


@pytest.mark.parametrize("kind", ["paging", "query", "path"])
@pytest.mark.parametrize("switch", [False, True])
@pytest.mark.parametrize("profile", [None, *sorted(TOOL_PROFILES)])
@pytest.mark.asyncio
async def test_results_without_a_connector_list_read_as_before(
    result_store: ResultHandleStore,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    switch: bool,
    profile: str | None,
) -> None:
    """A result made before the upgrade, or through REST, has no profile.

    A session with no profile or a built-in profile reads it, with the
    switch off and on. A result such a session makes is read back the
    same way.
    """
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true" if switch else "false")
    get_settings.cache_clear()
    old_record = await _spill_under(result_store, None)
    assert await _read(_operator(profile), old_record, kind)
    own_record = await _spill_under(result_store, result_profile_scope(profile))
    assert await _read(_operator(None), own_record, kind)
    get_settings.cache_clear()


@pytest.mark.parametrize("profile", [None, *sorted(TOOL_PROFILES)])
@pytest.mark.asyncio
async def test_switch_changes_nothing_without_a_connector_list(
    profile: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every registered tool: same list, same gates, with the switch off and on."""
    tools = sorted(registered_tool_names())
    answers: dict[bool, Any] = {}
    for switch in (False, True):
        monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true" if switch else "false")
        monkeypatch.setenv("MCP_CONNECTOR_PROFILES", json.dumps(_PROFILES))
        get_settings.cache_clear()
        listed = await handle_tools_list(_operator(profile, TenantRole.TENANT_ADMIN), None)
        answers[switch] = (listed, _gates(profile, tools))
    assert answers[False] == answers[True]
    for _tool, _admits, connector_ok, denial in answers[True][1]:
        assert connector_ok is True
        assert denial is None
    get_settings.cache_clear()
