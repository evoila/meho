# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The manifest of a ``k8s.apply`` preview holds only the identity of each document.

Each preview goes through ``preview_dispatch``, the REST route
``POST /api/v1/operations/preview`` or the MCP tool ``preview_operation``,
over the stand-in Kubernetes API server of
``test_k8s_apply_card_through_dispatch``. Each value is made up at run time.

* ``redacted_body.manifest`` is one entry per document with its kind,
  namespace and name only. No value and no key of the documents is in any
  field of the answer.
* A name that looks like a secret is the marker in its entry.
* A small manifest whose YAML aliases name one list many times gives its
  preview fast and with little memory.
* All three hold with the ``PREVIEW_TYPED_WRITES`` switch off and on.
* The stored approval card of a call that waits hides a value that the
  value-shape engine knows by its key and value together, and shows the
  key names of a Secret with one large value.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import tracemalloc
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.handlers import handle_tools_call
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import meta_tools
from meho_backplane.operations._preview import card_param_echoes
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import isolated_registry
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _agent,
    _bff_env,
    _kubernetes_registry,
    _login,
    _park,
    _setup,
    _StandInServer,
    _Target,
)

__all__ = ["_bff_env", "_kubernetes_registry", "isolated_registry"]

_PATHS = ("preview_dispatch", "rest", "mcp")


@pytest.fixture(params=["0", "1"], ids=["switch-off", "switch-on"])
def _typed_writes(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("PREVIEW_TYPED_WRITES", request.param)
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


def _made_up() -> str:
    """Two letters, 20 hex characters and one letter: no word a hide rule knows."""
    return "qv" + secrets.token_hex(10) + "x"


def _secret_and_deployment(note: str, data_value: str, env_value: str) -> str:
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "stringData": {"note": note},
        "data": {"pw": base64.b64encode(data_value.encode()).decode()},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "prod"},
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "nginx:1.27",
                            "env": [{"name": "MODE", "value": env_value}],
                        }
                    ]
                }
            },
        },
    }
    return yaml.safe_dump_all([secret, deployment])


def _resolve_to_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolved(*_: Any, **__: Any) -> tuple[_Target, None]:
        return _Target(), None

    monkeypatch.setattr(meta_tools, "_resolve_target_or_error", _resolved)


def _arguments(manifest: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": "k8s.apply",
        "target": _Target().name,
        "params": {"manifest": manifest},
    }


def _through_dispatch(manifest: str) -> tuple[dict[str, Any], str]:
    answer = asyncio.run(
        preview_dispatch(
            operator=_agent(),
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": manifest},
        )
    )
    return answer, json.dumps(answer, default=str)


def _through_rest(manifest: str) -> tuple[dict[str, Any], str]:
    app = FastAPI()
    app.include_router(operations_api.router)
    caller: Operator = _login("person-rest")

    async def _operator() -> Operator:
        return caller

    app.dependency_overrides[verify_jwt_and_bind] = _operator
    response = TestClient(app).post("/api/v1/operations/preview", json=_arguments(manifest))
    assert response.status_code == 200, response.text
    return response.json(), response.text


def _through_mcp(manifest: str) -> tuple[dict[str, Any], str]:
    result = asyncio.run(
        handle_tools_call(
            _agent(), {"name": "preview_operation", "arguments": _arguments(manifest)}
        )
    )
    texts = [block["text"] for block in result["content"]]
    answer = result.get("structuredContent") or json.loads(texts[0])
    return answer, json.dumps(result, default=str)


def _preview(path: str, manifest: str) -> tuple[dict[str, Any], str]:
    """The answer of the preview on *path*, and every text of that answer."""
    if path == "rest":
        return _through_rest(manifest)
    if path == "mcp":
        return _through_mcp(manifest)
    return _through_dispatch(manifest)


def _identity_shape(entries: list[dict[str, Any]]) -> None:
    """Each entry holds ``kind`` and ``metadata`` with ``namespace`` and ``name``, nothing else."""
    for entry in entries:
        assert set(entry) <= {"kind", "metadata"}, entry
        assert set(entry["metadata"]) <= {"namespace", "name"}, entry


