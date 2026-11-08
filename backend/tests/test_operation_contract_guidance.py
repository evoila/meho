# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The written guidance block of the exact-op contract (#4757).

* :func:`bounded_guidance` keeps only the prose keys of
  ``llm_instructions``, cuts each text and the whole block to fixed
  limits, and returns ``None`` when nothing is left.
* ``search_operations`` with ``op_id`` carries the block only while the
  ``OPERATION_CONTRACT_GUIDANCE`` switch is on. With the switch off, or
  for an op without guidance, the answer has no ``guidance`` key.
* Ranked hits never carry it.
* The exact-op answer with the block matches the tool's ``outputSchema``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import jsonschema
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._contract_guidance import (
    CUT_MARK,
    GUIDANCE_KEYS,
    GUIDANCE_MAX_CHARS,
    GUIDANCE_VALUE_MAX_CHARS,
    bounded_guidance,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import with_data_notice

_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000047a7")

_GUIDANCE = {
    "when_to_call": "Use this to read one widget by its name.",
    "preconditions": "The widget must exist.",
    "parameter_hints": {"name": "The widget name, not its id."},
    "output_shape": "One object with `name` and `size`.",
    "next_step": "Call widgets.update to change it.",
    "pagination_hint": {"style": "cursor", "param": "after"},
    "result_scalars": ["size"],
}


def _text_size(guidance: dict[str, Any]) -> int:
    """Count the characters of every text in a block, parameter names included."""
    total = 0
    for value in guidance.values():
        if isinstance(value, dict):
            total += sum(len(name) + len(hint) for name, hint in value.items())
        else:
            total += len(value)
    return total


# ---------------------------------------------------------------------------
# bounded_guidance
# ---------------------------------------------------------------------------


def test_size_limits_are_pinned() -> None:
    """The limits are part of the contract the docs and the schema describe."""
    assert GUIDANCE_VALUE_MAX_CHARS == 1000
    assert GUIDANCE_MAX_CHARS == 3000
    assert CUT_MARK == "…"
    assert GUIDANCE_KEYS == (
        "when_to_call",
        "when_to_use",
        "preconditions",
        "parameter_hints",
        "output_shape",
        "next_step",
    )


def test_prose_keys_kept_and_machine_keys_left_out() -> None:
    """Only the prose keys come through, unchanged when they are short."""
    assert bounded_guidance(_GUIDANCE) == {
        "when_to_call": "Use this to read one widget by its name.",
        "preconditions": "The widget must exist.",
        "parameter_hints": {"name": "The widget name, not its id."},
        "output_shape": "One object with `name` and `size`.",
        "next_step": "Call widgets.update to change it.",
    }


def test_older_when_to_use_name_is_passed_on() -> None:
    assert bounded_guidance({"when_to_use": "Use it for X."}) == {"when_to_use": "Use it for X."}


@pytest.mark.parametrize(
    "instructions",
    [
        None,
        {},
        {"pagination_hint": {"style": "cursor"}, "result_ordering": "newest first"},
        {"when_to_call": "", "next_step": "   ", "output_shape": None},
        {"when_to_call": 5, "preconditions": ["a list"], "parameter_hints": "not a map"},
        {"parameter_hints": {}},
        {"parameter_hints": {"name": "", "size": 3}},
    ],
)
def test_no_usable_guidance_gives_none(instructions: dict[str, Any] | None) -> None:
    """Missing, empty, wrong-type or machine-only instructions give no block."""
    assert bounded_guidance(instructions) is None


def test_wrong_type_values_are_skipped_not_fatal() -> None:
    """A bad value drops only itself; the good keys stay."""
    result = bounded_guidance(
        {
            "when_to_call": {"nested": "dict"},
            "parameter_hints": {"name": "Good hint.", "size": 12, 7: "number key"},
            "next_step": "Then call widgets.list.",
        }
    )
    assert result == {
        "parameter_hints": {"name": "Good hint."},
        "next_step": "Then call widgets.list.",
    }


def test_text_is_stripped() -> None:
    assert bounded_guidance({"next_step": "  Call it again.\n"}) == {"next_step": "Call it again."}


def test_long_text_is_cut_to_the_value_limit() -> None:
    result = bounded_guidance({"when_to_call": "a" * 5000})
    assert result is not None
    text = result["when_to_call"]
    assert len(text) == GUIDANCE_VALUE_MAX_CHARS
    assert text.endswith(CUT_MARK)
    assert text[:-1] == "a" * (GUIDANCE_VALUE_MAX_CHARS - 1)


def test_text_at_the_value_limit_is_not_cut() -> None:
    text = "b" * GUIDANCE_VALUE_MAX_CHARS
    assert bounded_guidance({"output_shape": text}) == {"output_shape": text}


def test_each_parameter_hint_is_cut_to_the_value_limit() -> None:
    result = bounded_guidance({"parameter_hints": {"name": "h" * 4000}})
    assert result is not None
    hint = result["parameter_hints"]["name"]
    assert len(hint) == GUIDANCE_VALUE_MAX_CHARS
    assert hint.endswith(CUT_MARK)


def test_total_limit_cuts_and_drops_later_keys() -> None:
    """Every key at its value limit: the total runs out and later keys go."""
    instructions = {key: "x" * 5000 for key in GUIDANCE_KEYS if key != "parameter_hints"}
    result = bounded_guidance(instructions)
    assert result is not None
    assert _text_size(result) <= GUIDANCE_MAX_CHARS
    # The first three texts fill the 3000 characters; the rest are left out.
    assert list(result) == ["when_to_call", "when_to_use", "preconditions"]
    assert all(len(text) == GUIDANCE_VALUE_MAX_CHARS for text in result.values())


def test_total_limit_cuts_the_text_that_crosses_it() -> None:
    instructions = {
        "when_to_call": "a" * 2500,
        "preconditions": "b" * 900,
        "next_step": "c" * 10,
    }
    result = bounded_guidance(instructions)
    assert result is not None
    assert len(result["when_to_call"]) == GUIDANCE_VALUE_MAX_CHARS
    assert len(result["preconditions"]) == 900
    # 3000 - 1000 - 900 = 1100 left: next_step fits whole.
    assert result["next_step"] == "c" * 10

    tight = bounded_guidance(
        {"when_to_call": "a" * 2500, "when_to_use": "b" * 2500, "next_step": "c" * 2500}
    )
    assert tight is not None
    assert len(tight["next_step"]) == GUIDANCE_VALUE_MAX_CHARS
    assert _text_size(tight) == GUIDANCE_MAX_CHARS


def test_many_parameter_hints_stay_within_the_total() -> None:
    """Many hints, also with long names, never push the block past the total."""
    hints = {f"param_{i}_" + "n" * 40: "hint text " * 5 for i in range(500)}
    result = bounded_guidance({"parameter_hints": hints, "next_step": "Then list them."})
    assert result is not None
    assert _text_size(result) <= GUIDANCE_MAX_CHARS
    assert len(result["parameter_hints"]) < len(hints)


# ---------------------------------------------------------------------------
# search_operations exact-op mode
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("OPERATION_CONTRACT_GUIDANCE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def embedding_stub(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("OPERATION_CONTRACT_GUIDANCE", "true" if on else "false")
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _register(
    embedding_stub: AsyncMock,
    *,
    op_id: str,
    llm_instructions: dict[str, Any] | None,
) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_handler,
        summary="Read one widget.",
        description="Reads one widget by its name.",
        parameter_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Widget name."}},
            "required": ["name"],
        },
        when_to_use=None,
        safety_level="safe",
        requires_approval=False,
        llm_instructions=llm_instructions,
        embedding_service=embedding_stub,
    )


