# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for read-side Kubernetes ``Secret`` redaction (#3501).

Two layers:

* the pure structural redactor
  (:func:`~meho_backplane.connectors.kubernetes.redaction.redact_kubernetes_payload`)
  -- single object, list envelope, nested, digest correctness, immutability;
* the wired read path -- ``custom_resource_row`` and the ``k8s.cr.list`` /
  ``k8s.cr.info`` handlers projecting a raw ``Secret`` (the dynamic read
  pointed at core ``v1/secrets``), proving no base64 ``data`` value ever
  rides back in the envelope and that the per-value digest is present.

No event loop is needed for the pure layer; the handler layer stubs
``CustomObjectsApi`` at the connector boundary (no Docker, no real
secret), matching ``test_connectors_k8s_storage_cr.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_customresource import custom_resource_row
from meho_backplane.connectors.kubernetes.redaction import (
    REDACTED_PREFIX,
    redact_kubernetes_payload,
    redact_secret_value,
)
from meho_backplane.settings import get_settings

# A base64 ``data`` value and a plaintext ``stringData`` value that match
# NO named credential pattern (no ``key=value`` label, no ``Bearer`` /
# JWT shape) -- so a green assertion proves the redaction is *structural*,
# not a lucky Tier-1 pattern hit.
_TLS_KEY_B64 = "TFMwdExTMUNSVWRKVGlCUVVrbFdRVlJGSUV0RldRPT0="
_APP_TOKEN_B64 = "cGxhaW4tYXBwLXRva2VuLXdpdGgtbm8tbGFiZWw="
_STRINGDATA_PLAINTEXT = "hunter2-no-pattern-here"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str = "rke2-meho"
    host: str = "rke2-meho.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/rke2-meho"
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget()


def _stub_kubeconfig() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "clusters": [{"name": "c", "cluster": {"server": "https://rke2-meho.test.invalid:6443"}}],
        "contexts": [{"name": "default", "context": {"cluster": "c", "user": "u"}}],
        "users": [{"name": "u", "user": {"token": "kubeconfig-token"}}],
    }


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return _stub_kubeconfig()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-k8s-redaction-test",
        name="K8s Redaction Test Operator",
        email=None,
        raw_jwt="op.k8s.redaction.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_secret(
    *, namespace: str | None = "team-a", with_string_data: bool = False
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": "app-secret",
            "namespace": namespace,
            "creationTimestamp": "2026-09-01T00:00:00Z",
            "labels": {"app": "demo"},
        },
        "data": {"tls.key": _TLS_KEY_B64, "app-token": _APP_TOKEN_B64},
    }
    if with_string_data:
        obj["stringData"] = {"config.ini": _STRINGDATA_PLAINTEXT}
    return obj


# ---------------------------------------------------------------------------
# Pure redactor
# ---------------------------------------------------------------------------


def test_redact_secret_value_is_placeholder_plus_stable_digest() -> None:
    out = redact_secret_value(_TLS_KEY_B64)
    assert out.startswith(REDACTED_PREFIX + ";sha256:")
    assert out.endswith("]")
    # Deterministic across calls (correlatable), and NOT the raw value.
    assert out == redact_secret_value(_TLS_KEY_B64)
    assert _TLS_KEY_B64 not in out
    # 12 hex chars of digest between the prefix and the closing bracket.
    digest = out[len(REDACTED_PREFIX) + len(";sha256:") : -1]
    assert len(digest) == 12
    assert all(c in "0123456789abcdef" for c in digest)


def test_single_secret_object_data_and_stringdata_redacted() -> None:
    out = redact_kubernetes_payload(_make_secret(with_string_data=True))
    # Key names preserved.
    assert set(out["data"]) == {"tls.key", "app-token"}
    assert set(out["stringData"]) == {"config.ini"}
    # Values redacted with a digest; raw bytes gone.
    assert out["data"]["tls.key"] == redact_secret_value(_TLS_KEY_B64)
    assert out["data"]["app-token"] == redact_secret_value(_APP_TOKEN_B64)
    assert out["stringData"]["config.ini"] == redact_secret_value(_STRINGDATA_PLAINTEXT)
    blob = str(out)
    assert _TLS_KEY_B64 not in blob
    assert _APP_TOKEN_B64 not in blob
    assert _STRINGDATA_PLAINTEXT not in blob
    # Non-secret fields survive.
    assert out["type"] == "Opaque"
    assert out["metadata"]["name"] == "app-secret"


