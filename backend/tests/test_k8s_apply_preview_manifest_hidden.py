# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The manifest of a ``k8s.apply`` preview, with the hiding of the approval card.

Each preview goes through ``preview_dispatch``, the REST route
``POST /api/v1/operations/preview`` or the MCP tool ``preview_operation``,
over the stand-in Kubernetes API server of
``test_k8s_apply_card_through_dispatch``. Each value is made up at run time.

* A Secret ``stringData`` value, a Secret ``data`` value and a Deployment
  ``env`` value are in no field of the answer.
* ``redacted_body.manifest`` equals the ``manifest`` of the approval card for
  the same manifest: the same fields and the same hidden marks.
* Both hold with the ``PREVIEW_TYPED_WRITES`` switch off and on.
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
from multidict import CIMultiDict

from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.kubernetes import ops_write_dangerous
from meho_backplane.connectors.kubernetes.ops_write_preview import MANIFEST_ECHO_MAX_BYTES
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


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.usefixtures("_typed_writes")
def test_the_preview_hides_manifest_values_as_the_approval_card(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    note, data_value, env_value = _made_up(), _made_up(), _made_up()
    manifest = _secret_and_deployment(note, data_value, env_value)
    data_b64 = base64.b64encode(data_value.encode()).decode()

    answer, text = _preview(path, manifest)

    assert answer["status"] == "ok", answer
    for value in (note, data_value, data_b64, env_value):
        assert value not in text
    shown = answer["redacted_body"]["manifest"]
    secret, deployment = shown
    assert secret["metadata"] == {"name": "db", "namespace": "prod"}
    assert deployment["spec"]["replicas"] == 2

    card = _park(_login("person-token"), manifest).proposed_effect
    assert shown == card["preview"]["manifest"]


def test_a_manifest_that_cannot_be_read_shows_only_the_marker() -> None:
    from types import SimpleNamespace

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


@pytest.mark.parametrize("path", ["preview_dispatch", "mcp"])
@pytest.mark.parametrize("case", sorted(_PAIR_CASES))
def test_a_value_known_by_its_key_is_hidden_in_the_preview_and_the_card(
    monkeypatch: pytest.MonkeyPatch, path: str, case: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    value = _key_ring_value() if "key-ring" in case else _basic_value()
    manifest = _PAIR_CASES[case](value)
    forms = (value, base64.b64encode(value.encode()).decode(), json.dumps(value)[1:-1])

    answer, text = _preview(path, manifest)
    card = _park(_login("person-token"), manifest).proposed_effect

    assert answer["status"] == "ok", answer
    card_text = json.dumps(card, default=str)
    for form in forms:
        assert form not in text
        assert form not in card_text
    shown = answer["redacted_body"]["manifest"]
    assert shown == card["preview"]["manifest"]
    assert shown[0]["kind"] in {"ConfigMap", "Probe", "Store"}


@pytest.mark.parametrize("path", ["preview_dispatch", "mcp"])
def test_an_ordinary_config_map_value_shows_in_the_preview_and_the_card(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _setup(monkeypatch, _StandInServer())
    _resolve_to_stand_in(monkeypatch)
    plain = _made_up()
    manifest = _config_map(f"data:\n  mode: {plain}\n")

    answer, _text = _preview(path, manifest)
    card = _park(_login("person-token"), manifest).proposed_effect

    shown = answer["redacted_body"]["manifest"]
    assert shown == card["preview"]["manifest"]
    assert shown == [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cfg", "namespace": "prod"},
            "data": {"mode": plain},
        }
    ]


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


def _send_identity_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer each dry run with the object's identity, without sending the document.

    So the test measures how MEHO builds the preview and the card, not how
    the Kubernetes client writes a large document to the stand-in server.
    """

    async def _send(_dyn: Any, _resource: Any, doc: dict[str, Any], **_: Any) -> tuple[Any, str]:
        metadata = doc.get("metadata") or {}
        identity = {key: metadata.get(key) for key in ("name", "namespace")}
        body = {"apiVersion": doc.get("apiVersion"), "kind": doc.get("kind"), "metadata": identity}
        return SimpleNamespace(status=200, headers=CIMultiDict()), json.dumps(body)

    monkeypatch.setattr(ops_write_dangerous, "_send_apply", _send)


@pytest.mark.parametrize("levels", [6, 7])
def test_an_alias_manifest_gives_a_small_preview_and_card_fast(
    monkeypatch: pytest.MonkeyPatch, levels: int
) -> None:
    _setup(monkeypatch, _StandInServer())
    _send_identity_only(monkeypatch)
    manifest = _alias_manifest(levels)
    assert len(manifest) < 600
    # The first call that waits sets up the database; it is not measured.
    _park(_login("person-token"), _config_map("data:\n  a: b\n"))
    (answer, _text), preview_seconds, preview_peak = _timed(lambda: _through_dispatch(manifest))
    row, card_seconds, card_peak = _timed(lambda: _park(_login("person-token"), manifest))

    assert preview_seconds < 5
    assert preview_peak < 50 * 1024 * 1024
    assert card_seconds < 5
    assert card_peak < 50 * 1024 * 1024
    for shown in (answer["redacted_body"]["manifest"], row.proposed_effect["preview"]["manifest"]):
        assert len(json.dumps(shown).encode()) <= MANIFEST_ECHO_MAX_BYTES
        assert shown == [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "a", "namespace": "prod"},
                "echo_omitted": shown[0]["echo_omitted"],
            }
        ]
