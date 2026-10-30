# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``k8s.secret.compare_to_ref``: a Secret key against its Vault copy.

The Kubernetes API is a mocked ``CoreV1Api`` and Vault is the in-process
fake from ``_vault_fakes`` with a small per-path store. The leak test uses
values that match no known credential pattern, so only the op's own design
keeps them out of the answer, the broadcast payload and the trace.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import hvac.exceptions
import pytest
from kubernetes_asyncio.client.rest import ApiException

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload, scrub_secret_named_values
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_secret_compare import SECRET_COMPARE_OPS
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.redaction.flight_recorder import redact_span
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_vault

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000c0df")
_BASE = f"tenants/{_TENANT_ID}"

# Plain words and digits: no credential pattern matches these.
_VALUE = "copper-meadow-lantern-5512"
_OTHER_VALUE = "copper-meadow-lantern-5513"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    yield


@dataclass
class _StubTarget:
    name: str = "apps-cluster"
    host: str = "cluster.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/apps-cluster"
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://cluster.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _operator() -> Operator:
    return Operator(
        sub="op-secret-compare-test",
        name=None,
        email=None,
        raw_jwt="secret-compare.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _install_vault(
    monkeypatch: pytest.MonkeyPatch,
    store: dict[str, tuple[int, dict[str, Any]]],
    *,
    forbidden: frozenset[str] = frozenset(),
) -> Any:
    fake = install_fake_vault(monkeypatch)

    def _read(path: str, mount_point: str = "secret", **_kwargs: Any) -> dict[str, Any]:
        full = f"{mount_point}/{path}"
        if full in forbidden:
            raise hvac.exceptions.Forbidden("permission denied")
        if full not in store:
            raise hvac.exceptions.InvalidPath(f"no secret at {full}")
        version, data = store[full]
        return {"data": {"data": data, "metadata": {"version": version}}}

    fake.secrets.kv.v2.read_secret_version = _read
    return fake


def _secret(data: dict[str, str] | None, resource_version: str = "48213") -> MagicMock:
    return MagicMock(data=data, metadata=MagicMock(resource_version=resource_version))


async def _compare(
    read_result: Any,
    *,
    ref: dict[str, Any] | None = None,
    key: str = "admin-password",
) -> dict[str, Any]:
    conn = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.ops_secret_compare.client.CoreV1Api") as core,
    ):
        if isinstance(read_result, Exception):
            core.return_value.read_namespaced_secret = AsyncMock(side_effect=read_result)
        else:
            core.return_value.read_namespaced_secret = AsyncMock(return_value=read_result)
        return await conn.k8s_secret_compare_to_ref(
            operator=_operator(),
            target=_StubTarget(),
            params={
                "namespace": "keycloak",
                "name": "keycloak-admin",
                "key": key,
                "ref": ref or {"path": f"{_BASE}/keycloak/admin", "key": "password"},
            },
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registered_as_safe_read_without_approval() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    op = by_id["k8s.secret.compare_to_ref"]
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert getattr(KubernetesConnector, op.handler_attr, None) is not None
    assert op.parameter_schema["required"] == ["namespace", "name", "key", "ref"]
    assert classify_op("k8s.secret.compare_to_ref") == "credential_read"


# ---------------------------------------------------------------------------
# Behaviour: same, different, missing, forbidden
# ---------------------------------------------------------------------------


async def test_secret_and_vault_copy_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_vault(
        monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _VALUE})}
    )
    result = await _compare(_secret({"admin-password": _b64(_VALUE)}))
    assert result["same"] is True
    k8s_copy, vault_copy = result["copies"]
    assert k8s_copy == {
        "source": "k8s",
        "namespace": "keycloak",
        "name": "keycloak-admin",
        "key": "admin-password",
        "found": True,
        "matches_first": True,
        "version": "48213",
        "reason": None,
    }
    assert vault_copy == {
        "source": "vault",
        "mount": "secret",
        "path": f"{_BASE}/keycloak/admin",
        "key": "password",
        "found": True,
        "matches_first": True,
        "version": 6,
        "reason": None,
    }
    # The Vault copy is read with the caller's own login.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "secret-compare.jwt"


async def test_secret_and_vault_copy_different(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _OTHER_VALUE})})
    result = await _compare(_secret({"admin-password": _b64(_VALUE)}))
    assert result["same"] is False
    assert [c["matches_first"] for c in result["copies"]] == [True, False]
    assert all(c["found"] for c in result["copies"])


@pytest.mark.parametrize("vault_value", [123, 1.5, True, None, [1], {"a": 1}])
async def test_secret_text_never_matches_a_non_string_vault_value(
    monkeypatch: pytest.MonkeyPatch, vault_value: Any
) -> None:
    """A Secret holding ``123`` matches the Vault string ``"123"``, not the number."""
    text = json.dumps(vault_value, separators=(",", ":"))
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": vault_value})})
    result = await _compare(_secret({"admin-password": _b64(text)}))
    assert result["same"] is False
    assert [c["found"] for c in result["copies"]] == [True, True]
    assert [c["matches_first"] for c in result["copies"]] == [True, False]

    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (7, {"password": text})})
    result = await _compare(_secret({"admin-password": _b64(text)}))
    assert result["same"] is True


