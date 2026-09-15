# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the governed guest-kubeconfig read ``k8s.secret.read_to_ref`` (#3496).

Proves the no-transit contract: the op reads a Secret's ``data`` value on
a k8s / Supervisor target, stages it to a tenant-scoped Vault
``secret_ref`` under the operator's identity, and returns **only** the
ref + provenance — the kubeconfig never enters the op result (the leak
vector the ``read_to_ref`` shape closes over the issue's ``read_data``
sketch), the op params, or a broadcast payload.

The kubernetes API and Vault are stubbed at their boundaries (the
in-process Vault fake + a mocked ``CoreV1Api``) so the suite runs in the
secret-free unit lane with no Docker and no real secret. The canary
kubeconfig strings are the leak sentinels the no-leak assertions grep
against.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import (
    classify_op,
    redact_payload,
    scrub_secret_named_values,
)
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_secret_read import (
    DEFAULT_KUBECONFIG_DATA_KEY,
    DEFAULT_STAGE_FIELD,
    SECRET_READ_OPS,
    KubernetesSecretDataError,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary kubeconfig — asserted to NEVER appear in the result / params.
# The server VIP + bearer token are the load-bearing secret fields.
# ---------------------------------------------------------------------------

_CANARY_SERVER = "https://guest-vip.envision.test.invalid:6443"
_CANARY_TOKEN = "guest-admin-client-cert-canary-MUST-NOT-LEAK"
_CANARY_KUBECONFIG = f"""apiVersion: v1
kind: Config
current-context: guest
contexts:
- name: guest
  context: {{cluster: guest, user: admin}}
clusters:
- name: guest
  cluster:
    server: {_CANARY_SERVER}
users:
- name: admin
  user:
    token: {_CANARY_TOKEN}
"""
_CANARY_B64 = base64.b64encode(_CANARY_KUBECONFIG.encode("utf-8")).decode("ascii")

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a0")
_EXPECTED_REF = f"tenants/{_TENANT_ID}/envision-guest"


# ---------------------------------------------------------------------------
# Fixtures (mirror test_connectors_k8s_write.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    name: str
    host: str
    port: int | None
    secret_ref: str
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="envision-supervisor",
    host="supervisor.envision.test.invalid",
    port=6443,
    secret_ref="k8s/envision-supervisor",
)


def _stub_kubeconfig() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://supervisor.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return _stub_kubeconfig()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-read-to-ref-test",
        name="Read-To-Ref Test Operator",
        email=None,
        raw_jwt="op.read-to-ref.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_kubeconfig() -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


def _core_v1_patch() -> Any:
    return patch("meho_backplane.connectors.kubernetes.ops_secret_read.client.CoreV1Api")


def _secret_with(data: dict[str, str]) -> MagicMock:
    """A stand-in ``V1Secret`` whose ``.data`` is *data* (base64 values)."""
    return MagicMock(data=data)


# ---------------------------------------------------------------------------
# Registration + schema surface
# ---------------------------------------------------------------------------


def test_registered_with_caution_and_approval() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    assert "k8s.secret.read_to_ref" in by_id, "op not registered in KUBERNETES_OPS"
    op = by_id["k8s.secret.read_to_ref"]
    assert op.safety_level == "caution"
    assert op.requires_approval is True


def test_handler_attr_resolves_on_connector() -> None:
    op = SECRET_READ_OPS[0]
    assert getattr(KubernetesConnector, op.handler_attr, None) is not None


def test_schema_shape_and_defaults() -> None:
    op = SECRET_READ_OPS[0]
    props = op.parameter_schema["properties"]
    assert {"name", "namespace", "data_key", "register_as", "field"} <= set(props)
    assert op.parameter_schema["required"] == ["name", "namespace", "register_as"]
    assert op.parameter_schema["additionalProperties"] is False
    assert props["data_key"]["default"] == DEFAULT_KUBECONFIG_DATA_KEY == "value"
    assert props["field"]["default"] == DEFAULT_STAGE_FIELD == "kubeconfig"


# ---------------------------------------------------------------------------
# Classification — credential_read, aggregate-only broadcast
# ---------------------------------------------------------------------------


def test_classifies_credential_read_and_collapses_broadcast() -> None:
    """The op classifies credential_read; its broadcast payload is aggregate-only."""
    assert classify_op("k8s.secret.read_to_ref") == "credential_read"
    raw = {"params": {"name": "guest-kubeconfig", "namespace": "envision-ns", "register_as": "g"}}
    payload = redact_payload("credential_read", raw, "ok")
    assert payload == {"op_class": "credential_read", "result_status": "ok"}


def test_result_fields_survive_the_credential_read_scrub() -> None:
    """The #2467 credential_read response scrub must NOT redact the returned ref.

    The op's whole value is the secret_ref it hands back; if the key-name
    scrub collapsed it the caller could never register the target.
    """
    result = {
        "secret_ref": _EXPECTED_REF,
        "field": "kubeconfig",
        "registered_as": "envision-guest",
        "name": "guest-kubeconfig",
        "namespace": "envision-ns",
        "data_key": "value",
        "value_sha256": "deadbeef",
        "length": 42,
    }
    scrubbed, found = scrub_secret_named_values(result)
    assert found is False
    assert scrubbed["secret_ref"] == _EXPECTED_REF
    assert scrubbed["field"] == "kubeconfig"


