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
* A builder that raises is fail-soft but not silent (#1628): the hook
  returns an explicit ``preview_unavailable`` marker + reason rather
  than propagating (the park always proceeds) or degrading to ``None``
  (the reviewer must be able to tell "blast-radius unknown" from a
  genuinely small action).
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
    build_permission_preflight,
    build_proposed_effect,
    register_permission_preflight,
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
async def test_no_builder_credential_class_op_yields_none() -> None:
    """A credential-class op with no builder still parks with no preview.

    Since #1856 a no-builder op gets the generic params-echo default, but
    the credential-class suppression runs *first*: ``vault.kv.put`` is
    ``credential_write``, so it collapses to the identifier-only default
    (caller fallback) rather than echoing its (secret-bearing) params.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vault.kv.put"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"path": "secret/data/x", "data": {"k": "v"}},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_no_builder_op_echoes_params() -> None:
    """A non-credential op with no builder echoes its params (#1856).

    Every approval-gated op gets param-level legibility for free: the
    requested params land under a ``params_echo`` envelope key (distinct
    from a computed ``preview``) tagged with the op's sensitivity class.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="keycloak.realm.update"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        connector_id="keycloak-1.x",
        params={"realm": "master", "displayName": "Master Realm", "enabled": True},
    )
    effect = await build_proposed_effect(ctx)
    assert effect == {
        "op_class": "write",
        "params_echo": {
            "realm": "master",
            "displayName": "Master Realm",
            "enabled": True,
        },
    }


@pytest.mark.asyncio
async def test_no_builder_op_empty_params_yields_none() -> None:
    """Empty params collapse to the identifier-only default (no echo).

    An empty params dict carries no more legibility than the bare
    identifier default, so the generic echo declines (returns ``None``)
    rather than storing an empty ``params_echo``.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="keycloak.realm.update"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        connector_id="keycloak-1.x",
        params={},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_no_builder_echo_scrubs_secret_param_keys() -> None:
    """The generic echo scrubs secret-by-name params (#1856 redaction).

    The connector-boundary engine matches secret *value* shapes but walks
    a params Mapping key-by-key without inspecting the key, so a
    structured ``{"password": "hunter2"}`` would otherwise echo verbatim.
    The key-name scrub closes that gap -- recursively, including nested
    dicts and lists -- while leaving non-secret params legible.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="keycloak.user.update"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        connector_id="keycloak-1.x",
        params={
            "username": "alice",
            "password": "hunter2",
            "client_secret": "s3cr3t",
            "profile": {"email": "alice@example.com", "token": "raw-token-value"},
            "credentials": [{"type": "password", "value": "nested-secret"}],
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    echo = effect["params_echo"]
    # Non-secret params stay legible.
    assert echo["username"] == "alice"
    assert echo["profile"]["email"] == "alice@example.com"
    # Secret-by-name params are masked, including nested keys + the whole
    # ``credentials`` subtree.
    assert echo["password"] == "***REDACTED***"
    assert echo["client_secret"] == "***REDACTED***"
    assert echo["profile"]["token"] == "***REDACTED***"
    assert echo["credentials"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_no_builder_echo_scrubs_secret_value_shapes() -> None:
    """The generic echo runs the connector-boundary value-shape scrub too.

    A JWT in an *un*-credential-named param (so the key-name scrub misses
    it) is still caught by the connector-boundary redaction engine -- the
    same pipeline the response path and the dispatch-request preview use.
    """
    # Built by concatenation so the literal never forms a single
    # high-entropy token the secret scanner flags (it is a fake fixture).
    jwt = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghij"
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="keycloak.user.update"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        connector_id="keycloak-1.x",
        params={"username": "alice", "note": jwt},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    echo = effect["params_echo"]
    assert echo["username"] == "alice"
    # The raw JWT never lands verbatim in the durable row.
    assert jwt not in str(echo["note"])


@pytest.mark.asyncio
async def test_builder_that_raises_yields_preview_unavailable_marker() -> None:
    """A builder exception degrades to an explicit unavailability marker (#1628).

    The raise never propagates -- the park (the safety-relevant action)
    always proceeds -- but the degradation is no longer silent: pre-#1628
    the hook returned ``None``, which collapsed to the identifier-only
    default indistinguishable from a genuinely small action. The marker
    + reason let the reviewer see "blast-radius unknown".
    """

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
    # No exception escapes; the marker names the failure for the reviewer.
    assert await build_proposed_effect(ctx) == {
        "op_class": "other",
        "preview_unavailable": True,
        "preview_error": "RuntimeError: dry-run hit the API and failed",
    }


@pytest.mark.asyncio
async def test_builder_failure_reason_is_truncated_and_message_less_safe() -> None:
    """The reviewer-facing reason is bounded and survives message-less raises.

    A pathological exception repr (an HTTP error echoing a response
    body) must not balloon the durable approval row; a bare
    ``ValueError()`` must still produce a usable type name.
    """

    async def _boom_long(_ctx: PreviewContext) -> dict[str, Any] | None:
        raise RuntimeError("x" * 2000)

    register_preview_builder("test.preview.boomlong", _boom_long)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="test.preview.boomlong"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert effect["preview_error"].endswith(" [truncated]")
    assert len(effect["preview_error"]) == 500 + len(" [truncated]")

    async def _boom_bare(_ctx: PreviewContext) -> dict[str, Any] | None:
        raise ValueError

    register_preview_builder("test.preview.boombare", _boom_bare)
    ctx_bare = PreviewContext(
        descriptor=_FakeDescriptor(op_id="test.preview.boombare"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    effect_bare = await build_proposed_effect(ctx_bare)
    assert effect_bare is not None
    assert effect_bare["preview_error"] == "ValueError"


@pytest.mark.asyncio
async def test_credential_class_op_generic_echo_suppressed() -> None:
    """A credential-class op with NO bespoke builder gets no generic echo.

    The generic params-echo default (#1856) can only do generic key-name /
    value-shape redaction and is not trusted to scrub a connector-specific
    secret shape, so an op classifying credential-class (here
    ``k8s.secret.create`` → ``credential_write``) with no registered
    builder collapses to the identifier-only default -- the durable
    approval row must not carry secret material.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.secret.create"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"name": "creds", "namespace": "ns", "string_data": {"k": "v"}},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_credential_class_op_bespoke_builder_runs() -> None:
    """A *bespoke* builder runs even for a credential-class op (#1857).

    Unlike the generic echo, a registered builder is trusted to own its
    own field discipline (the keycloak user-create preview scrubs the
    inline password before returning), so the credential-class
    suppression does not apply to it -- the same trust model the
    permission-preflight hook relies on. The builder's output is wrapped
    in the ``{op_class, preview}`` envelope.
    """
    builder_ran = False

    async def _scrubbed(_ctx: PreviewContext) -> dict[str, Any] | None:
        nonlocal builder_ran
        builder_ran = True
        # A real builder returns a scrubbed view; the test stand-in echoes
        # only non-secret identity so the contract is "builder ran + result
        # surfaced", not "result blindly trusted".
        return {"username": "svc-meho", "password": "***REDACTED***"}

    register_preview_builder("k8s.secret.create", _scrubbed)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.secret.create"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"name": "creds", "namespace": "ns", "string_data": {"k": "v"}},
    )
    effect = await build_proposed_effect(ctx)
    assert builder_ran is True, "a bespoke builder must run for a credential-class op"
    assert effect is not None
    assert effect["op_class"] == "credential_write"
    assert effect["preview"] == {"username": "svc-meho", "password": "***REDACTED***"}


# ---------------------------------------------------------------------------
# Permission preflight hook (G0.20-T4 #1504)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_preflight_runs_for_credential_class_op() -> None:
    """A permission preflight runs even when the op classifies credential-class.

    This is the whole point of the separate hook: ``vault.kv.put`` IS
    ``credential_write`` (so its *preview* is suppressed), yet its
    capability-only *permission* check must still run — it carries no
    secret material, and it is exactly the op whose write Vault may deny
    post-approval.
    """
    ran = False

    async def _preflight(_ctx: PreviewContext) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"check": "vault.capabilities-self", "will_be_denied": True}

    register_permission_preflight("vault.kv.put", _preflight)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vault.kv.put"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"path": "secret/data/x", "data": {"k": "v"}},
    )

    # The preview is suppressed (credential class), proving the preflight
    # is a genuinely separate, non-suppressed path.
    assert await build_proposed_effect(ctx) is None
    result = await build_permission_preflight(ctx)
    assert ran is True
    assert result == {"check": "vault.capabilities-self", "will_be_denied": True}


@pytest.mark.asyncio
async def test_permission_preflight_none_without_registration() -> None:
    """An op with no registered preflight yields ``None`` (no banner)."""
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="some.unregistered.op"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    assert await build_permission_preflight(ctx) is None


@pytest.mark.asyncio
async def test_permission_preflight_is_fail_soft() -> None:
    """A preflight that raises degrades to ``None`` — the park always proceeds."""

    async def _boom(_ctx: PreviewContext) -> dict[str, Any] | None:
        raise RuntimeError("capabilities probe hit Vault and failed")

    register_permission_preflight("test.preflight.boom", _boom)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="test.preflight.boom"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    assert await build_permission_preflight(ctx) is None
