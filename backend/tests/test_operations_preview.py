# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the per-op ``proposed_effect`` builder hook.

G11.7 follow-up (#1437). :mod:`meho_backplane.operations._preview` is the
opt-in seam that lets an op compute a side-effect-free preview at
approval-park time so the reviewer reads the diff in the approval queue.

Coverage matrix (per Issue #1437 acceptance criteria):

* ``k8s.apply`` parks with its server-dry-run summary populated, and the
  builder forces ``dry_run="server"`` regardless of the parked params'
  ``dry_run`` -- the preview never persists.
* An op with no registered builder yields ``None`` (caller falls back to
  the identifier-only default).
* A builder that raises is fail-soft: the hook returns ``None`` rather
  than propagating, so the park always proceeds.
* A credential-class op is suppressed (no raw preview in the durable
  row), reusing the :func:`classify_op` sensitivity classification.

The kubernetes API surface is mocked so the test runs in every CI lane.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    register_preview_builder,
)


@dataclass
class _FakeDescriptor:
    """Minimal stand-in -- the hook only reads ``op_id``."""

    op_id: str


def _operator() -> Operator:
    return Operator(
        sub="op-preview-test",
        name="Preview Test Operator",
        email=None,
        raw_jwt="op.preview.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeTarget:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "rke2-meho"


_SINGLE_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: argocd
spec:
  replicas: 2
"""


@pytest.mark.asyncio
async def test_k8s_apply_preview_forces_server_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The k8s.apply builder runs the SSA dry-run and wraps the summary.

    The parked params carry ``dry_run="none"`` (a real apply), yet the
    preview must force ``dry_run="server"`` so it never persists. The
    returned dict is the ``{op_class, preview}`` envelope; the preview is
    the handler's identity-only summary (redaction-safe).
    """
    captured: dict[str, Any] = {}

    async def _fake_apply(
        connector: KubernetesConnector,
        target: Any,
        operator: Operator,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        captured["params"] = params
        return {
            "dry_run": True,
            "field_manager": "meho",
            "applied": [
                {
                    "api_version": "apps/v1",
                    "kind": "Deployment",
                    "name": "web",
                    "namespace": "argocd",
                    "resource_version": "dry",
                    "uid": "u-1",
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.k8s_apply",
        _fake_apply,
    )

    connector = KubernetesConnector(kubeconfig_loader=None)  # type: ignore[arg-type]
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": _SINGLE_MANIFEST, "dry_run": "none"},
    )

    effect = await build_proposed_effect(ctx)

    assert effect is not None
    # The dry-run flag is forced on even though the parked call was a real apply.
    assert captured["params"]["dry_run"] == "server"
    assert effect["op_class"] == "other"
    assert effect["preview"]["dry_run"] is True
    assert effect["preview"]["applied"][0]["resource_version"] == "dry"
    # Identity-only summary: no Secret data / manifest body echoed.
    assert "data" not in effect["preview"]["applied"][0]


@pytest.mark.asyncio
async def test_k8s_apply_preview_none_without_connector() -> None:
    """No resolved connector instance ⇒ no preview (caller uses default)."""
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": _SINGLE_MANIFEST},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_no_builder_op_yields_none() -> None:
    """An op without a registered builder parks with no preview."""
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vault.kv.put"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"path": "secret/data/x"},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_builder_that_raises_is_fail_soft() -> None:
    """A builder exception degrades to no-preview, never propagates."""

    async def _boom(_ctx: PreviewContext) -> dict[str, Any] | None:
        raise RuntimeError("dry-run hit the API and failed")

    register_preview_builder("test.preview.boom", _boom)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="test.preview.boom"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    # No exception escapes; the park proceeds with the identifier-only default.
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_credential_class_op_preview_suppressed() -> None:
    """A credential-class op never stores a raw preview (redaction-safe).

    Even with a registered builder, an op classifying as a credential
    class (here ``k8s.secret.create`` → ``credential_write``) is
    suppressed before the builder runs -- the durable approval row must
    not carry secret material.
    """
    builder_ran = False

    async def _leaky(_ctx: PreviewContext) -> dict[str, Any] | None:
        nonlocal builder_ran
        builder_ran = True
        return {"secret_value": "super-secret"}

    register_preview_builder("k8s.secret.create", _leaky)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.secret.create"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"name": "creds", "namespace": "ns", "string_data": {"k": "v"}},
    )
    assert await build_proposed_effect(ctx) is None
    assert builder_ran is False, "sensitive-class gate must run before the builder"
