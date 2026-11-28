# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.apply`` refuses a document that is too large with its YAML aliases written out.

What these tests prove, through the real dispatch over a stand-in cluster
(made-up names):

* a small manifest whose aliases or merge lists write out past the limit
  is refused with ``invalid_params`` on the preview, on a call that would
  wait and on a call that runs at once; the answer comes fast and with
  little memory, nothing goes to the cluster and no approval request is
  stored;
* a manifest with aliases under the limit parses as YAML reads it;
* the preview parses the manifest once;
* the hiding of the approval card reads a shared mapping once.
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import yaml
from sqlalchemy import func, select

import meho_backplane.operations.dispatcher as dispatcher
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes import _apply_manifest as apply_manifest
from meho_backplane.connectors.kubernetes._apply_manifest import (
    WRITTEN_OUT_LIMIT,
    ApplyManifestError,
    _parse_manifest,
    manifest_size_problem,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, PermissionVerdict
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import _text_entries
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings
from tests.test_connectors_k8s_finalizer_remove import _StubTarget

_OP = "k8s.apply"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000001949")
_TARGET = _StubTarget(tenant_id=_TENANT_ID)

_HEAD = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\ndata:\n"
_TOO_LARGE = f"is larger than {WRITTEN_OUT_LIMIT} characters when its YAML aliases are written out"


def _nested_aliases(levels: int) -> str:
    """Each level is a list that names the level below ten times."""
    lines = ["  l0: &a0 [xxxxxxxxxx]"]
    lines += [f"  l{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, levels)]
    return _HEAD + "\n".join(lines) + "\n"


def _merge_lists(levels: int) -> str:
    """Each level is a map whose merge list names the level below ten times."""
    lines = ["  l0: &m0 {k: v}"]
    lines += [f"  l{i}: &m{i} {{<<: [{', '.join([f'*m{i - 1}'] * 10)}]}}" for i in range(1, levels)]
    return _HEAD + "\n".join(lines) + "\n"


_LARGE: dict[str, str] = {
    "nested-aliases": _nested_aliases(8),
    "merge-lists": _merge_lists(8),
}


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
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def cluster(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Register the ops; any use of the cluster client is a call to this stand-in."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()
    get_client = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", get_client)
    return get_client


def _operator() -> Operator:
    return Operator(
        sub="op-apply-size",
        name="Apply Size Test Operator",
        email=None,
        raw_jwt="op.apply.size.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _call(params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_OP, target=_TARGET, params=params
    )


async def _preview(params: dict[str, Any]) -> dict[str, Any]:
    return await preview_dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_OP, target=_TARGET, params=params
    )


async def _approval_requests() -> int:
    async with get_sessionmaker()() as s:
        query = select(func.count()).select_from(ApprovalRequest)
        return int((await s.execute(query)).scalar_one())


def _runs_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy lets the call run at once, as a grant does."""
    gate = AsyncMock(return_value=(PermissionVerdict.AUTO_EXECUTE, None))
    monkeypatch.setattr(dispatcher, "policy_gate", gate)


async def _fast_and_small(answer: Any) -> Any:
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = await answer
        seconds = time.perf_counter() - started
        _size, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert seconds < 1.0
    assert peak < 50 * 1024 * 1024
    return result


def _assert_refused(errors: list[dict[str, Any]]) -> None:
    assert [e["path"] for e in errors] == ["manifest"]
    assert errors[0]["validator"] == "params_check"
    assert errors[0]["remediation"] == f"The manifest cannot be applied: document 1 {_TOO_LARGE}"


@pytest.mark.parametrize("name", list(_LARGE))
async def test_the_preview_refuses_a_document_too_large_written_out(
    name: str, cluster: AsyncMock
) -> None:
    preview = await _fast_and_small(_preview({"manifest": _LARGE[name]}))

    assert preview["status"] == "error", preview
    assert preview["extras"]["error_code"] == "invalid_params"
    _assert_refused(preview["extras"]["validation_errors"])
    assert await _approval_requests() == 0
    cluster.assert_not_awaited()


@pytest.mark.parametrize("name", list(_LARGE))
async def test_a_call_that_would_wait_refuses_it_and_stores_nothing(
    name: str, cluster: AsyncMock
) -> None:
    result = await _fast_and_small(_call({"manifest": _LARGE[name]}))

    assert result.status == "error", result
    assert result.extras["error_code"] == "invalid_params"
    _assert_refused(result.extras["validation_errors"])
    assert await _approval_requests() == 0
    cluster.assert_not_awaited()


@pytest.mark.parametrize("name", list(_LARGE))
async def test_a_call_that_runs_at_once_refuses_it_and_sends_nothing(
    name: str, cluster: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runs_at_once(monkeypatch)
    for dry_run in ("server", "none"):
        params = {"manifest": _LARGE[name], "dry_run": dry_run}
        result = await _fast_and_small(_call(params))

        assert result.extras["error_code"] == "invalid_params", result
        _assert_refused(result.extras["validation_errors"])
    assert await _approval_requests() == 0
    cluster.assert_not_awaited()


async def test_a_call_that_runs_at_once_reaches_the_cluster_for_a_normal_manifest(
    cluster: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runs_at_once(monkeypatch)

    await _call({"manifest": _nested_aliases(3), "dry_run": "server"})

    cluster.assert_awaited()


@pytest.mark.parametrize("manifest", [_nested_aliases(5), _merge_lists(5)])
def test_a_manifest_with_aliases_under_the_limit_parses_as_yaml_reads_it(manifest: str) -> None:
    assert manifest_size_problem(manifest) is None
    assert _parse_manifest(manifest) == list(yaml.safe_load_all(manifest))


def test_the_document_number_is_named() -> None:
    manifest = _HEAD + "  a: b\n---\n" + _LARGE["merge-lists"]

    problem = manifest_size_problem(manifest)
    with pytest.raises(ApplyManifestError) as raised:
        _parse_manifest(manifest)

    assert problem is not None
    assert problem.text() == f"The manifest cannot be applied: document 2 {_TOO_LARGE}"
    assert str(raised.value) == problem.text()


def test_a_document_that_holds_itself_is_refused() -> None:
    manifest = _HEAD + "  loop: &x [*x]\n"

    with pytest.raises(ApplyManifestError, match=_TOO_LARGE):
        _parse_manifest(manifest)


def test_a_document_just_under_the_limit_parses() -> None:
    manifest = _HEAD + "  big: " + "x" * (WRITTEN_OUT_LIMIT // 2) + "\n"

    assert manifest_size_problem(manifest) is None
    assert _parse_manifest(manifest)[0]["data"]["big"] == "x" * (WRITTEN_OUT_LIMIT // 2)


def test_a_document_over_the_limit_without_aliases_is_refused() -> None:
    manifest = _HEAD + "  big: " + "x" * WRITTEN_OUT_LIMIT + "\n"

    problem = manifest_size_problem(manifest)

    assert problem is not None
    assert _TOO_LARGE in problem.text()


async def test_the_preview_parses_the_manifest_once(cluster: AsyncMock) -> None:
    manifest = _nested_aliases(3)
    with patch.object(
        apply_manifest, "_parse_documents", wraps=apply_manifest._parse_documents
    ) as parse:
        preview = await _preview({"manifest": manifest})

    assert preview["status"] == "ok", preview
    assert parse.call_count == 1


def test_the_card_hiding_reads_a_shared_mapping_once() -> None:
    shared = {"token": "x"}
    docs = [{"a": shared, "b": shared, "c": [shared, shared]}]

    assert _text_entries(docs) == [(shared, "token")]


def test_the_run_gets_its_own_documents() -> None:
    manifest = _nested_aliases(2)

    shown = _parse_manifest(manifest)
    own = _parse_manifest(manifest, shared=False)

    assert own == shown
    assert own[0] is not shown[0]
    assert _parse_manifest(manifest)[0] is shown[0]