# ---------------------------------------------------------------------------
# Happy path — read Secret, stage to a tenant-scoped Vault ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_secret_and_stages_to_tenant_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_client(monkeypatch)
    conn = _make_connector()
    operator = _make_operator()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value.read_namespaced_secret = AsyncMock(
            return_value=_secret_with({"value": _CANARY_B64})
        )
        result = await conn.k8s_secret_read_to_ref(
            operator=operator,
            target=_TARGET,
            params={
                "name": "envision-guest-kubeconfig",
                "namespace": "envision-ns",
                "register_as": "envision-guest",
            },
        )

    # Result is only the ref + provenance — the derived path is tenant-scoped
    # and matches what `targets create --name envision-guest` (secret_ref
    # omitted) reads.
    assert result["secret_ref"] == _EXPECTED_REF
    assert result["field"] == "kubeconfig"
    assert result["registered_as"] == "envision-guest"
    assert result["data_key"] == "value"
    assert result["length"] == len(_CANARY_KUBECONFIG.encode("utf-8"))
    assert result["value_sha256"] == hashlib.sha256(_CANARY_KUBECONFIG.encode()).hexdigest()

    # The decoded kubeconfig reached Vault (the point) — under the operator's
    # own JWT, at the derived tenant path, on the default 'secret' mount.
    put = fake.secrets.kv.v2.put_calls[-1]
    assert put["path"] == _EXPECTED_REF
    assert put["mount_point"] == "secret"
    assert put["secret"] == {"kubeconfig": _CANARY_KUBECONFIG}
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt


@pytest.mark.asyncio
async def test_result_envelope_carries_no_secret_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redaction contract: no kubeconfig bytes in the op result envelope."""
    install_fake_client(monkeypatch)
    conn = _make_connector()
    params = {
        "name": "envision-guest-kubeconfig",
        "namespace": "envision-ns",
        "register_as": "envision-guest",
    }
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value.read_namespaced_secret = AsyncMock(
            return_value=_secret_with({"value": _CANARY_B64})
        )
        result = await conn.k8s_secret_read_to_ref(
            operator=_make_operator(), target=_TARGET, params=params
        )

    blob = str(result)
    assert _CANARY_TOKEN not in blob, "bearer token leaked into the op result"
    assert _CANARY_SERVER not in blob, "server VIP leaked into the op result"
    assert _CANARY_KUBECONFIG not in blob, "raw kubeconfig leaked into the op result"
    assert _CANARY_B64 not in blob, "base64 kubeconfig leaked into the op result"
    # The value never entered the op params either (only the ref does).
    assert _CANARY_TOKEN not in str(params)
    # Provenance is present so an approver/audit can correlate.
    assert result["value_sha256"] and result["length"] > 0


@pytest.mark.asyncio
async def test_custom_data_key_and_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_client(monkeypatch)
    conn = _make_connector()
    payload = base64.b64encode(b"a-different-kubeconfig-blob").decode("ascii")
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value.read_namespaced_secret = AsyncMock(
            return_value=_secret_with({"admin.conf": payload})
        )
        result = await conn.k8s_secret_read_to_ref(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "name": "s",
                "namespace": "ns",
                "register_as": "envision-guest",
                "data_key": "admin.conf",
                "field": "kubeconfig",
            },
        )
    assert result["data_key"] == "admin.conf"
    assert fake.secrets.kv.v2.put_calls[-1]["secret"] == {
        "kubeconfig": "a-different-kubeconfig-blob"
    }


# ---------------------------------------------------------------------------
# Error paths — value never leaks, nothing is written on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_data_key_raises_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value.read_namespaced_secret = AsyncMock(
            return_value=_secret_with({"other-key": _CANARY_B64})
        )
        with pytest.raises(KubernetesSecretDataError) as excinfo:
            await conn.k8s_secret_read_to_ref(
                operator=_make_operator(),
                target=_TARGET,
                params={
                    "name": "envision-guest-kubeconfig",
                    "namespace": "envision-ns",
                    "register_as": "envision-guest",
                },
            )
    # Error names the missing key, never the value; no Vault write happened.
    assert "value" in str(excinfo.value)
    assert _CANARY_B64 not in str(excinfo.value)
    assert fake.secrets.kv.v2.put_calls == []


@pytest.mark.asyncio
async def test_invalid_base64_raises_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value.read_namespaced_secret = AsyncMock(
            return_value=_secret_with({"value": "!!! not base64 !!!"})
        )
        with pytest.raises(KubernetesSecretDataError):
            await conn.k8s_secret_read_to_ref(
                operator=_make_operator(),
                target=_TARGET,
                params={
                    "name": "envision-guest-kubeconfig",
                    "namespace": "envision-ns",
                    "register_as": "envision-guest",
                },
            )
    assert fake.secrets.kv.v2.put_calls == []
