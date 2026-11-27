# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` off, the explain answer is the one of main, value for value.

For each caller and each risk level (and a ``safe`` operation marked to
always wait), the whole answer of ``POST /api/v1/policy/explain`` is
compared with the answer recorded from main
(``snapshots/policy_explain_switch_off.json``). So is the answer for a
person in a hosted agent run. ``APPROVAL_ALLOW_SELF_APPROVAL`` is on, so a
new part would have something to say.

Fixture values are made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.main import app
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    register_preview_builder,
)
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._own_approval_cases import (
    ADMIN_TOKEN,
    ALL_CALLERS,
    OPERATOR_TOKEN,
    OPS,
    SAFE_MARKED,
    embedding_service,
    op,
    register_levels,
    switches,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_MAIN = Path(__file__).parent / "snapshots" / "policy_explain_switch_off.json"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    return embedding_service()


async def _blast_radius(ctx: PreviewContext) -> dict[str, Any]:
    return {
        "blast_radius": {
            "object": {"kind": "vm", "id": "vm-1"},
            "children": [],
            "irreversibility": "permanent",
        }
    }


@pytest.fixture(autouse=True)
def _builders() -> Iterator[None]:
    saved = dict(_PREVIEW_BUILDERS)
    register_preview_builder(op("destructive"), _blast_radius)
    yield
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(saved)


async def _answers(client: TestClient, key: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for caller in ALL_CALLERS:
        for name in OPS:
            answers[f"{caller.name} / {name}"] = caller.ask(client, key, op(name))
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: "agent:hosted"}):
        for person in (OPERATOR_TOKEN, ADMIN_TOKEN):
            for name in ("caution", "dangerous", SAFE_MARKED):
                answer = await explain_call(
                    CallQuestion(
                        asker=person.operator(), me=True, connector_id="demo-1.x", op_id=op(name)
                    )
                )
                answers[f"hosted run, {person.name} / {name}"] = answer.model_dump(mode="json")
    return answers


@pytest.mark.asyncio
async def test_with_the_switch_off_every_answer_is_the_one_of_main(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="false", APPROVAL_ALLOW_SELF_APPROVAL="true")
    await register_levels(embedding)
    key = make_rsa_keypair("kid-ownappr-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        answers = await _answers(client, key)
    main = json.loads(_MAIN.read_text())
    assert len(main) == len(ALL_CALLERS) * len(OPS) + 6
    assert answers == main
