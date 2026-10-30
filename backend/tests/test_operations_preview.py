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

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.operations._preview import (
    _PERMISSION_PREFLIGHTS,
    _PREVIEW_BUILDERS,
    PreviewContext,
    build_permission_preflight,
    build_proposed_effect,
    register_permission_preflight,
    register_preview_builder,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_vault


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


_WEBHOOK_DENIAL = 'admission webhook "machine.example.test" denied the request: not allowed'


# Credential fixtures assembled from fragments so gitleaks' built-in
# rules do not false-positive on the test source (the
# ``test_operations_connector_error_redaction.py`` posture).
_PASSWORD_SECRET = "P@ssw0rd" + "!withMore"
_PASSWORD_DENIAL = (
    'admission webhook "machine.example.test" denied the request: login failed for admin: '
    + "password"
    + "="
    + _PASSWORD_SECRET
)
_BEARER_SECRET = "eyJtoken" + "$value!123" + "abcdef"
_BEARER_DENIAL = (
    'admission webhook "machine.example.test" denied the request: header was '
    + "Bearer "
    + _BEARER_SECRET
)

#: The fixed part of the reason ``k8s_apply`` renders for a 403 on
#: ``_SINGLE_MANIFEST``. The straddle test places a credential across
#: the 500-char cap relative to it, and asserts the prefix still matches
#: so the placement math cannot go stale silently.
_K8S_REASON_PREFIX = (
    "ApplyRejectedError: the API server rejected the server-side apply of "
    "apps/v1/Deployment argocd/web (HTTP 403 Forbidden): "
)


class _StatusFailureResponse:
    """A raw API response carrying a Kubernetes ``Status`` failure body."""

    status = 403
    reason = "Forbidden"

    def __init__(self, message: str = _WEBHOOK_DENIAL) -> None:
        self._message = message

    async def text(self) -> str:
        return json.dumps(
            {
                "kind": "Status",
                "apiVersion": "v1",
                "status": "Failure",
                "reason": "Forbidden",
                "code": 403,
                "message": self._message,
            }
        )


class _DryRunWithWarningResponse:
    """A 200 dry-run answer that carries one API-server ``Warning`` header."""

    status = 200
    reason = "OK"
    headers = CIMultiDictProxy(
        CIMultiDict([("Warning", '299 - "apps/v1beta1 Deployment is deprecated"')])
    )

    async def text(self) -> str:
        return json.dumps({"kind": "Deployment", "metadata": {"name": "web"}})


@pytest.mark.asyncio
async def test_k8s_apply_preview_shows_api_server_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approval preview carries the dry-run's ``Warning`` header texts (#3842)."""
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_DryRunWithWarningResponse())

    async def _dynamic_client(_api_client: Any) -> Any:
        return dyn

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
        _dynamic_client,
    )
    connector = KubernetesConnector(kubeconfig_loader=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "_get_api_client", AsyncMock(return_value=MagicMock()))
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": _SINGLE_MANIFEST, "dry_run": "none"},
    )

    effect = await build_proposed_effect(ctx)

    assert effect is not None
    assert effect["preview"]["applied"][0]["warnings"] == ["apps/v1beta1 Deployment is deprecated"]


async def _k8s_apply_preview_denied_with(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> dict[str, Any]:
    """Run the real ``k8s_apply`` preview against a stub that answers 403 with *message*."""
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_StatusFailureResponse(message))

    async def _dynamic_client(_api_client: Any) -> Any:
        return dyn

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
        _dynamic_client,
    )
    connector = KubernetesConnector(kubeconfig_loader=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "_get_api_client", AsyncMock(return_value=MagicMock()))
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": _SINGLE_MANIFEST, "dry_run": "none"},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    return effect


