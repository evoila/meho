# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Anthropic integration test for the ``AgentRun`` seam (G11.1-T1 / #808).

Unlike :mod:`tests.test_agent_run` (deterministic ``FunctionModel``), this
test drives the seam against the *real* Anthropic Messages API via
:func:`~meho_backplane.agent.run.default_model_factory`, proving the loop
calls a real ``call_operation`` end to end and that the turn budget + tool
wiring hold against the live model.

It is **opt-in**: the module skips entirely unless ``ANTHROPIC_API_KEY`` is
set (the sandbox + the always-on CI lane do not provision it). It is marked
``slow`` because it makes a billed network call. The seeded op
(``vault.kv.read``) lives in the per-test SQLite DB the unit conftest
provisions, so no PostgreSQL container is needed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.agent import AgentDefinition, AgentRunStatus, PydanticAgentRun
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set; real-Anthropic loop runs in CI/lab only",
    ),
]

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


class _ReadOnlyVaultConnector(Connector):
    """Connector registered so the seeded op's dispatch lookup succeeds."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _list_groups_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A deterministic, side-effect-free op the live model can call."""
    return {"groups": ["secrets", "system"], "tenant": str(operator.tenant_id)}


async def test_real_anthropic_loop_calls_operation() -> None:
    """The seam drives a real Anthropic loop that calls a MEHO operation."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_ReadOnlyVaultConnector)
    stub_embedding = AsyncMock()
    stub_embedding.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    stub_embedding.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    stub_embedding.dimension = EMBEDDING_DIMENSION
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.groups.list",
        handler=_list_groups_handler,
        summary="List the available secret groups.",
        description="Returns the configured groups of secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding,
    )

    runtime = PydanticAgentRun()  # default_model_factory -> real Anthropic
    definition = AgentDefinition(
        name="vault-explorer",
        system_prompt=(
            "You help operators explore MEHO. When asked which secret groups "
            "exist, call the call_operation_tool with connector_id='vault-1.x' "
            "and op_id='vault.groups.list' and report the groups it returns."
        ),
        request_limit=4,
    )
    operator = Operator(
        sub="op-integration",
        name="Integration Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )

    handle = runtime.start(definition, operator, "Which secret groups exist?")
    result = await runtime.result(handle)

    assert runtime.poll(handle) is AgentRunStatus.SUCCEEDED
    # The live model called the tool at least once and produced a final answer
    # mentioning a group the operation returned.
    assert result.tool_call_count >= 1
    assert "secrets" in str(result.output).lower()
