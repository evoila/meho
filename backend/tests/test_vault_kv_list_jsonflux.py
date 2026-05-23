# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.3-T4 — JSONFlux result-handle verification for ``vault.kv.list``.

CLAUDE.md postulate 6 / v0.1-spec §4 require that any operation
returning a set larger than the default threshold (~50 rows / 4 KB)
returns a sample + result handle, not the raw list. The G0.6
dispatcher already owns the wrapping seam (it invokes the configured
:class:`~meho_backplane.operations.reducer.Reducer` after the handler
returns and threads the handle onto
:attr:`~meho_backplane.connectors.schemas.OperationResult.handle`).
This module is the single named regression test that pins that
contract for the Vault connector's only set-shaped v0.2 op,
``vault.kv.list``.

Two behaviours are pinned:

* **Default (v0.2): pass-through.** With the production default
  :class:`~meho_backplane.operations.reducer.PassThroughReducer`
  installed, a ``vault.kv.list`` returning a small key set comes back
  as the inline ``{"keys": [...]}`` list with ``handle is None``. This
  is the v0.2 default the Initiative #366 work item 6 specifies
  ("v0.2 default is pass-through").
* **Over-threshold: handle.** With the **real**
  :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
  (G0.6.1-T3 #753) installed via
  :func:`~meho_backplane.operations.dispatcher.set_default_reducer`
  (the same seam the ``*_jsonflux_force_handle.py`` acceptance tests
  exercise for the ingested vendor connectors), a ``vault.kv.list``
  against a path with more than 50 keys returns ``{row_count, total,
  sample, ...}`` on :attr:`OperationResult.result` plus a populated
  :class:`~meho_backplane.connectors.schemas.ResultHandle` carrying the
  full ``total_rows`` count, a JSON-Schema ``schema_``, and a bounded
  ``sample_rows`` slice. The agent never sees the raw >50-key list.

On ``result_query`` / ``result_describe``
=========================================

The ``result_query`` / ``result_aggregate`` / ``result_describe`` /
``result_export`` meta-tools that read a handle back land in a
follow-on Initiative. The DoD's ``result_describe(handle)`` /
``result_query(handle, ...)`` drill-in is therefore verified at the
**contract** level: the handle carries the exact ``total_rows`` (what
``result_describe`` will report) and a non-empty ``sample_rows`` slice
keyed off the same payload ``result_query`` will page through.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult, ResultHandle
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_typed_operations,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

#: JSONFlux default set-shape threshold (v0.1-spec §4: ~50 rows). The
#: real :class:`JsonFluxReducer` keys its set-detection on this exact
#: bound; the constant pins the ">50 keys → handle, ≤50 keys → inline"
#: boundary the spec names so a future default change is caught here.
_THRESHOLD = 50

#: Sample slice the handle carries for the agent's first look —
#: :class:`JsonFluxReducer`'s default ``sample_size``.
_SAMPLE_SIZE = 5


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    """Re-register VaultConnector (v2) + reset the dispatcher caches.

    Mirrors ``test_connectors_vault.py``'s isolation fixture:
    alphabetically earlier suites clear both registry layers via their
    own autouse fixtures, so the v2 entry must be re-established before
    each test for the resolver to find :class:`VaultConnector`. The
    cache reset keeps a stale connector instance from bleeding across
    functions.
    """
    clear_registry()
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars Settings / VaultConnector need (same as the unit suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the Vault typed-op descriptor rows (autouse SQLite is migrated)."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    yield


@pytest.fixture
def threshold_reducer() -> Iterator[None]:
    """Swap the real :class:`JsonFluxReducer` in as the dispatcher default.

    Constructed with its production defaults (``row_threshold=50``,
    ``sample_size=5``) so the regression pins the exact ">50 keys →
    handle, ≤50 keys → inline" boundary against the shipped reducer.
    The reducer is module-level state on
    :mod:`meho_backplane.operations.dispatcher`; teardown restores the
    v0.2 production :class:`PassThroughReducer` so a follow-on test in
    the same session sees the shipped default, and drops the dispatcher
    caches so connector-instance state does not leak.
    """
    set_default_reducer(JsonFluxReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """Request-scoped operator carrying the bearer token the vault
    handlers forward to Vault's JWT/OIDC auth (G0.8-T3 #629). Replaces
    the pre-#224 ``VaultTarget(raw_jwt=...)`` stub.
    """
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_vault(
    op_id: str, params: dict[str, Any], *, jwt: str = "fake.jwt.value"
) -> OperationResult:
    """Dispatch a vault op through the real operator-aware path.

    The dispatcher threads a real :class:`Operator`, resolves the
    connector by ``connector_id``, and ``target`` is ``None`` (vault
    connection params come from settings). The handler reads the JWT
    from ``operator.raw_jwt`` — the #629 contract.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# v0.2 default: pass-through (no handle) for a ≤threshold key set
# ---------------------------------------------------------------------------


async def test_kv_list_under_threshold_returns_inline_list_no_handle(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """≤ threshold keys → inline ``{"keys": [...]}``, ``handle is None``.

    This is the shipped v0.2 default: the dispatcher's
    :class:`PassThroughReducer` returns the handler payload verbatim
    with no :class:`ResultHandle`. Initiative #366 work item 6 fixes
    pass-through as the v0.2 default; this test pins it for the Vault
    connector so a future real-reducer swap can't silently start
    handle-wrapping small lists.
    """
    small_keys = [f"secret-{i}" for i in range(_THRESHOLD)]  # exactly 50 — at, not over
    install_fake_client(monkeypatch, keys=small_keys)

    result = await _dispatch_vault(
        "vault.kv.list",
        {"path": "meho/test"},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error
    assert result.handle is None, (
        f"v0.2 default must not produce a handle for a ≤threshold list; "
        f"got handle={result.handle!r}"
    )
    assert isinstance(result.result, dict)
    assert result.result["keys"] == small_keys, (
        "pass-through must return the full inline key list verbatim"
    )


async def test_kv_list_under_threshold_passes_through_even_with_real_reducer(
    monkeypatch: pytest.MonkeyPatch,
    threshold_reducer: None,
    _registered_vault_typed_ops: None,
) -> None:
    """A threshold-aware reducer still passes a ≤threshold list through.

    Even with the (test-only) real-shaped reducer installed, a key set
    at the threshold boundary (exactly 50) stays inline with no handle
    — the spec boundary is *> 50*, not *>= 50*. Pins the off-by-one so
    the eventual production reducer can't regress the boundary.
    """
    boundary_keys = [f"secret-{i}" for i in range(_THRESHOLD)]  # exactly 50
    install_fake_client(monkeypatch, keys=boundary_keys)

    result = await _dispatch_vault(
        "vault.kv.list",
        {"path": "meho/test"},
    )

    assert result.status == "ok", result.error
    assert result.handle is None, (
        f"a list of exactly {_THRESHOLD} keys is at — not over — the "
        f"threshold; expected pass-through, got handle={result.handle!r}"
    )
    assert isinstance(result.result, dict)
    assert result.result["keys"] == boundary_keys


# ---------------------------------------------------------------------------
# Force-mode / over-threshold: {sample, handle}
# ---------------------------------------------------------------------------


async def test_kv_list_over_threshold_returns_sample_and_handle(
    monkeypatch: pytest.MonkeyPatch,
    threshold_reducer: None,
    _registered_vault_typed_ops: None,
) -> None:
    """> 50 keys → ``{sample, ...}`` on result + a populated handle.

    Exercises the JSONFlux dispatcher → reducer → ``OperationResult``
    seam for ``vault.kv.list``:

    * ``status == 'ok'`` — dispatch succeeded through the typed branch.
    * :attr:`OperationResult.handle` is a :class:`ResultHandle` — the
      reducer's handle flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` equals the full key count — this is what a
      future ``result_describe(handle)`` reports.
    * ``handle.sample_rows`` is a bounded non-empty slice — the seed a
      future ``result_query(handle, ...)`` pages from.
    * :attr:`OperationResult.result` carries the reduced summary
      (``sample`` + ``total``), **not** the raw >50-key list — the
      agent never sees the full set inline (DoD: "no agent-visible raw
      list larger than threshold for this op").
    """
    big_keys = [f"secret-{i}" for i in range(_THRESHOLD + 75)]  # 125 keys, well over
    install_fake_client(monkeypatch, keys=big_keys)

    result = await _dispatch_vault(
        "vault.kv.list",
        {"path": "meho/test"},
        jwt="op-jwt",
    )

    assert result.status == "ok", result.error

    handle = result.handle
    assert handle is not None, (
        "expected OperationResult.handle to be populated for a >threshold "
        f"key set; got handle=None on result={result!r}"
    )
    assert isinstance(handle, ResultHandle)
    # total_rows is the contract a future result_describe(handle) reads.
    assert handle.total_rows == len(big_keys), (
        f"handle.total_rows must report the full key count; "
        f"got {handle.total_rows}, expected {len(big_keys)}"
    )
    assert handle.summary_md and str(len(big_keys)) in handle.summary_md, (
        f"summary_md must mention the full row count; got {handle.summary_md!r}"
    )
    # ResultHandle._freeze_nested wraps schema_ in a MappingProxyType
    # (the model's frozen-after-construction guarantee), so it is a
    # Mapping, not a plain dict — assert the real array-of-objects shape
    # the reducer inferred from the DuckDB-materialized table. The Vault
    # key list is a list of scalars; the reducer normalizes each key into
    # a one-column ``{"value": <key>}`` row, so the inferred schema has a
    # single ``value`` column.
    assert isinstance(handle.schema_, Mapping) and handle.schema_, (
        f"handle.schema_ must be a non-empty mapping; got {handle.schema_!r}"
    )
    assert handle.schema_["type"] == "array"
    assert "value" in handle.schema_["items"]["properties"], (
        f"expected the normalized 'value' column in the inferred schema; "
        f"got {handle.schema_['items']['properties']!r}"
    )
    # sample_rows is the slice a future result_query(handle, ...) pages
    # from — bounded, non-empty, strictly smaller than the full set, and
    # carrying the normalized ``{"value": <key>}`` rows.
    assert handle.sample_rows is not None
    assert 0 < len(handle.sample_rows) <= _SAMPLE_SIZE < len(big_keys), (
        f"sample_rows must be a bounded non-empty slice smaller than the "
        f"full set; got {len(handle.sample_rows)} rows"
    )
    assert [row["value"] for row in handle.sample_rows] == big_keys[:_SAMPLE_SIZE], (
        f"sample_rows must carry the first {_SAMPLE_SIZE} real keys; "
        f"got {[dict(row) for row in handle.sample_rows]!r}"
    )

    # The agent-visible inlined result is the reducer's summary, NOT the
    # raw 125-key list. This is the load-bearing JSONFlux guarantee
    # (CLAUDE.md postulate 6): no 4 MB / >50-row raw payload reaches an
    # agent.
    assert isinstance(result.result, dict)
    assert result.result.get("total") == len(big_keys)
    assert result.result.get("row_count") == len(big_keys)
    assert [row["value"] for row in result.result.get("sample", [])] == big_keys[:_SAMPLE_SIZE]
    assert "keys" not in result.result, (
        "the raw inline key list must not survive on the result envelope once a handle is produced"
    )
    inlined_rows = result.result.get("sample", [])
    assert len(inlined_rows) <= _THRESHOLD, (
        f"no agent-visible list larger than the threshold may be inlined; "
        f"got {len(inlined_rows)} rows on the result envelope"
    )
