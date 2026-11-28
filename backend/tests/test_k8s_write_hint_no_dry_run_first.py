# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No Kubernetes write operation tells the agent to dry-run itself before the real call.

A call of a Kubernetes write operation with ``dry_run='server'`` waits for
approval, like the real call. A hint that says "call it with a dry run first"
costs one more approval for each manifest.

The guard reads the texts that ``KubernetesConnector.register_operations()``
stores with the switch ``K8S_APPLY_ONE_APPROVAL_HINT`` on. It fails when a text
of a write operation tells the agent to call it with a dry run, or to read a
dry-run answer, before the real call. With the switch off, the start stores
the texts written in ``ops_write_meta.py``, and a test shows that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors.kubernetes import KUBERNETES_OPS, KubernetesConnector
from meho_backplane.connectors.kubernetes._apply_hint import with_apply_hint
from meho_backplane.operations import typed_register
from meho_backplane.settings import Settings, get_settings

#: Text that is not the end of a sentence. A dot inside a name such as
#: ``k8s.apply`` does not end the sentence.
_SAME_SENTENCE = r"(?:[^.]|\.(?=\S))*"
_DRY_RUN_FIRST = re.compile(
    rf"dry[_ ]run{_SAME_SENTENCE}\b(first|before|then)\b"
    rf"|\b(first|before)\b{_SAME_SENTENCE}dry[_ ]run"
    r"|\bbefore the real (apply|call)\b",
    re.IGNORECASE,
)
_DO_NOT = re.compile(r"\b(do not|don't|never)\b", re.IGNORECASE)


def _walk(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _walk(item)


def dry_run_first_advice(texts: Iterator[str]) -> list[str]:
    """The sentences of *texts* that tell the agent to dry-run before the real call."""
    found: list[str] = []
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if _DRY_RUN_FIRST.search(sentence) and not _DO_NOT.search(sentence):
                found.append(sentence)
    return found


def _agent_texts(stored: dict[str, Any]) -> Iterator[str]:
    """Every text an agent reads about one stored op: summary, description, hints, schema."""
    yield stored["summary"]
    yield stored["description"]
    yield from _walk(stored["llm_instructions"])
    yield from _walk(stored["parameter_schema"])


def _is_write(stored: dict[str, Any]) -> bool:
    return bool(stored["safety_level"] != "safe" or "write" in stored["tags"])


async def _stored_ops(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """The keyword arguments ``register_operations()`` stores for each op, by op id."""
    register = AsyncMock()
    monkeypatch.setattr(typed_register, "register_typed_operation", register)
    await KubernetesConnector.register_operations()
    return {call.kwargs["op_id"]: call.kwargs for call in register.await_args_list}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("K8S_APPLY_ONE_APPROVAL_HINT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", "true")
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "sentence",
    [
        "Run with dry_run='server' first to preview the would-be result.",
        "First call `k8s.apply` with `dry_run='server'`.",
        "First call k8s.apply with a dry run, then apply.",
        "warnings lists the API server's warnings; read them before the real apply.",
        "Read the dry-run answer before the real call.",
    ],
)
def test_the_guard_finds_the_advice(sentence: str) -> None:
    assert dry_run_first_advice(iter([sentence])) == [sentence]


@pytest.mark.parametrize(
    "sentence",
    [
        "A call with dry_run='server' waits for approval too.",
        "Do not call k8s.apply with dry_run='server' first.",
        "The approval request shows the result of a server dry run.",
    ],
)
def test_the_guard_lets_other_sentences_pass(sentence: str) -> None:
    assert dry_run_first_advice(iter([sentence])) == []


@pytest.mark.asyncio
async def test_no_stored_write_operation_says_dry_run_first_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    stored = await _stored_ops(monkeypatch)
    writes = {op_id: kwargs for op_id, kwargs in stored.items() if _is_write(kwargs)}
    assert "k8s.apply" in writes
    advice = {op_id: dry_run_first_advice(_agent_texts(kw)) for op_id, kw in writes.items()}
    assert {op_id: found for op_id, found in advice.items() if found} == {}


@pytest.mark.asyncio
async def test_the_start_stores_the_switch_on_texts_of_k8s_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails when ``register_operations()`` does not use the switch-on texts."""
    _switch_on(monkeypatch)
    stored = (await _stored_ops(monkeypatch))["k8s.apply"]
    written = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.apply")
    assert stored["summary"] == with_apply_hint(written).summary
    assert stored["llm_instructions"] == with_apply_hint(written).llm_instructions
    assert stored["summary"] != written.summary


@pytest.mark.asyncio
async def test_the_guard_finds_the_switch_off_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off, the start stores the written texts, and the guard finds them."""
    stored = (await _stored_ops(monkeypatch))["k8s.apply"]
    advice = dry_run_first_advice(_agent_texts(stored))
    assert any("dry_run='server' first" in sentence for sentence in advice), advice
    assert any("before the real apply" in sentence for sentence in advice), advice


@pytest.mark.asyncio
async def test_the_switch_on_hint_says_what_the_preview_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    stored = (await _stored_ops(monkeypatch))["k8s.apply"]
    when_to_use = stored["llm_instructions"]["when_to_use"]
    assert (
        "preview_operation needs no approval and does not contact the cluster: it shows the "
        "manifest text that was sent, after the check of the parameter shape."
    ) in when_to_use
    assert "the change" not in when_to_use
    assert (
        "Send all manifests of one deploy in one k8s.apply call, as one manifest text with "
        "the documents split by '---'"
    ) in when_to_use
    assert when_to_use.endswith(
        "Every call waits for a person, also with approval off; a standing grant does not cover it."
    )
    assert stored["requires_approval"] is True
    assert stored["safety_level"] == "dangerous"


@pytest.mark.parametrize("value", [None, "", "false", "0"])
@pytest.mark.asyncio
async def test_with_the_switch_off_the_start_stores_the_written_texts(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", value)
    stored = await _stored_ops(monkeypatch)
    for op in KUBERNETES_OPS:
        assert stored[op.op_id]["summary"] == op.summary
        assert stored[op.op_id]["llm_instructions"] == op.llm_instructions
    assert Settings.model_fields["k8s_apply_one_approval_hint"].default is False


def test_the_switch_changes_only_the_texts() -> None:
    written = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.apply")
    switched = with_apply_hint(written)
    assert switched.parameter_schema == written.parameter_schema
    assert switched.safety_level == written.safety_level
    assert switched.requires_approval == written.requires_approval
    assert switched.tags == written.tags
    assert switched.handler_attr == written.handler_attr
    assert switched.llm_instructions is not None and written.llm_instructions is not None
    assert set(switched.llm_instructions) == set(written.llm_instructions)
    assert set(switched.llm_instructions["parameter_hints"]) == set(
        written.llm_instructions["parameter_hints"]
    )
