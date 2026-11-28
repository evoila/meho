# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run preview of ``k8s.apply`` passes on no cluster text for a document with hidden values.

A document holds hidden values when the manifest echo hides a value of it:
Secret ``data`` and ``stringData`` values, ``env`` and ``httpHeaders``
values, command lines, the last-applied annotation, and the values a
redaction setting hides. For such a document, ``warnings``, ``error`` and
``reason`` hold one fixed sentence. ``document``, ``kind``, ``namespace``,
``name`` and ``change`` stay. A document with no hidden value keeps the
cluster's text.

Each case goes through the real ``preview_dispatch`` over a stand-in
dynamic client. Every value is made at run time.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.kubernetes import ops_write_apply_dry_run as dry_run_module
from meho_backplane.connectors.kubernetes.ops_write_apply_dry_run import HIDDEN_VALUES_TEXT
from meho_backplane.operations._preview import _k8s_apply_redactor
from tests.test_k8s_apply_card_through_dispatch import _kubernetes_registry
from tests.test_k8s_apply_preview_dry_run import (
    _preview,
    _setup,
    _switch_off,
    _switch_on,
    _WarningResponse,
)
from tests.test_ui_approvals import _bff_env

__all__ = ["_bff_env", "_kubernetes_registry", "_switch_off"]

_FIXED = "MEHO does not pass on the cluster's text for a document that holds hidden values."
_CONFIG_MAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "cfg", "namespace": "prod"},
    "data": {"mode": "fast"},
}


def _value() -> str:
    return "v" + secrets.token_hex(8)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@dataclass
class _Answer:
    status: int = 201
    message: str = ""
    warnings: tuple[str, ...] = ()


@dataclass
class _Case:
    """A manifest, the stand-in's answer for each document name, and what entry *index* shows."""

    docs: list[dict[str, Any]]
    answers: dict[str, _Answer]
    index: int
    change: str
    text_field: str
    values: list[str]
    hidden_name_at: int | None = None
    whole_answer: bool = False
    kept: dict[str, Any] = field(default_factory=dict)


class _StandIn:
    """A stand-in API server that answers each document by its name."""

    def __init__(self, answers: dict[str, _Answer]) -> None:
        self.answers = answers
        self.requests: list[dict[str, Any]] = []

    @property
    def resources(self) -> Any:
        return self

    async def get(self, *, api_version: str, kind: str) -> Any:
        return type("Resource", (), {"kind": kind, "namespaced": True})()

    async def server_side_apply(self, resource: Any, **kwargs: Any) -> _WarningResponse:
        self.requests.append(kwargs)
        answer = self.answers.get(kwargs["name"], _Answer())
        body: Any = kwargs["body"]
        if answer.status >= 300:
            body = {"kind": "Status", "message": answer.message}
        return _WarningResponse(answer.status, body, list(answer.warnings))


def _secret_case(answer: Callable[[str, str], _Answer], change: str, text_field: str) -> _Case:
    v1, v2 = _value(), _value()
    v2_b64 = _b64(v2)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "stringData": {"pw": v1},
        "data": {"k": v2_b64[:8] + "\n" + v2_b64[8:]},
    }
    return _Case(
        docs=[secret, _CONFIG_MAP],
        answers={
            "db": answer(v1, v2),
            "cfg": _Answer(warnings=("mode fast is deprecated",)),
        },
        index=0,
        change=change,
        text_field=text_field,
        values=[v1, v2, _b64(v1), v2_b64],
        kept={"kind": "Secret", "namespace": "prod", "name": "db"},
        whole_answer=True,
    )


def _warning_case() -> _Case:
    return _secret_case(
        lambda v1, v2: _Answer(warnings=(f"value {v1} is weak; also {_b64(v1)}; also {v2}",)),
        "create",
        "warnings",
    )


def _refusal_case() -> _Case:
    return _secret_case(
        lambda v1, v2: _Answer(422, f"admission webhook denied: {v1} / {v2}"),
        "refused",
        "error",
    )


def _unavailable_case() -> _Case:
    return _secret_case(lambda v1, v2: _Answer(503, f"busy: {v1} / {v2}"), "not_checked", "reason")


