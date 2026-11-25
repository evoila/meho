# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No Kubernetes write operation tells the agent to dry-run itself before the real call.

A call of a Kubernetes write operation with ``dry_run='server'`` waits for
approval, like the real call. A hint that says "call it with a dry run first"
costs one more approval for each manifest.

The tests read the rows of the real registry (``KUBERNETES_OPS``), as the
connector registers them with the switch ``K8S_APPLY_ONE_APPROVAL_HINT``
on, and fail when a hint of a write operation says it. With the switch off,
the hint is as before, and a test shows that nothing changes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.connectors.kubernetes import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes._apply_hint import with_switched_apply_hint
from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.settings import Settings, get_settings

_DRY_RUN_FIRST = re.compile(
    r"dry[_ ]run[^.]*\b(first|before|then)\b|\b(first|before)\b[^.]*dry[_ ]run",
    re.IGNORECASE,
)
_DO_NOT = re.compile(r"\b(do not|don't|never)\b", re.IGNORECASE)


def _texts(op: KubernetesOp) -> Iterator[str]:
    """Every sentence an agent reads about *op*: names, texts, hints and schema descriptions."""

    def walk(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                yield from walk(item)

    yield op.summary
    yield op.description
    yield from walk(op.llm_instructions)
    yield from walk(op.parameter_schema)


def dry_run_first_advice(op: KubernetesOp) -> list[str]:
    """The sentences of *op* that tell the agent to dry-run before the real call."""
    found: list[str] = []
    for text in _texts(op):
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if _DRY_RUN_FIRST.search(sentence) and not _DO_NOT.search(sentence):
                found.append(sentence)
    return found


def _write_ops() -> list[KubernetesOp]:
    return [op for op in KUBERNETES_OPS if op.safety_level != "safe" or "write" in op.tags]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_registry_has_write_operations() -> None:
    assert "k8s.apply" in {op.op_id for op in _write_ops()}


def test_no_write_operation_says_dry_run_first_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", "true")
    registered = [with_switched_apply_hint(op) for op in _write_ops()]
    assert {
        op.op_id: dry_run_first_advice(op) for op in registered if dry_run_first_advice(op)
    } == {}


def test_the_guard_finds_the_old_text() -> None:
    """The old hint is in the registry as written, and the guard reads it."""
    apply_op = next(op for op in _write_ops() if op.op_id == "k8s.apply")
    advice = dry_run_first_advice(apply_op)
    assert any("dry_run='server' first" in sentence for sentence in advice), advice


def test_the_new_hint_says_the_two_things(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", "true")
    apply_op = with_switched_apply_hint(next(op for op in _write_ops() if op.op_id == "k8s.apply"))
    when_to_use = (apply_op.llm_instructions or {})["when_to_use"]
    assert "preview_operation" in when_to_use
    assert "needs no approval" in when_to_use
    assert "all manifests of one deploy in one k8s.apply call" in when_to_use
    assert "Requires approval" in when_to_use
    assert apply_op.requires_approval is True
    assert apply_op.safety_level == "dangerous"


@pytest.mark.parametrize("value", [None, "", "false", "0"])
def test_with_the_switch_off_every_operation_is_as_before(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("K8S_APPLY_ONE_APPROVAL_HINT", raising=False)
    else:
        monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", value)
    assert [with_switched_apply_hint(op) for op in KUBERNETES_OPS] == list(KUBERNETES_OPS)
    assert Settings.model_fields["k8s_apply_one_approval_hint"].default is False


def test_the_switch_changes_only_the_hint_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_APPLY_ONE_APPROVAL_HINT", "true")
    before = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.apply")
    after = with_switched_apply_hint(before)
    assert after.parameter_schema == before.parameter_schema
    assert after.safety_level == before.safety_level
    assert after.requires_approval == before.requires_approval
    assert after.tags == before.tags
    assert after.handler_attr == before.handler_attr
    assert after.llm_instructions is not None and before.llm_instructions is not None
    assert after.llm_instructions["output_shape"] == before.llm_instructions["output_shape"]
    assert set(after.llm_instructions["parameter_hints"]) == set(
        before.llm_instructions["parameter_hints"]
    )