def test_list_envelope_of_secrets_all_redacted() -> None:
    """A ``kind: List`` (or any list) of Secrets — every item scrubbed."""
    envelope = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [_make_secret(namespace="a"), _make_secret(namespace="b")],
    }
    out = redact_kubernetes_payload(envelope)
    for item in out["items"]:
        assert item["data"]["tls.key"] == redact_secret_value(_TLS_KEY_B64)
    assert _TLS_KEY_B64 not in str(out)


def test_nested_secret_deep_in_structure_is_caught() -> None:
    nested = {"outer": {"inner": [{"wrap": _make_secret()}]}}
    out = redact_kubernetes_payload(nested)
    assert out["outer"]["inner"][0]["wrap"]["data"]["app-token"] == redact_secret_value(
        _APP_TOKEN_B64
    )
    assert _APP_TOKEN_B64 not in str(out)


def test_non_secret_object_is_untouched() -> None:
    """A ConfigMap-shaped object (has ``data`` but kind != Secret) is not redacted."""
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "data": {"log-level": "debug", "feature-flag": "on"},
    }
    assert redact_kubernetes_payload(cm) == cm


def test_input_is_not_mutated() -> None:
    original = _make_secret()
    snapshot = {"tls.key": _TLS_KEY_B64, "app-token": _APP_TOKEN_B64}
    redact_kubernetes_payload(original)
    assert original["data"] == snapshot


def test_scalars_pass_through() -> None:
    assert redact_kubernetes_payload("x") == "x"
    assert redact_kubernetes_payload(7) == 7
    assert redact_kubernetes_payload(None) is None


# ---------------------------------------------------------------------------
# custom_resource_row -- single CR read of a Secret surfaces redacted data
# ---------------------------------------------------------------------------


def test_custom_resource_row_secret_surfaces_redacted_data_with_digest() -> None:
    row = custom_resource_row(_make_secret(with_string_data=True))
    assert row["kind"] == "Secret"
    assert row["name"] == "app-secret"
    # Secret-only fields present, values redacted (key inventory kept).
    assert set(row["data"]) == {"tls.key", "app-token"}
    assert row["data"]["tls.key"].startswith(REDACTED_PREFIX)
    assert row["string_data"]["config.ini"].startswith(REDACTED_PREFIX)
    assert _TLS_KEY_B64 not in str(row)
    assert _APP_TOKEN_B64 not in str(row)
    assert _STRINGDATA_PLAINTEXT not in str(row)


def test_custom_resource_row_non_secret_has_no_data_fields() -> None:
    row = custom_resource_row(
        {"apiVersion": "v1", "kind": "IPAddressPool", "metadata": {"name": "default"}}
    )
    assert "data" not in row
    assert "string_data" not in row


# ---------------------------------------------------------------------------
# Wired handler path -- k8s.cr.info / k8s.cr.list projecting a raw Secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cr_info_of_secret_returns_no_bytes_but_keeps_keys() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.get_namespaced_custom_object = AsyncMock(return_value=_make_secret())
        result = await connector.k8s_cr_info(
            _make_operator(),
            _TARGET,
            {
                "group": "",
                "version": "v1",
                "plural": "secrets",
                "name": "app-secret",
                "namespace": "team-a",
            },
        )

    assert result["kind"] == "Secret"
    assert set(result["data"]) == {"tls.key", "app-token"}
    assert result["data"]["tls.key"] == redact_secret_value(_TLS_KEY_B64)
    assert _TLS_KEY_B64 not in str(result)
    assert _APP_TOKEN_B64 not in str(result)


@pytest.mark.asyncio
async def test_cr_list_of_secrets_returns_no_bytes_in_envelope() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [_make_secret(namespace="team-a"), _make_secret(namespace="team-a")]
            }
        )
        result = await connector.k8s_cr_list(
            _make_operator(),
            _TARGET,
            {"group": "", "version": "v1", "plural": "secrets", "namespace": "team-a"},
        )

    assert result["total"] == 2
    for row in result["rows"]:
        assert row["data"]["tls.key"] == redact_secret_value(_TLS_KEY_B64)
    # No base64 value survives anywhere in the rows/total envelope.
    assert _TLS_KEY_B64 not in str(result)
    assert _APP_TOKEN_B64 not in str(result)