def _refused(kind: str, spec: dict[str, Any], value: str, **extra: Any) -> _Case:
    doc = {
        "apiVersion": "apps/v1" if kind == "Deployment" else "v1",
        "kind": kind,
        "metadata": {"name": "app", "namespace": "prod", **extra},
        **spec,
    }
    return _Case(
        docs=[doc, _CONFIG_MAP],
        answers={
            "app": _Answer(422, f"denied: {value}"),
            "cfg": _Answer(warnings=("mode fast is deprecated",)),
        },
        index=0,
        change="refused",
        text_field="error",
        values=[value],
        kept={"kind": kind, "namespace": "prod", "name": "app"},
    )


def _pod_spec(container: dict[str, Any]) -> dict[str, Any]:
    return {"spec": {"containers": [{"name": "app", "image": "web:2", **container}]}}


def _env_case() -> _Case:
    value = _value()
    spec = {"spec": {"template": _pod_spec({"env": [{"name": "X", "value": value}]})}}
    return _refused("Deployment", spec, value)


def _header_case() -> _Case:
    value = _value()
    probe = {"httpGet": {"path": "/", "port": 80, "httpHeaders": [{"name": "H", "value": value}]}}
    return _refused("Pod", _pod_spec({"readinessProbe": probe}), value)


def _last_applied_case() -> _Case:
    value = _value()
    annotations = {"kubectl.kubernetes.io/last-applied-configuration": value}
    return _refused("ConfigMap", {"data": {"a": "b"}}, value, annotations=annotations)


def _command_case() -> _Case:
    value = _value()
    return _refused("Pod", _pod_spec({"command": ["run", value]}), value)


def _redaction_setting_case() -> _Case:
    value = _value()
    other = {**_CONFIG_MAP, "metadata": {"name": value, "namespace": "prod"}}
    return _Case(
        docs=[_CONFIG_MAP, other],
        answers={
            "cfg": _Answer(warnings=("mode fast is deprecated",)),
            value: _Answer(422, f"denied: {value}"),
        },
        index=1,
        change="refused",
        text_field="error",
        values=[value],
        hidden_name_at=1,
        kept={"kind": "ConfigMap", "namespace": "prod"},
    )


def _hides_name_at(position: int) -> Callable[[Any], Any]:
    """A redaction setting that hides ``manifest.<position>.metadata.name``."""

    def _redactor(ctx: Any) -> Any:
        redact = _k8s_apply_redactor(ctx)

        def _redact(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = redact(docs)
            if len(out) > position:
                hidden = json.loads(json.dumps(out[position]))
                hidden["metadata"]["name"] = "[REDACTED]"
                out[position] = hidden
            return out

        return _redact

    return _redactor


@pytest.mark.parametrize(
    "make",
    [
        _warning_case,
        _refusal_case,
        _unavailable_case,
        _env_case,
        _header_case,
        _last_applied_case,
        _command_case,
        _redaction_setting_case,
    ],
    ids=lambda make: make.__name__.strip("_"),
)
def test_a_document_with_hidden_values_shows_the_fixed_sentence(
    monkeypatch: pytest.MonkeyPatch, make: Callable[[], _Case]
) -> None:
    case = make()
    _switch_on(monkeypatch)
    cluster = _StandIn(case.answers)
    _setup(monkeypatch, cluster)  # type: ignore[arg-type]
    if case.hidden_name_at is not None:
        monkeypatch.setattr(
            dry_run_module, "_k8s_apply_redactor", _hides_name_at(case.hidden_name_at)
        )

    answer = _preview(yaml.safe_dump_all(case.docs))

    assert HIDDEN_VALUES_TEXT == _FIXED
    documents = answer["proposed_effect"]["preview"]["documents"]
    entry = documents[case.index]
    assert entry["document"] == case.index + 1
    assert entry["change"] == case.change
    expected = [_FIXED] if case.text_field == "warnings" else _FIXED
    assert entry[case.text_field] == expected
    assert {key: entry[key] for key in case.kept} == case.kept
    assert "name" in entry
    if case.text_field != "warnings":
        assert entry.get("warnings", []) == []
    # The document with no hidden value keeps the cluster's text.
    config_map = documents[1 - case.index]
    assert config_map["warnings"] == ["mode fast is deprecated"]
    # The Secret cases search the whole answer. The other cases search the
    # dry run's entries: the stand-in redaction setting of the last case is
    # set only for the dry run.
    text = json.dumps(answer if case.whole_answer else documents)
    for value in case.values:
        assert value not in text
    assert len(cluster.requests) == 2
    assert all(request["dry_run"] == "All" for request in cluster.requests)
