# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The synthetic preview shows a safe view of the params.

A ``typed`` / ``composite`` operation in a governed approval tier previews
through :mod:`meho_backplane.operations._composite_preview`. Its
``redacted_body`` is the same safe view the generic plan shows: a
secret-named value is masked, and every value of a free-form map (a
parameter the schema types as an object without fixed ``properties``, for
example the OVF properties of an appliance deploy) is masked while its
keys stay. The ``preview_hash`` still changes with every value through the
keyed ``params_digest``.

Every value below is made up. The OVF values are deliberately not
credential-shaped, so the tests prove the schema rule fires, not the
value-shape net.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    register_preview_builder,
)
from meho_backplane.operations._request_preview import (
    PARAMS_DIGEST_KEY,
    compute_preview_hash,
    preview_dispatch,
)
from meho_backplane.redaction import clear_overrides
from meho_backplane.settings import get_settings
from tests.test_operations_request_preview import (
    _FakeTarget,
    _handler_returning_ok,
    _make_operator,
    _RecordingHttpConnector,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches, the connector registry, redaction overrides and builders."""
    snapshot = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    yield
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(snapshot)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so descriptor inserts do not pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


_MARKER = "***REDACTED***"
_FAKE_ROOT_PASSWORD = "FAKE-root-pw-0001"
_FAKE_USER_DATA = base64.b64encode(
    b"#cloud-config\nusers:\n  - name: demo\n    passwd: FAKE-hash-0002\n"
).decode("ascii")

_DEPLOY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "placement": {
            "type": "object",
            "properties": {"cluster": {"type": "string"}},
        },
        "ovf_properties": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}


def _deploy_params(root_password: str = _FAKE_ROOT_PASSWORD) -> dict[str, Any]:
    return {
        "name": "appliance-01",
        "placement": {"cluster": "cluster-a"},
        "ovf_properties": {
            "guestinfo.hostname": "appliance-01",
            "guestinfo.root.passwd": root_password,
            "user-data": _FAKE_USER_DATA,
        },
    }


async def _keys_only_preview(ctx: PreviewContext) -> dict[str, Any]:
    """A plan builder that shows only the OVF property names, like the real one."""
    return {
        "resource": "vm",
        "name": ctx.params.get("name"),
        "ovf_property_keys": sorted(ctx.params.get("ovf_properties", {})),
    }


async def _register_deploy_op(
    stub_embedding_service: AsyncMock,
    *,
    op_id: str = "demo.appliance.deploy",
    parameter_schema: dict[str, Any] | None = None,
    builder: bool = True,
) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_RecordingHttpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_handler_returning_ok,
        summary="Deploy an appliance.",
        description="Typed, dangerous, requires approval.",
        parameter_schema=parameter_schema or _DEPLOY_SCHEMA,
        safety_level="dangerous",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    if builder:
        register_preview_builder(op_id, _keys_only_preview)


async def _preview(
    params: dict[str, Any], *, op_id: str = "demo.appliance.deploy"
) -> dict[str, Any]:
    envelope: dict[str, Any] = await preview_dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id=op_id,
        target=_FakeTarget(product="demo", version="1.x", name="demo-prod"),
        params=params,
    )
    return envelope


@pytest.mark.asyncio
async def test_preview_hides_free_form_map_values_and_keeps_keys(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every OVF property value is masked; the names and the other params stay."""
    await _register_deploy_op(stub_embedding_service)

    envelope = await _preview(_deploy_params())

    assert envelope["status"] == "ok"
    assert envelope["redacted_body"] == {
        "name": "appliance-01",
        "placement": {"cluster": "cluster-a"},
        "ovf_properties": {
            "guestinfo.hostname": _MARKER,
            "guestinfo.root.passwd": _MARKER,
            "user-data": _MARKER,
        },
    }
    assert envelope["proposed_effect"]["preview"]["ovf_property_keys"] == [
        "guestinfo.hostname",
        "guestinfo.root.passwd",
        "user-data",
    ]
    blob = json.dumps(envelope, default=str)
    assert _FAKE_ROOT_PASSWORD not in blob
    assert _FAKE_USER_DATA not in blob
    assert "FAKE-hash-0002" not in blob
    assert isinstance(envelope[PARAMS_DIGEST_KEY], str)
    assert len(envelope[PARAMS_DIGEST_KEY]) == 64


@pytest.mark.asyncio
async def test_preview_hash_changes_with_a_hidden_value(
    stub_embedding_service: AsyncMock,
) -> None:
    """Two previews that differ only in one masked value bind different hashes."""
    await _register_deploy_op(stub_embedding_service)

    first = await _preview(_deploy_params("FAKE-root-pw-0001"))
    second = await _preview(_deploy_params("FAKE-root-pw-0003"))
    same_as_first = await _preview(_deploy_params("FAKE-root-pw-0001"))

    assert first["redacted_body"] == second["redacted_body"]
    assert first["preview_hash"] != second["preview_hash"]
    assert first[PARAMS_DIGEST_KEY] != second[PARAMS_DIGEST_KEY]
    assert first["preview_hash"] == same_as_first["preview_hash"]
    # The digest is keyed, not a plain hash of the params.
    plain = hashlib.sha256(
        json.dumps(_deploy_params(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert first[PARAMS_DIGEST_KEY] != plain


@pytest.mark.asyncio
async def test_preview_hides_a_secret_named_param_in_redacted_body(
    stub_embedding_service: AsyncMock,
) -> None:
    """``redacted_body`` masks a secret-named value like the generic plan does."""
    schema = {
        "type": "object",
        "properties": {"host": {"type": "string"}, "password": {"type": "string"}},
    }
    await _register_deploy_op(
        stub_embedding_service,
        op_id="demo.thing.reconfigure",
        parameter_schema=schema,
        builder=False,
    )

    envelope = await _preview(
        {"host": "h1", "password": "FAKE-pw-0004"}, op_id="demo.thing.reconfigure"
    )

    assert envelope["status"] == "ok"
    assert envelope["redacted_body"] == {"host": "h1", "password": _MARKER}
    assert envelope["proposed_effect"]["params_echo"] == {"host": "h1", "password": _MARKER}
    assert "FAKE-pw-0004" not in json.dumps(envelope, default=str)


@pytest.mark.asyncio
async def test_preview_hides_a_map_param_the_schema_does_not_describe(
    stub_embedding_service: AsyncMock,
) -> None:
    """A map whose keys the schema does not fix is free-form: values masked, scalars kept."""
    await _register_deploy_op(
        stub_embedding_service,
        op_id="demo.thing.apply",
        parameter_schema={"type": "object"},
        builder=False,
    )

    envelope = await _preview(
        {
            "name": "thing-1",
            "count": 2,
            "settings": {"size": "medium", "token": "FAKE-token-0005"},
            "tags": ["a", "b"],
            "entries": [{"id": "e1", "secret_value": "FAKE-value-0006"}],
        },
        op_id="demo.thing.apply",
    )

    assert envelope["status"] == "ok"
    assert envelope["redacted_body"] == {
        "name": "thing-1",
        "count": 2,
        "settings": {"size": _MARKER, "token": _MARKER},
        "tags": ["a", "b"],
        "entries": [{"id": _MARKER, "secret_value": _MARKER}],
    }
    assert "FAKE-token-0005" not in json.dumps(envelope["redacted_body"])
    assert "FAKE-value-0006" not in json.dumps(envelope["redacted_body"])


def test_ingested_preview_hash_stays_without_a_digest() -> None:
    """An envelope with no digest hashes the six resolved-request keys only."""
    envelope = {
        "status": "ok",
        "connector_id": "gh-rest-3",
        "op_id": "gh.issue.create",
        "method": "POST",
        "resolved_path": "/repos/o/r/issues",
        "query": None,
        "redacted_body": {"title": "t"},
        "source_kind": "ingested",
    }
    projection = {
        key: envelope.get(key)
        for key in ("connector_id", "op_id", "method", "resolved_path", "query", "redacted_body")
    }
    canonical = json.dumps(projection, sort_keys=True, default=str, separators=(",", ":"))
    assert compute_preview_hash(envelope) == hashlib.sha256(canonical.encode()).hexdigest()
    assert compute_preview_hash({**envelope, PARAMS_DIGEST_KEY: "ab" * 32}) != compute_preview_hash(
        envelope
    )