@pytest.mark.asyncio
async def test_k8s_apply_preview_shows_api_server_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry-run the API server denies is not shown as a clean preview (#3876).

    Runs the real ``k8s_apply`` handler against a stubbed dynamic client
    that answers 403 with a ``Status`` failure body. The park preview must
    carry the denial, not a summary with no resourceVersion and no uid.
    """
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_StatusFailureResponse())

    async def _dynamic_client(_api_client: Any) -> Any:
        return dyn

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
        _dynamic_client,
    )
    connector = KubernetesConnector(kubeconfig_loader=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "_get_api_client", AsyncMock(return_value=MagicMock()))
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": _SINGLE_MANIFEST, "dry_run": "none"},
    )

    effect = await build_proposed_effect(ctx)

    assert effect is not None
    assert "preview" not in effect
    assert effect["preview_unavailable"] is True
    assert effect["preview_error"].startswith("ApplyRejectedError: ")
    assert "HTTP 403 Forbidden" in effect["preview_error"]
    assert _WEBHOOK_DENIAL in effect["preview_error"]
    assert dyn.server_side_apply.call_args.kwargs["dry_run"] == "All"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "secret", "marker"),
    [
        (_PASSWORD_DENIAL, _PASSWORD_SECRET, "[REDACTED:api_key]"),
        (_BEARER_DENIAL, _BEARER_SECRET, "[REDACTED:bearer_token]"),
    ],
)
async def test_k8s_apply_preview_redacts_credential_in_denial(
    monkeypatch: pytest.MonkeyPatch, message: str, secret: str, marker: str
) -> None:
    """A denial that quotes a credential does not put it into the approval row (#3876).

    ``preview_error`` is durable and reviewer-facing. The server message
    is kept, but the credential inside it is replaced by the Tier-1
    marker, the same way ``extras.upstream_message`` is treated.
    """
    effect = await _k8s_apply_preview_denied_with(monkeypatch, message)

    reason = effect["preview_error"]
    assert reason.startswith(_K8S_REASON_PREFIX)
    assert "denied the request" in reason
    assert marker in reason
    assert secret not in reason


@pytest.mark.asyncio
async def test_k8s_apply_preview_redacts_credential_across_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential that straddles the 500-char cap leaves no cleartext fragment (#3876).

    Redaction must run before truncation: cutting first would leave the
    head of the password in the row as a fragment the patterns no longer
    match.
    """
    label = "password" + "="
    pad_len = 500 - len(_K8S_REASON_PREFIX) - len(label) - 3
    message = "x" * (pad_len - 1) + " " + label + _PASSWORD_SECRET
    raw_len = len(_K8S_REASON_PREFIX) + len(message)
    # The un-redacted reason would be cut three characters into the secret.
    assert raw_len - len(_PASSWORD_SECRET) < 500 < raw_len

    effect = await _k8s_apply_preview_denied_with(monkeypatch, message)

    reason = effect["preview_error"]
    assert reason.startswith(_K8S_REASON_PREFIX)
    assert reason.endswith(" [truncated]")
    assert len(reason) == 500 + len(" [truncated]")
    assert "[REDACTED:" in reason
    assert label not in reason
    assert _PASSWORD_SECRET[:3] not in reason


@pytest.mark.asyncio
async def test_builder_failure_reason_withholds_message_when_redaction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the redactor itself fails, the message is withheld rather than stored raw."""

    def _broken_redact(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("redactor down")

    monkeypatch.setattr("meho_backplane.operations._preview.redact", _broken_redact)

    async def _boom(_ctx: PreviewContext) -> dict[str, Any] | None:
        raise RuntimeError(_PASSWORD_DENIAL)

    register_preview_builder("test.preview.boomredact", _boom)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="test.preview.boomredact"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert effect["preview_error"] == "RuntimeError: <message withheld: redaction unavailable>"
    assert _PASSWORD_SECRET not in effect["preview_error"]


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
    the credential-class suppression runs *first*:
    ``vmware.composite.vm.guest.env.read`` is ``credential_write`` with no
    bespoke builder, so it collapses to the identifier-only default (caller
    fallback) rather than echoing its params. (The userpass writes got
    bespoke builders in #4850; this ``safe`` read never waits for approval,
    so it is a still-builderless credential op.)
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vmware.composite.vm.guest.env.read"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"vm": "vm-42", "password": "s3kret"},
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
@pytest.mark.parametrize(
    "key",
    [
        # The well-known names the echo always masked.
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "secret_id",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "session_token",
        "private_key",
        "credentials",
        # Longer names the broadcast scrub flags (#3808).
        "vcsa_root_password",
        "adminPassword",
        "nsx-admin-password",
        "sddcManagerApiKey",
        "ssh_private_key",
        "kubeconfig",
        "PassWord",
        "passWord",
    ],
)
async def test_no_builder_echo_masks_every_key_the_broadcast_scrub_flags(key: str) -> None:
    """The echo and the broadcast scrub agree on which params are secret (#3808).

    Both use the same name check, so a param that the broadcast path flags
    as secret is always masked in the stored echo.
    """
    from meho_backplane.broadcast.events import scrub_broadcast_params

    params = {"name": "fake-appliance", key: "FAKE-secret-value-0001"}
    _scrubbed, secret_detected = scrub_broadcast_params(params)
    assert secret_detected is True

    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="holodeck.config.apply"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params=params,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["params_echo"] == {"name": "fake-appliance", key: "***REDACTED***"}


@pytest.mark.asyncio
async def test_no_builder_echo_keeps_scalar_settings_under_secret_names() -> None:
    """Bare scalars under a secret-shaped name stay visible (#3808).

    The broadcast scrub masks only string, mapping and list values under a
    secret-shaped name. The echo uses the same rule, so settings like
    ``bind_secret_id`` or ``secret_id_ttl`` stay readable for the approver
    while a real string secret is still masked.
    """
    from meho_backplane.broadcast.events import scrub_broadcast_params

    params = {
        "role_name": "fake-role",
        "bind_secret_id": False,
        "secret_id_ttl": 600,
        "secret_id_num_uses": 5,
        "secret_id": "FAKE-secret-value-0002",
    }
    scrubbed, _detected = scrub_broadcast_params(params)
    assert scrubbed["bind_secret_id"] is False
    assert scrubbed["secret_id_ttl"] == 600

    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vault.auth.approle.write"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params=params,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["params_echo"] == {
        "role_name": "fake-role",
        "bind_secret_id": False,
        "secret_id_ttl": 600,
        "secret_id_num_uses": 5,
        "secret_id": "***REDACTED***",
    }


@pytest.mark.asyncio
async def test_no_builder_echo_scrubs_secrets_nested_in_tuples() -> None:
    """A secret inside a tuple is masked, like the broadcast scrub does (#3808).

    The echo walks the same container types as the broadcast scrub: any
    mapping and any sequence other than a string. A tuple of dicts must
    not slip past the walk and reach the stored echo as a plain list with
    the secret still inside.
    """
    from meho_backplane.broadcast.events import scrub_broadcast_params

    params = {"items": ({"name": "fake-vm", "adminPassword": "FAKE-secret-value-0003"},)}
    scrubbed, secret_detected = scrub_broadcast_params(params)
    assert secret_detected is True
    assert scrubbed["items"][0]["adminPassword"] != "FAKE-secret-value-0003"

    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="holodeck.config.apply"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params=params,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["params_echo"] == {
        "items": [{"name": "fake-vm", "adminPassword": "***REDACTED***"}]
    }


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
    ``vmware.composite.vm.guest.env.read`` → ``credential_write``) with no
    registered builder collapses to the identifier-only default -- the
    durable approval row must not carry secret material.
    """
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vmware.composite.vm.guest.env.read"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"vm": "vm-42", "guest_secret_ref": "targets/app-vm"},
    )
    assert await build_proposed_effect(ctx) is None


@pytest.mark.asyncio
async def test_credential_class_op_bespoke_builder_runs(monkeypatch: pytest.MonkeyPatch) -> None:
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

    # setitem, not register_preview_builder: the stub must not stay in the
    # process-wide registry after this test.
    monkeypatch.setitem(_PREVIEW_BUILDERS, "vmware.composite.vm.guest.env.read", _scrubbed)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vmware.composite.vm.guest.env.read"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"vm": "vm-42", "guest_secret_ref": "targets/app-vm"},
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
async def test_permission_preflight_runs_for_credential_class_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission preflight runs even when the op classifies credential-class.

    This is the whole point of the separate hook:
    ``vmware.composite.vm.guest.env.read`` IS ``credential_write`` with no
    bespoke preview builder (so its
    *preview* is suppressed), yet its capability-only *permission* check
    must still run — it carries no secret material, and a credential write
    is exactly the op whose write Vault may deny post-approval. (The
    exemplar is a builderless credential op because ``vault.kv.put`` (#2332)
    and the userpass writes (#4850) now ship bespoke preview builders, so
    their previews are no longer suppressed.)
    """
    ran = False

    async def _preflight(_ctx: PreviewContext) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"check": "vault.capabilities-self", "will_be_denied": True}

    monkeypatch.setitem(_PERMISSION_PREFLIGHTS, "vmware.composite.vm.guest.env.read", _preflight)
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="vmware.composite.vm.guest.env.read"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params={"vm": "vm-42", "password": "s3kret"},
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


# ---------------------------------------------------------------------------
# k8s.secret.read_to_ref preflight (#3844)
# ---------------------------------------------------------------------------

_READ_TO_REF_PARAMS = {"name": "guest-kubeconfig", "namespace": "ns-a", "register_as": "guest-a"}
_READ_TO_REF_PATH = "secret/data/tenants/00000000-0000-0000-0000-00000000a0a0/guest-a"


@pytest.fixture
def _vault_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings the real Vault probe needs (fake client, no network)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _read_to_ref_ctx() -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.secret.read_to_ref"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),
        params=dict(_READ_TO_REF_PARAMS),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_vault_settings_env")
@pytest.mark.parametrize(
    ("granted", "will_be_denied"),
    [(["read"], True), (["create", "update"], False)],
)
async def test_read_to_ref_preflight_probes_the_staged_vault_path(
    monkeypatch: pytest.MonkeyPatch, granted: list[str], will_be_denied: bool
) -> None:
    """The op's preflight probes the Vault path the handler writes to.

    ``k8s.secret.read_to_ref`` is credential-class, so its preview is
    suppressed. The preflight still runs, on the tenant path derived from
    ``register_as``, so a write Vault would refuse shows ``will_be_denied``
    before anyone approves it.
    """
    fake = install_fake_vault(monkeypatch)
    fake.sys.capabilities_by_path = {_READ_TO_REF_PATH: granted}
    ctx = _read_to_ref_ctx()

    assert await build_proposed_effect(ctx) is None
    result = await build_permission_preflight(ctx)

    assert result is not None
    assert result["path"] == _READ_TO_REF_PATH
    assert result["required"] == ["create", "update"]
    assert result["will_be_denied"] is will_be_denied
    assert fake.sys.get_capabilities_calls == [
        {"paths": [_READ_TO_REF_PATH], "token": None, "accessor": None}
    ]
    assert fake.secrets.kv.v2.read_calls == []


@pytest.mark.asyncio
async def test_read_to_ref_preflight_uses_the_handler_vault_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs with ``target=None``, like the handler's write.

    ``ctx.target`` is the Kubernetes cluster. Passing it on would make the
    probe pick a role the real write never uses.
    """
    from meho_backplane.connectors.vault import ops as vault_ops

    seen: dict[str, Any] = {}

    async def _spy(
        operator: Operator, op_id: str, params: dict[str, Any], target: Any = None
    ) -> dict[str, Any]:
        seen.update(op_id=op_id, params=params, target=target)
        return {"will_be_denied": False}

    monkeypatch.setattr(vault_ops, "vault_kv_write_capability_preflight", _spy)

    assert await build_permission_preflight(_read_to_ref_ctx()) == {"will_be_denied": False}
    assert seen == {
        "op_id": "vault.kv.put",
        "params": {
            "mount": "secret",
            "path": "tenants/00000000-0000-0000-0000-00000000a0a0/guest-a",
        },
        "target": None,
    }
