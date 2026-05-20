# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.typed_register`.

Coverage matrix (G0.6-T4 / Task #395 acceptance criteria):

* :func:`register_typed_operation` first-call path -- inserts a new
  ``endpoint_descriptor`` row with ``source_kind='typed'``,
  ``handler_ref`` set to the dotted Python path, every field
  populated from the inputs, embedding computed once.
* Idempotency: calling the helper twice with the **same** args
  produces one row + the embedding service is called exactly once
  across both calls (skip-re-embed branch on the second call).
* Body-hash skip semantics:
  - Changing ``summary`` triggers a re-embed.
  - Changing ``description`` triggers a re-embed.
  - Changing ``custom_description`` triggers a re-embed.
  - Changing ``tags`` triggers a re-embed.
  - Changing ``parameter_schema`` does **not** trigger a re-embed.
  - Changing ``response_schema`` does **not** trigger a re-embed.
  - Changing ``safety_level`` does **not** trigger a re-embed.
  - Changing ``requires_approval`` does **not** trigger a re-embed.
  - Changing ``llm_instructions`` does **not** trigger a re-embed.
* ``group_key`` -- resolves to an existing :class:`OperationGroup`
  row when one is present; creates one with
  ``review_status='enabled'`` and ``tenant_id IS NULL`` when absent.
* Invalid inputs raise :class:`ValueError`:
  - Empty / whitespace ``op_id``.
  - ``safety_level`` not in the bounded enum.
* :func:`derive_handler_ref` rejects closures, lambdas, partials,
  and non-coroutine functions with :class:`HandlerRefError` (a
  :class:`ValueError` subclass).
* Module-level async functions resolve to ``module.qualname``;
  bound methods resolve to ``module.Class.method``.
* Embeddings stored on the row are the 384-element ``list[float]``
  returned by the mocked service (which mirrors the production
  :class:`EmbeddingService.encode_one` contract).

The embedding service is mocked via the explicit
``embedding_service=`` parameter so tests don't pull fastembed or
ONNX runtime, and the call count assertion is what proves the
skip-re-embed branch is exercised on idempotent re-calls.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.typed_register import (
    HandlerRefError,
    derive_handler_ref,
    register_typed_operation,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` standing in for :class:`EmbeddingService`.

    Returns a deterministic 384-dim vector for every ``encode_one``
    call so test assertions can compare ``row.embedding`` against the
    known value, and the mock's ``call_count`` proves the
    skip-re-embed branch is being exercised on idempotent calls.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Module-level / class-defined handler fixtures used by handler-ref tests.
# Defined at module scope so the dotted-path derivation has something stable
# to round-trip; closures and lambdas live inside the test bodies that
# exercise the rejection paths.
# ---------------------------------------------------------------------------


async def sample_module_level_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Module-level async handler -- the typical typed-connector op shape."""
    return {"ok": True, "params": params}


class SampleHandlerClass:
    """A class with an async method, used to exercise the bound-method path."""

    async def bound_method_handler(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "params": params}


def sync_handler() -> dict[str, Any]:  # pragma: no cover - rejected before call
    """A sync function -- rejected by ``derive_handler_ref``."""
    return {}


# ---------------------------------------------------------------------------
# derive_handler_ref
# ---------------------------------------------------------------------------


def test_derive_handler_ref_module_level_function() -> None:
    """Module-level async function -> ``module.qualname``."""
    ref = derive_handler_ref(sample_module_level_handler)
    assert ref == ("tests.test_operations_typed_register.sample_module_level_handler")


def test_derive_handler_ref_bound_method() -> None:
    """Bound async method -> ``module.Class.method``."""
    instance = SampleHandlerClass()
    ref = derive_handler_ref(instance.bound_method_handler)
    assert ref == ("tests.test_operations_typed_register.SampleHandlerClass.bound_method_handler")


def test_derive_handler_ref_rejects_lambda() -> None:
    """Lambdas have ``__qualname__ == '<lambda>'`` -- rejected."""
    handler = lambda target, params: {}  # type: ignore[misc]  # noqa: E731 - exercising the rejection path
    with pytest.raises(HandlerRefError, match="lambda"):
        derive_handler_ref(handler)


def test_derive_handler_ref_rejects_closure() -> None:
    """Inner / closure functions have ``<locals>`` in qualname -- rejected."""

    def make_closure() -> Any:
        async def inner(target: Any, params: dict[str, Any]) -> dict[str, Any]:
            return {}

        return inner

    handler = make_closure()
    with pytest.raises(HandlerRefError, match="closure"):
        derive_handler_ref(handler)


def test_derive_handler_ref_rejects_sync_function() -> None:
    """Sync functions are rejected -- typed ops must be ``async def``."""
    with pytest.raises(HandlerRefError, match="async def"):
        derive_handler_ref(sync_handler)  # type: ignore[arg-type]


def test_derive_handler_ref_rejects_functools_partial() -> None:
    """``functools.partial`` wrappers lack ``__qualname__`` -- rejected.

    The wrapped callable's identity lives at ``.func``; the partial
    itself is a callable object with no stable dotted path for the
    dispatcher's ``importlib`` resolution to round-trip.
    """
    partial_handler = functools.partial(sample_module_level_handler)
    with pytest.raises(HandlerRefError):
        derive_handler_ref(partial_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# register_typed_operation -- first-register path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_first_call_inserts_descriptor_with_every_field(
    stub_embedding_service: AsyncMock,
) -> None:
    """First call inserts a new row, every field populated, embedding called once."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=sample_module_level_handler,
        summary="Read a KV v2 secret.",
        description="Read a secret from Vault's KV v2 mount at the given path.",
        parameter_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        response_schema={"type": "object"},
        group_key="kv",
        tags=["read-only", "secrets"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={"when_to_call": "to fetch a stored credential"},
        embedding_service=stub_embedding_service,
    )

    assert stub_embedding_service.encode_one.call_count == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        row = result.scalar_one()

    assert isinstance(row.id, uuid.UUID)
    assert row.tenant_id is None  # built-in / global by construction
    assert row.product == "vault"
    assert row.version == "1.x"
    assert row.impl_id == "vault"
    assert row.op_id == "vault.kv.read"
    assert row.source_kind == "typed"
    assert row.method is None
    assert row.path is None
    assert row.handler_ref == ("tests.test_operations_typed_register.sample_module_level_handler")
    assert row.summary == "Read a KV v2 secret."
    assert row.description.startswith("Read a secret from Vault's KV v2")
    assert row.tags == ["read-only", "secrets"]
    assert row.parameter_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    assert row.response_schema == {"type": "object"}
    assert row.llm_instructions == {"when_to_call": "to fetch a stored credential"}
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert row.is_enabled is True
    assert row.embedding == [0.1] * 384
    assert row.custom_description is None
    assert row.custom_notes is None
    assert row.created_at is not None
    assert row.updated_at is not None

    # Group resolved to an existing row OR newly created with the expected shape.
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(OperationGroup).where(OperationGroup.id == row.group_id)
        )
        group = result.scalar_one()
    assert group.tenant_id is None
    assert group.product == "vault"
    assert group.version == "1.x"
    assert group.impl_id == "vault"
    assert group.group_key == "kv"
    assert group.review_status == "enabled"


@pytest.mark.asyncio
async def test_register_first_call_without_group_leaves_group_id_null(
    stub_embedding_service: AsyncMock,
) -> None:
    """``group_key=None`` leaves ``group_id`` NULL -- an ungrouped op."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.health",
        handler=sample_module_level_handler,
        summary="Vault health probe.",
        description="Hit Vault's /sys/health endpoint.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.health")
        )
        row = result.scalar_one()
    assert row.group_id is None


# ---------------------------------------------------------------------------
# Idempotency -- body-hash skip path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_same_args_twice_skips_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Re-call with identical args -> 1 row, embedding called exactly once.

    The load-bearing assertion of T4: connector init on restart must
    not re-embed every typed op when descriptions are unchanged.
    """
    kwargs: dict[str, Any] = {
        "product": "vault",
        "version": "1.x",
        "impl_id": "vault",
        "op_id": "vault.kv.read",
        "handler": sample_module_level_handler,
        "summary": "Read a KV v2 secret.",
        "description": "Read a secret from Vault's KV v2 mount.",
        "parameter_schema": {"type": "object"},
        "group_key": "kv",
        "tags": ["read-only"],
        "embedding_service": stub_embedding_service,
    }
    await register_typed_operation(**kwargs)
    assert stub_embedding_service.encode_one.call_count == 1

    await register_typed_operation(**kwargs)
    assert stub_embedding_service.encode_one.call_count == 1, (
        "Embedding service must not be invoked when the embedding text is unchanged"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        rows = result.scalars().all()
    assert len(rows) == 1  # exactly one row across both calls


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("summary", "Updated summary blurb."),
        ("description", "Updated description body."),
        ("custom_description", "Operator-curated override."),
        ("tags", ["read-only", "new-tag"]),
    ],
)
@pytest.mark.asyncio
async def test_register_changed_embedding_text_triggers_reembed(
    stub_embedding_service: AsyncMock,
    field: str,
    new_value: Any,
) -> None:
    """Changing any field that feeds the embedding text triggers re-embed.

    The four fields ``summary`` / ``description`` / ``custom_description`` /
    ``tags`` are the embedding-text inputs per the contract; changing any
    of them must force a re-embed.
    """
    baseline: dict[str, Any] = {
        "product": "vault",
        "version": "1.x",
        "impl_id": "vault",
        "op_id": "vault.kv.read",
        "handler": sample_module_level_handler,
        "summary": "Read a KV v2 secret.",
        "description": "Read a secret from Vault's KV v2 mount.",
        "parameter_schema": {"type": "object"},
        "tags": ["read-only"],
        "embedding_service": stub_embedding_service,
    }
    await register_typed_operation(**baseline)
    assert stub_embedding_service.encode_one.call_count == 1

    updated = dict(baseline)
    updated[field] = new_value
    await register_typed_operation(**updated)
    assert stub_embedding_service.encode_one.call_count == 2, (
        f"Changing {field!r} must trigger a re-embed"
    )


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("parameter_schema", {"type": "object", "properties": {"new": {"type": "string"}}}),
        ("response_schema", {"type": "object"}),
        ("safety_level", "caution"),
        ("requires_approval", True),
        ("llm_instructions", {"when_to_call": "carefully"}),
    ],
)
@pytest.mark.asyncio
async def test_register_changed_non_embedding_field_skips_reembed(
    stub_embedding_service: AsyncMock,
    field: str,
    new_value: Any,
) -> None:
    """Changing fields outside the embedding text must NOT trigger re-embed.

    The fields below are stored on the row but do not contribute to the
    embedding text composition -- the dispatcher consumes them but
    retrieval ranking does not. Changing them updates the row in place
    without re-running the ONNX inference.
    """
    baseline: dict[str, Any] = {
        "product": "vault",
        "version": "1.x",
        "impl_id": "vault",
        "op_id": "vault.kv.read",
        "handler": sample_module_level_handler,
        "summary": "Read a KV v2 secret.",
        "description": "Read a secret from Vault's KV v2 mount.",
        "parameter_schema": {"type": "object"},
        "tags": ["read-only"],
        "embedding_service": stub_embedding_service,
    }
    await register_typed_operation(**baseline)
    assert stub_embedding_service.encode_one.call_count == 1

    updated = dict(baseline)
    updated[field] = new_value
    await register_typed_operation(**updated)
    assert stub_embedding_service.encode_one.call_count == 1, (
        f"Changing {field!r} must not trigger a re-embed"
    )

    # The field's new value should be persisted on the row.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        row = result.scalar_one()
    assert getattr(row, field) == new_value


# ---------------------------------------------------------------------------
# Group resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_reuses_existing_operation_group(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """If an :class:`OperationGroup` already exists for the key, reuse its id."""
    pre_existing_id = uuid.uuid4()
    session.add(
        OperationGroup(
            id=pre_existing_id,
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            group_key="kv",
            name="KV secrets",
            when_to_use="Read and write KV v2 secrets.",
            review_status="enabled",
        )
    )
    await session.commit()

    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=sample_module_level_handler,
        summary="Read.",
        description="Read.",
        parameter_schema={"type": "object"},
        group_key="kv",
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        descriptors = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
                )
            )
            .scalars()
            .all()
        )
        # One descriptor pointing at the pre-existing group id.
        assert len(descriptors) == 1
        assert descriptors[0].group_id == pre_existing_id

        # No extra group rows were created.
        groups = (
            (await fresh.execute(select(OperationGroup).where(OperationGroup.group_key == "kv")))
            .scalars()
            .all()
        )
        assert len(groups) == 1
        assert groups[0].id == pre_existing_id


@pytest.mark.asyncio
async def test_register_creates_operation_group_when_absent(
    stub_embedding_service: AsyncMock,
) -> None:
    """Missing :class:`OperationGroup` is auto-created with ``review_status='enabled'``."""
    await register_typed_operation(
        product="kubernetes",
        version="1.32",
        impl_id="kubernetes",
        op_id="k8s.pod.list",
        handler=sample_module_level_handler,
        summary="List pods.",
        description="List every pod in a namespace.",
        parameter_schema={"type": "object"},
        group_key="workloads",
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(OperationGroup).where(OperationGroup.group_key == "workloads")
        )
        group = result.scalar_one()
    assert group.tenant_id is None
    assert group.product == "kubernetes"
    assert group.version == "1.32"
    assert group.impl_id == "kubernetes"
    assert group.review_status == "enabled"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_op_id", ["", "   ", "\t\n"])
@pytest.mark.asyncio
async def test_register_rejects_invalid_op_id(
    stub_embedding_service: AsyncMock,
    bad_op_id: str,
) -> None:
    """Empty or whitespace-only ``op_id`` raises :class:`ValueError`."""
    with pytest.raises(ValueError, match="op_id"):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=bad_op_id,
            handler=sample_module_level_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_rejects_invalid_safety_level(
    stub_embedding_service: AsyncMock,
) -> None:
    """``safety_level`` outside the bounded enum raises :class:`ValueError`."""
    with pytest.raises(ValueError, match="safety_level"):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.read",
            handler=sample_module_level_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            safety_level="extremely-dangerous",  # type: ignore[arg-type]
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_rejects_closure_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """Closure handler is rejected with :class:`HandlerRefError`."""

    def make_inner() -> Any:
        async def inner(target: Any, params: dict[str, Any]) -> dict[str, Any]:
            return {}

        return inner

    bad_handler = make_inner()
    with pytest.raises(HandlerRefError, match="closure"):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.read",
            handler=bad_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Registration-time handler_ref resolvability guard (#697 AC #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_rejects_unreachable_handler_ref_via_import_round_trip(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler whose derived ``handler_ref`` does not import-walk is rejected.

    Fail-closed at registration. Simulates the green-but-hollow case
    that #697 caught after the fact (#367 bind9 ``about`` op closed
    green with a handler unreachable through ``call_operation``):
    after ``derive_handler_ref`` succeeds on the in-memory callable,
    point the handler's ``__module__`` at a non-existent module so the
    dispatcher's :func:`import_handler` walk fails. Without the guard,
    the row would commit and the connector would close green; with
    the guard, registration raises :class:`HandlerRefError` and the
    upsert never runs.
    """
    from meho_backplane.operations import _handler_resolve

    _handler_resolve.reset_handler_cache()

    # Use a module-level handler so ``derive_handler_ref`` (which
    # rejects ``<locals>``-bearing qualnames before any other check)
    # accepts it; then point ``__module__`` at a non-existent module
    # so :func:`import_handler`'s walk fails. That isolates this test
    # to the resolution-time guard, not the closure-rejection path
    # exercised separately above.
    monkeypatch.setattr(
        sample_module_level_handler,
        "__module__",
        "definitely_not_a_real_module_xyzzy",
    )

    with pytest.raises(HandlerRefError, match="handler_unreachable at dispatch"):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.read",
            handler=sample_module_level_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            embedding_service=stub_embedding_service,
        )

    # No row committed: the guard fires before _register_in_session.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_register_accepts_module_level_handler_via_import_round_trip(
    stub_embedding_service: AsyncMock,
) -> None:
    """A real module-level handler imports cleanly and registers.

    Pairs with the rejection test above so the guard's positive path
    is also pinned: ``sample_module_level_handler`` lives in this test
    module, ``derive_handler_ref`` produces the dotted path
    ``tests.test_operations_typed_register.sample_module_level_handler``,
    and :func:`import_handler` round-trips that ref to the same
    callable -- the registration proceeds normally.
    """
    from meho_backplane.operations import _handler_resolve

    _handler_resolve.reset_handler_cache()

    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=sample_module_level_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
            )
        ).scalar_one()
        assert (
            row.handler_ref == "tests.test_operations_typed_register.sample_module_level_handler"
        )


@pytest.mark.asyncio
async def test_register_accepts_bound_method_handler_via_import_round_trip(
    stub_embedding_service: AsyncMock,
) -> None:
    """Bound-method handlers (the typed-connector shape) round-trip too.

    The bind9 / vault / kubernetes connectors all register ops via
    ``register_typed_operation(handler=self.<method>, ...)``. The
    dispatcher's resolver returns the *unbound* function for those
    refs (``getattr(Cls, 'method')`` is unbound in Py3); the guard's
    :func:`import_handler` call MUST accept that same shape so the
    typed-connector pattern keeps working. Pins the binding contract
    for #699 (MRO-aware ``is_unbound_method``).
    """
    from meho_backplane.operations import _handler_resolve

    _handler_resolve.reset_handler_cache()
    instance = SampleHandlerClass()

    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=instance.bound_method_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
            )
        ).scalar_one()
        assert (
            row.handler_ref
            == "tests.test_operations_typed_register.SampleHandlerClass.bound_method_handler"
        )


# ---------------------------------------------------------------------------
# Caller-owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_caller_session_does_not_commit(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """When the caller passes a session, the helper does not commit.

    The caller's rollback discards the upsert. Verified by checking
    that a *fresh* session sees no row until the caller commits.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=sample_module_level_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        session=session,
        embedding_service=stub_embedding_service,
    )

    # Fresh session sees nothing -- caller hasn't committed yet.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        assert result.scalar_one_or_none() is None

    await session.commit()
    async with sessionmaker() as fresh_after:
        result = await fresh_after.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        assert result.scalar_one() is not None


@pytest.mark.asyncio
async def test_register_caller_session_rollback_discards_insert(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Caller rollback discards the upsert -- the helper does not pre-commit."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=sample_module_level_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        session=session,
        embedding_service=stub_embedding_service,
    )

    await session.rollback()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.kv.read")
        )
        assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Bound-method handler -- the typical typed-connector "self.kv_read" shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_bound_method_handler_ref(
    stub_embedding_service: AsyncMock,
) -> None:
    """A bound async method round-trips as ``module.Class.method``."""
    instance = SampleHandlerClass()
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.bound.probe",
        handler=instance.bound_method_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "vault.bound.probe")
        )
        row = result.scalar_one()
    assert row.handler_ref == (
        "tests.test_operations_typed_register.SampleHandlerClass.bound_method_handler"
    )