async def test_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _VALUE})})
    result = await _compare(ApiException(status=404, reason="Not Found"))
    assert result["same"] is False
    k8s_copy, vault_copy = result["copies"]
    assert (k8s_copy["found"], k8s_copy["reason"], k8s_copy["version"]) == (
        False,
        "not_found",
        None,
    )
    assert (vault_copy["found"], vault_copy["matches_first"]) == (True, False)


async def test_secret_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _VALUE})})
    result = await _compare(_secret({"other": _b64(_VALUE)}))
    k8s_copy = result["copies"][0]
    assert (k8s_copy["found"], k8s_copy["reason"], k8s_copy["version"]) == (
        False,
        "key_not_found",
        "48213",
    )


async def test_vault_copy_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {})
    result = await _compare(_secret({"admin-password": _b64(_VALUE)}))
    assert result["same"] is False
    vault_copy = result["copies"][1]
    assert (vault_copy["found"], vault_copy["reason"]) == (False, "not_found")


async def test_secret_forbidden_does_not_fail_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _VALUE})})
    result = await _compare(ApiException(status=403, reason="Forbidden"))
    assert result["same"] is False
    k8s_copy, vault_copy = result["copies"]
    assert (k8s_copy["found"], k8s_copy["reason"]) == (False, "forbidden")
    assert vault_copy["found"] is True


async def test_vault_copy_forbidden_does_not_fail_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(
        monkeypatch,
        {f"secret/{_BASE}/keycloak/admin": (6, {"password": _VALUE})},
        forbidden=frozenset({f"secret/{_BASE}/keycloak/admin"}),
    )
    result = await _compare(_secret({"admin-password": _b64(_VALUE)}))
    vault_copy = result["copies"][1]
    assert (vault_copy["found"], vault_copy["matches_first"], vault_copy["reason"]) == (
        False,
        False,
        "forbidden",
    )
    # A Vault path outside the caller's tenant is forbidden too.
    result = await _compare(
        _secret({"admin-password": _b64(_VALUE)}),
        ref={"path": "tenants/someone-else/admin", "key": "password"},
    )
    assert (result["copies"][1]["found"], result["copies"][1]["reason"]) == (False, "forbidden")


async def test_other_kubernetes_errors_still_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_vault(monkeypatch, {})
    with pytest.raises(ApiException):
        await _compare(ApiException(status=500, reason="Internal Server Error"))


# ---------------------------------------------------------------------------
# Leak test: no value and no hash in the answer, audit, broadcast, trace
# ---------------------------------------------------------------------------


async def test_no_value_or_hash_reaches_result_broadcast_or_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch, {f"secret/{_BASE}/keycloak/admin": (6, {"password": _OTHER_VALUE})})
    result = await _compare(_secret({"admin-password": _b64(_VALUE)}))
    assert result["same"] is False

    # The credential-read answer scrub keeps the whole answer as it is. The
    # audit row stores this same handler answer and the value-free params.
    scrubbed, found = scrub_secret_named_values(result)
    assert found is False
    assert scrubbed == result

    params = {
        "namespace": "keycloak",
        "name": "keycloak-admin",
        "key": "admin-password",
        "ref": {"path": f"{_BASE}/keycloak/admin", "key": "password"},
    }
    broadcast = redact_payload(classify_op("k8s.secret.compare_to_ref"), {"params": params}, "ok")
    assert broadcast == {"op_class": "credential_read", "result_status": "ok"}

    # The Secret read's response body never reaches the flight recorder.
    span = redact_span(
        op_id="k8s.secret.compare_to_ref",
        method="GET",
        response_body={"kind": "Secret", "data": {"admin-password": _b64(_VALUE)}},
        response_content_type="application/json",
        delete_shaped_patterns=(),
    )
    assert span.body_recorded is False

    blobs = {
        "result": json.dumps(result),
        "broadcast": json.dumps(broadcast),
        "trace": span.model_dump_json(),
    }
    for where, blob in blobs.items():
        for value in (_VALUE, _OTHER_VALUE):
            raw = value.encode("utf-8")
            for marker in (
                value,
                _b64(value),
                hashlib.sha256(raw).hexdigest(),
                hashlib.sha1(raw).hexdigest(),
                hashlib.md5(raw).hexdigest(),
            ):
                assert marker not in blob, f"value or hash leaked into the {where}"


def test_response_schema_has_no_value_field() -> None:
    props = SECRET_COMPARE_OPS[0].response_schema["properties"]["copies"]["items"]["properties"]
    assert not {"value", "data", "sha256", "hash", "digest"} & set(props)