async def _contract(op_id: str) -> dict[str, Any]:
    answer = await search_operations(_operator(), {"connector_id": "vault-1.x", "op_id": op_id})
    operation: dict[str, Any] = answer["operation"]
    return operation


def test_switch_is_off_by_default() -> None:
    assert get_settings().operation_contract_guidance is False


@pytest.mark.asyncio
async def test_switch_off_contract_has_no_guidance(
    embedding_stub: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing sessions see the same contract as before."""
    await _register(embedding_stub, op_id="vault.widget.read", llm_instructions=_GUIDANCE)
    contract = await _contract("vault.widget.read")
    assert "guidance" not in contract
    # The machine key still reaches the contract where it always did.
    assert contract["pagination_hint"] == {"style": "cursor", "param": "after"}

    _switch(monkeypatch, on=False)
    assert "guidance" not in await _contract("vault.widget.read")


@pytest.mark.asyncio
async def test_switch_on_contract_carries_bounded_guidance(
    embedding_stub: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_instructions = {**_GUIDANCE, "output_shape": "o" * 5000}
    await _register(embedding_stub, op_id="vault.widget.read", llm_instructions=long_instructions)
    _switch(monkeypatch, on=True)

    contract = await _contract("vault.widget.read")

    guidance = contract["guidance"]
    assert guidance["when_to_call"] == _GUIDANCE["when_to_call"]
    assert guidance["parameter_hints"] == {"name": "The widget name, not its id."}
    assert len(guidance["output_shape"]) == GUIDANCE_VALUE_MAX_CHARS
    assert guidance["output_shape"].endswith(CUT_MARK)
    assert not {"pagination_hint", "result_scalars"} & set(guidance)
    assert _text_size(guidance) <= GUIDANCE_MAX_CHARS
    assert contract["pagination_hint"] == {"style": "cursor", "param": "after"}


@pytest.mark.asyncio
async def test_switch_on_op_without_guidance_has_no_block(
    embedding_stub: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(embedding_stub, op_id="vault.widget.bare", llm_instructions=None)
    await _register(
        embedding_stub,
        op_id="vault.widget.machine",
        llm_instructions={"pagination_hint": {"style": "cursor", "param": "after"}},
    )
    _switch(monkeypatch, on=True)

    assert "guidance" not in await _contract("vault.widget.bare")
    assert "guidance" not in await _contract("vault.widget.machine")


@pytest.mark.asyncio
async def test_switch_on_ranked_hits_do_not_change(
    embedding_stub: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(embedding_stub, op_id="vault.widget.read", llm_instructions=_GUIDANCE)
    _switch(monkeypatch, on=True)

    answer = await search_operations(
        _operator(), {"connector_id": "vault-1.x", "query": "read one widget"}
    )

    assert answer["hits"], answer
    for hit in answer["hits"]:
        assert set(hit) == {
            "op_id",
            "summary",
            "description",
            "group_key",
            "safety_level",
            "requires_approval",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False])
async def test_exact_op_answer_matches_output_schema(
    embedding_stub: AsyncMock, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """The MCP answer, with and without the block, matches ``outputSchema``."""
    long_instructions = {**_GUIDANCE, "when_to_use": "w" * 4000}
    await _register(embedding_stub, op_id="vault.widget.read", llm_instructions=long_instructions)
    _switch(monkeypatch, on=on)

    answer = with_data_notice(
        await search_operations(
            _operator(), {"connector_id": "vault-1.x", "op_id": "vault.widget.read"}
        )
    )

    assert ("guidance" in answer["operation"]) is on
    entry = mcp_registry.get_tool("search_operations")
    assert entry is not None
    schema = entry[0].outputSchema
    assert schema is not None
    jsonschema.validate(
        instance=answer,
        schema=schema,
        cls=jsonschema.Draft202012Validator,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def test_output_schema_rejects_an_unknown_guidance_key() -> None:
    """The schema lists the prose keys only, so a machine key cannot slip in."""
    entry = mcp_registry.get_tool("search_operations")
    assert entry is not None
    schema = entry[0].outputSchema
    assert schema is not None
    exact = schema["oneOf"][1]
    guidance_schema = exact["properties"]["operation"]["properties"]["guidance"]
    assert set(guidance_schema["properties"]) == set(GUIDANCE_KEYS)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            instance={"pagination_hint": "x"},
            schema=guidance_schema,
            cls=jsonschema.Draft202012Validator,
        )