def _jwt_shape() -> str:
    return "eyJ" + secrets.token_hex(8) + ".eyJ" + secrets.token_hex(8) + "." + secrets.token_hex(8)


def _forms(value: str) -> tuple[str, ...]:
    """*value* as text, in base64 and JSON-escaped."""
    return (value, base64.b64encode(value.encode()).decode(), json.dumps(value)[1:-1])


def _four_documents(made_up: dict[str, str]) -> str:
    """A Secret, a Deployment and a ConfigMap, each holding values of *made_up*."""
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "stringData": {made_up["string_key"]: made_up["string_value"]},
        "data": {made_up["data_key"]: base64.b64encode(made_up["data_value"].encode()).decode()},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "prod"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "nginx:1.27",
                            "env": [{"name": made_up["env_key"], "value": made_up["env_value"]}],
                        }
                    ]
                }
            },
        },
    }
    config_map = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n  namespace: prod\n"
        f"data:\n  Authorization: Basic {made_up['basic']}\n  {made_up['jwt_key']}: x\n"
    )
    namespace = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: team-b\n"
    return "---\n".join([yaml.safe_dump_all([secret, deployment]), config_map, namespace])


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.usefixtures("_typed_writes")
def test_the_preview_shows_only_the_identity_of_each_document(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    made_up = {
        "string_key": _made_up(),
        "string_value": _made_up(),
        "data_key": _made_up(),
        "data_value": _made_up(),
        "env_key": "E" + secrets.token_hex(6).upper(),
        "env_value": _made_up(),
        "basic": _basic_value(),
        "jwt_key": _jwt_shape(),
    }
    manifest = _four_documents(made_up)

    answer, text = _preview(path, manifest)

    assert answer["status"] == "ok", answer
    for value in made_up.values():
        for form in _forms(value):
            assert form not in text
    shown = answer["redacted_body"]["manifest"]
    _identity_shape(shown)
    assert shown == [
        {"kind": "Secret", "metadata": {"namespace": "prod", "name": "db"}},
        {"kind": "Deployment", "metadata": {"namespace": "prod", "name": "web"}},
        {"kind": "ConfigMap", "metadata": {"namespace": "prod", "name": "cfg"}},
        {"kind": "Namespace", "metadata": {"name": "team-b"}},
    ]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.usefixtures("_typed_writes")
def test_a_name_that_looks_like_a_secret_is_hidden_in_its_entry(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    name = _jwt_shape()
    manifest = f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: prod\n"

    answer, text = _preview(path, manifest)

    for form in _forms(name):
        assert form not in text
    shown = answer["redacted_body"]["manifest"]
    _identity_shape(shown)
    assert shown[0]["kind"] == "ConfigMap"
    assert shown[0]["metadata"]["namespace"] == "prod"
    assert shown[0]["metadata"]["name"] != name
    assert "REDACTED" in shown[0]["metadata"]["name"]


def test_a_manifest_that_cannot_be_read_shows_only_the_marker() -> None:

    from meho_backplane.operations._preview import _PARAM_REDACTED, PreviewContext

    ctx = PreviewContext(
        descriptor=SimpleNamespace(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_agent(),
        target=None,
        params={"manifest": "a: [unclosed"},
        connector_id=_CONNECTOR_ID,
        agent_preview=True,
    )

    assert card_param_echoes(ctx) == {"manifest": _PARAM_REDACTED}


def _basic_value() -> str:
    return base64.b64encode(_made_up().encode()).decode()


def _key_ring_value() -> str:
    return "AQ" + secrets.token_hex(18) + "=="


def _config_map(body: str) -> str:
    return f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n  namespace: prod\n{body}"


def _custom(kind: str, body: str) -> str:
    return f"apiVersion: ex.io/v1\nkind: {kind}\nmetadata:\n  name: one\n  namespace: prod\n{body}"


#: Each case: the made-up secret part, and the manifest text that holds it unquoted.
_PAIR_CASES: dict[str, Any] = {
    "config-map-data-header": lambda v: _config_map(f"data:\n  Authorization: Basic {v}\n"),
    "config-map-annotation-header": lambda v: (
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n  namespace: prod\n"
        f"  annotations:\n    Authorization: Basic {v}\ndata:\n  a: b\n"
    ),
    "custom-kind-header": lambda v: _custom(
        "Probe", f"spec:\n  headers:\n    Authorization: Basic {v}\n"
    ),
    "config-map-data-key-ring": lambda v: _config_map(f"data:\n  key: {v}\n"),
    "custom-kind-key-ring": lambda v: _custom("Store", f"spec:\n  auth:\n    key: {v}\n"),
}


def _alias_manifest(levels: int) -> str:
    """A ConfigMap of under 600 bytes whose aliases name one list 10**levels times."""
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata: {name: a, namespace: prod}",
        "data: {x: y}",
        "spec:",
        "  l0: &l0 [q, q, q, q, q, q, q, q, q, q]",
    ]
    lines += [
        f"  l{n}: &l{n} [" + ", ".join([f"*l{n - 1}"] * 10) + "]" for n in range(1, levels + 1)
    ]
    return "\n".join(lines) + "\n"


def _timed(run: Any) -> tuple[Any, float, int]:
    """The result of *run*, the seconds it took and its peak of new memory in bytes."""
    tracemalloc.start()
    started = time.monotonic()
    try:
        result = run()
        return result, time.monotonic() - started, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("levels", [6, 7])
@pytest.mark.usefixtures("_typed_writes")
def test_an_alias_manifest_gives_its_preview_fast(
    monkeypatch: pytest.MonkeyPatch, levels: int
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    manifest = _alias_manifest(levels)
    assert len(manifest) < 600

    (answer, _text), seconds, peak = _timed(lambda: _through_dispatch(manifest))

    assert seconds < 5
    assert peak < 50 * 1024 * 1024
    assert answer["redacted_body"]["manifest"] == [
        {"kind": "ConfigMap", "metadata": {"namespace": "prod", "name": "a"}}
    ]


@pytest.mark.parametrize("case", sorted(_PAIR_CASES))
def test_the_card_hides_a_value_known_by_its_key(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    value = _key_ring_value() if "key-ring" in case else _basic_value()

    card = _park(_login("person-token"), _PAIR_CASES[case](value)).proposed_effect

    card_text = json.dumps(card, default=str)
    for form in _forms(value):
        assert form not in card_text
    assert card["preview"]["manifest"][0]["kind"] in {"ConfigMap", "Probe", "Store"}


def test_an_ordinary_config_map_value_shows_on_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, _StandInServer())
    plain = _made_up()

    card = _park(_login("person-token"), _config_map(f"data:\n  mode: {plain}\n")).proposed_effect

    assert card["preview"]["manifest"] == [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cfg", "namespace": "prod"},
            "data": {"mode": plain},
        }
    ]


def test_the_card_of_a_secret_with_one_large_value_shows_its_key_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(monkeypatch, _StandInServer())
    big, user, host = "qv" + secrets.token_hex(10_000) + "x", _made_up(), _made_up()
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "prod"},
        "stringData": {"big": big, "user": user, "host": host},
    }

    card = _park(_login("person-token"), yaml.safe_dump(secret)).proposed_effect

    card_text = json.dumps(card, default=str)
    for value in (big[:40], user, host):
        assert value not in card_text
    shown = card["preview"]["manifest"][0]
    assert "echo_omitted" not in shown
    assert sorted(shown["stringData"]) == ["big", "host", "user"]
    assert all(
        isinstance(item, str) and item != value
        for item, value in zip(
            (shown["stringData"][key] for key in ("big", "user", "host")),
            (big, user, host),
            strict=True,
        )
    )
