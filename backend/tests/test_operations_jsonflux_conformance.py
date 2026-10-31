# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry conformance for JSONFlux verdict preservation (#3425)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

# A result schema qualifies when it explicitly describes both a top-level
# collection and a caller-visible outcome. A lone ``status`` is not enough: it
# can be an opaque vendor state. One recognised Boolean verdict is unambiguous;
# otherwise two outcome fields form the outcome bundle a caller must retain beside
# the spilled detail.
_VERDICT_FIELD_NAMES = frozenset(
    {
        "handshake",
        "reachable",
        "reason",
        "success",
        "ok",
        "healthy",
        "status",
        "state",
        "resultStatus",
        "executionStatus",
    }
)
_UNAMBIGUOUS_BOOLEAN_VERDICT_FIELD_NAMES = frozenset(
    {"handshake", "reachable", "success", "ok", "healthy"}
)


def _schema_type_includes(schema: dict[str, Any], expected: str) -> bool:
    value = schema.get("type")
    return value == expected or (isinstance(value, list) and expected in value)


def _required_result_scalars(schema: dict[str, Any] | None) -> set[str]:
    """Return outcome fields that must survive an explicitly shaped spill.

    Schemas using only ``additionalProperties`` do not describe enough shape
    for this static conformance rule.  Connector authors can still opt into
    ``result_scalars`` for those dynamic vendor payloads.
    """
    if not isinstance(schema, dict) or not _schema_type_includes(schema, "object"):
        return set()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    has_collection = any(
        isinstance(definition, dict) and _schema_type_includes(definition, "array")
        for definition in properties.values()
    )
    if not has_collection:
        return set()
    verdict_fields = set(properties).intersection(_VERDICT_FIELD_NAMES)
    has_unambiguous_boolean_verdict = any(
        field in _UNAMBIGUOUS_BOOLEAN_VERDICT_FIELD_NAMES
        and isinstance(properties[field], dict)
        and _schema_type_includes(properties[field], "boolean")
        for field in verdict_fields
    )
    return verdict_fields if has_unambiguous_boolean_verdict or len(verdict_fields) >= 2 else set()


def _missing_verdict_scalar_hints(descriptors: list[EndpointDescriptor]) -> dict[str, list[str]]:
    """List qualifying typed descriptors missing declared outcome scalars."""
    missing: dict[str, list[str]] = {}
    for descriptor in descriptors:
        required = _required_result_scalars(descriptor.response_schema)
        if not required:
            continue
        instructions = descriptor.llm_instructions
        hint = instructions.get("result_scalars") if isinstance(instructions, dict) else None
        keys = hint.get("keys") if isinstance(hint, dict) else None
        declared = (
            {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else set()
        )
        absent = sorted(required - declared)
        if absent:
            missing[descriptor.op_id] = absent
    return missing


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the settings the real registrar path reads during the sweep."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_typed_verdict_plus_collection_descriptors_preserve_verdict_scalars(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every registered typed result schema preserves its declared outcome."""
    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptors = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.source_kind == "typed")
                )
            )
            .scalars()
            .all()
        )

    assert descriptors, "typed registrar sweep produced no descriptors"
    missing = _missing_verdict_scalar_hints(descriptors)
    assert not missing, (
        "typed verdict-plus-collection descriptors must declare "
        "llm_instructions.result_scalars.keys; missing: "
        f"{missing}"
    )


def test_verdict_plus_collection_conformance_rejects_new_unhinted_descriptor() -> None:
    """A future typed registrar cannot add a qualifying shape without hints."""
    descriptor = type("Descriptor", (), {})()
    descriptor.op_id = "example.health.inspect"
    descriptor.response_schema = {
        "type": "object",
        "properties": {
            "reachable": {"type": "boolean"},
            "status": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "checks": {"type": "array", "items": {"type": "object"}},
        },
    }
    descriptor.llm_instructions = {}

    assert _missing_verdict_scalar_hints([descriptor]) == {
        "example.health.inspect": ["reachable", "reason", "status"]
    }


@pytest.mark.parametrize(
    ("op_id", "verdict", "collection"),
    [
        ("example.delete.report", "success", "rows"),
        ("example.probe.report", "reachable", "checks"),
    ],
)
def test_single_unambiguous_boolean_verdict_with_collection_requires_a_hint(
    op_id: str, verdict: str, collection: str
) -> None:
    """A future registrar cannot evade the rule with one Boolean verdict field."""
    descriptor = type("Descriptor", (), {})()
    descriptor.op_id = op_id
    descriptor.response_schema = {
        "type": "object",
        "properties": {
            verdict: {"type": "boolean"},
            collection: {"type": "array", "items": {"type": "object"}},
        },
    }
    descriptor.llm_instructions = {}

    assert _missing_verdict_scalar_hints([descriptor]) == {op_id: [verdict]}


# ---------------------------------------------------------------------------
# Scalar siblings of a reduced list survive reduction (#3549 / #3633)
# ---------------------------------------------------------------------------

_SAMPLE_SCALARS: dict[str, Any] = {
    "string": "sample",
    "integer": 7,
    "number": 1.5,
    "boolean": True,
}


#: Credential-class ops whose scalar siblings are deliberately NOT kept inline
#: when their list spills. Each entry needs a reason. Keep this list short: a
#: new op belongs here only if its scalars are secret-shaped.
_SCALARS_HELD_BACK: dict[str, str] = {
    "vault.token.create": "client_token and accessor are secret-shaped (credential_mint).",
}


def _first_scalar_type(definition: Any) -> str | None:
    """The first JSON scalar type a property schema allows, or ``None``."""
    if not isinstance(definition, dict):
        return None
    declared = definition.get("type")
    types = declared if isinstance(declared, list) else [declared]
    return next((kind for kind in types if kind in _SAMPLE_SCALARS), None)


def _list_plus_scalars_payload(schema: Any) -> tuple[dict[str, Any], set[str]] | None:
    """Build an over-threshold payload for a "one list + scalar siblings" schema.

    Returns the payload and the scalar sibling names a caller must still see
    inline after reduction, or ``None`` when the schema is not that shape:
    no explicit properties, not exactly one array property, or no scalar
    property. The reducer's own bookkeeping keys (``total`` and friends) are
    left out of the expected set; the reducer owns those.
    """
    from meho_backplane.operations import jsonflux_reducer as reducer_module

    if not isinstance(schema, dict) or not _schema_type_includes(schema, "object"):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    arrays = [
        key
        for key, definition in properties.items()
        if isinstance(definition, dict) and _schema_type_includes(definition, "array")
    ]
    if len(arrays) != 1:
        return None
    payload: dict[str, Any] = {arrays[0]: [{"i": i} for i in range(60)]}
    expected: set[str] = set()
    for key, definition in properties.items():
        kind = _first_scalar_type(definition)
        if kind is None:
            continue
        payload[key] = _SAMPLE_SCALARS[kind]
        if key not in reducer_module._RESERVED_SUMMARY_KEYS:
            expected.add(key)
    return (payload, expected) if expected else None


async def _dropped_scalar_siblings(descriptors: list[Any]) -> dict[str, list[str]]:
    """Reduce a synthetic payload per qualifying descriptor; list lost scalars.

    The reducer context is built from the descriptor exactly as the
    dispatcher builds it, so an op's ``result_scalars`` hint is honoured.
    """
    from meho_backplane.operations.dispatcher import (
        _result_digest_from_descriptor,
        _result_objects_from_descriptor,
        _result_scalars_from_descriptor,
    )
    from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

    reducer = JsonFluxReducer()
    dropped: dict[str, list[str]] = {}
    for descriptor in descriptors:
        built = _list_plus_scalars_payload(descriptor.response_schema)
        if built is None:
            continue
        payload, expected = built
        context: dict[str, Any] = {"op_id": descriptor.op_id}
        for key, hint in (
            ("result_scalars", _result_scalars_from_descriptor(descriptor)),
            ("result_objects", _result_objects_from_descriptor(descriptor)),
            ("result_digest", _result_digest_from_descriptor(descriptor)),
        ):
            if hint is not None:
                context[key] = hint
        summary, handle = await reducer.reduce(payload, descriptor.response_schema, context)
        assert handle is not None, f"{descriptor.op_id}: the synthetic payload did not reduce"
        missing = sorted(key for key in expected if summary.get(key) != payload[key])
        if missing:
            dropped[descriptor.op_id] = missing
    return dropped


@pytest.mark.asyncio
async def test_typed_list_plus_scalar_ops_keep_their_scalars_on_reduction(
    stub_embedding_service: AsyncMock,
) -> None:
    """#3549: no registered typed list+scalars op loses a scalar when it spills.

    Non-credential ops are covered by the reducer's default scalar rule.
    A credential-class op must list its scalars in ``result_scalars`` — the
    default rule never copies a credential-class op's values.
    """
    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptors = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.source_kind == "typed")
                )
            )
            .scalars()
            .all()
        )

    qualifying = [d for d in descriptors if _list_plus_scalars_payload(d.response_schema)]
    assert qualifying, "no typed list+scalars descriptor found; the sweep is not checking anything"
    assert any(d.op_id == "linux.script.run" for d in qualifying)
    qualifying_ids = {d.op_id for d in qualifying}
    stale = sorted(set(_SCALARS_HELD_BACK) - qualifying_ids)
    assert not stale, f"remove stale _SCALARS_HELD_BACK entries: {stale}"
    checked = [d for d in qualifying if d.op_id not in _SCALARS_HELD_BACK]
    dropped = await _dropped_scalar_siblings(checked)
    assert not dropped, (
        "typed list+scalars ops drop scalar siblings when the list spills; a "
        "credential-class op must declare llm_instructions.result_scalars.keys. "
        f"Dropped: {dropped}"
    )


@pytest.mark.asyncio
async def test_list_plus_scalars_sweep_catches_an_unhinted_credential_op() -> None:
    """The sweep fails for a credential-class op that relies on the default rule."""
    descriptor = type("Descriptor", (), {})()
    descriptor.op_id = "vault.kv.list"
    descriptor.response_schema = {
        "type": "object",
        "properties": {"keys": {"type": "array"}, "mount": {"type": "string"}},
    }
    descriptor.llm_instructions = {}

    assert await _dropped_scalar_siblings([descriptor]) == {"vault.kv.list": ["mount"]}


# ---------------------------------------------------------------------------
# Static pagination hints and source coverage (#3676)
# ---------------------------------------------------------------------------


class _ValkeyDouble:
    """Minimal async Valkey double so the real result store can spill."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


async def _reduce_issue_page(context_extra: dict[str, Any]) -> tuple[Any, _ValkeyDouble]:
    """Reduce one 60-row page of GitHub issues through the real store."""
    from meho_backplane.connectors.github.pagination_hints import GH_ISSUE_LIST_OP_ID
    from meho_backplane.connectors.result_handle_store import ResultHandleStore
    from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

    valkey = _ValkeyDouble()
    store = ResultHandleStore(valkey)  # type: ignore[arg-type]
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    page = [{"number": i, "title": f"issue {i}", "state": "open"} for i in range(60)]
    context: dict[str, Any] = {
        "op_id": GH_ISSUE_LIST_OP_ID,
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
        **context_extra,
    }
    _summary, handle = await reducer.reduce(page, None, context)
    assert handle is not None, "a 60-row page must reduce to a handle"
    return handle, valkey


@pytest.mark.asyncio
async def test_github_issue_list_hint_surfaces_native_pagination() -> None:
    """With the curated hint, the agent is told how to fetch the next page."""
    from meho_backplane.connectors.github.pagination_hints import GH_ISSUE_LIST_PAGINATION_HINT

    handle, _valkey = await _reduce_issue_page({"pagination_hint": GH_ISSUE_LIST_PAGINATION_HINT})

    native = handle.fetch_more.native_pagination
    assert native.available is True
    assert native.params is not None
    assert {"per_page", "page"} <= set(native.params)
    assert native.example_next_call is not None
    assert native.example_next_call["args"]["params"]["page"] == 2


def test_github_issue_list_hint_example_is_a_complete_next_call() -> None:
    """The example names every required call_operation arg and says what it assumes."""
    from meho_backplane.connectors.github.pagination_hints import (
        GH_ISSUE_LIST_OP_ID,
        GH_ISSUE_LIST_PAGINATION_HINT,
    )
    from meho_backplane.connectors.schemas import PaginationHint

    hint = PaginationHint.model_validate(GH_ISSUE_LIST_PAGINATION_HINT)
    example = hint.example_next_call
    assert example["tool"] == "call_operation"
    args = example["args"]
    assert {"connector_id", "op_id", "params"} <= set(args)
    assert args["connector_id"].startswith("<")
    assert args["op_id"] == GH_ISSUE_LIST_OP_ID

    # The example assumes per_page=100 and state=open. The text must say so,
    # and must tell the caller to keep their own values and change only page.
    params = args["params"]
    assumes = example["assumes"]
    assert f"per_page={params['per_page']}" in assumes
    assert f"state={params['state']}" in assumes
    assert "change only page" in assumes
    assert "add 1 to page only" in hint.params["page"]
    assert "same value on every page" in hint.params["per_page"]


@pytest.mark.asyncio
async def test_list_without_hint_reports_native_pagination_unavailable() -> None:
    """Without a hint the envelope keeps the curated unavailable rationale."""
    from meho_backplane.operations.jsonflux_reducer import (
        _NATIVE_PAGINATION_UNAVAILABLE_RATIONALE,
    )

    handle, _valkey = await _reduce_issue_page({})

    native = handle.fetch_more.native_pagination
    assert native.available is False
    assert native.rationale == _NATIVE_PAGINATION_UNAVAILABLE_RATIONALE


@pytest.mark.asyncio
async def test_reduced_page_is_never_labeled_source_complete() -> None:
    """A hint says how to page; it does not make the stored page source-complete."""
    import msgspec

    from meho_backplane.connectors.github.pagination_hints import GH_ISSUE_LIST_PAGINATION_HINT
    from meho_backplane.connectors.result_handle_store import _StoredPayload

    handle, valkey = await _reduce_issue_page({"pagination_hint": GH_ISSUE_LIST_PAGINATION_HINT})

    (raw,) = valkey.values.values()
    stored = msgspec.json.decode(raw, type=_StoredPayload)
    assert stored.storage_coverage == "complete"
    assert stored.source_coverage == "unknown"
    rationale = handle.fetch_more.drill_in.rationale
    assert handle.fetch_more.drill_in.available is True
    assert "full result set" not in rationale
    assert "may hold more rows" in rationale


# ---------------------------------------------------------------------------
# Objects and lists next to a reduced list survive reduction (#5139)
# ---------------------------------------------------------------------------

#: Ops whose parts are deliberately NOT kept when their list spills. Each
#: entry needs a reason. Keep this list short: a new op belongs here only if
#: it is credential-class and its parts are secret-shaped.
_PARTS_HELD_BACK: dict[str, str] = {
    "fleet.locker.certificate.import": (
        "credential_write: the request carries the private key; the answer is one certificate."
    ),
    "sddc.credential.list": "credential_read: the page lists stored passwords.",
    "sddc.host.commission": "credential_write: the request carries host passwords.",
    "vmware.composite.vm.guest.file.read": (
        "credential_write: it reads guest files, which can hold secrets."
    ),
}

_SAMPLE_PART_OBJECT: dict[str, Any] = {"note": "sample"}


def _rows_plus_parts_payload(
    schema: Any, digest_hint: Any
) -> tuple[dict[str, Any], set[str]] | None:
    """Build an over-threshold payload for a "rows plus parts" schema.

    Every declared array gets 60 rows, every declared object a small value,
    every declared scalar a sample. Returns the payload and its parts (the
    objects and lists next to the rows the reducer picks), or ``None`` when
    the schema is not that shape. The rows key is picked with the reducer's
    own rules, so the sweep checks exactly the answers that reduce.
    """
    from meho_backplane.operations import jsonflux_reducer as reducer_module

    if not isinstance(schema, dict) or not _schema_type_includes(schema, "object"):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    payload: dict[str, Any] = {}
    for key, definition in properties.items():
        if not isinstance(definition, dict):
            continue
        if _schema_type_includes(definition, "array"):
            payload[key] = [{"i": i} for i in range(60)]
        elif _schema_type_includes(definition, "object"):
            payload[key] = dict(_SAMPLE_PART_OBJECT)
        elif (kind := _first_scalar_type(definition)) is not None:
            payload[key] = _SAMPLE_SCALARS[kind]
    spec = reducer_module._resolve_digest_hint(
        {"result_digest": digest_hint} if digest_hint is not None else None
    )
    rows_key, rows = reducer_module._detect_collection(payload, spec)
    if rows is None or rows_key is None:
        return None
    parts = {
        key for key, value in payload.items() if key != rows_key and isinstance(value, dict | list)
    }
    return (payload, parts) if parts else None


async def _lost_parts(descriptors: list[Any]) -> dict[str, list[str]]:
    """Reduce a synthetic payload per qualifying descriptor; list lost parts.

    A part is kept when it rides inline whole, or when a ``path`` read on
    the handle returns it whole. The reducer context is built from the
    descriptor exactly as the dispatcher builds it.
    """
    from meho_backplane.connectors.result_handle_store import (
        ResultHandleStore,
        ResultRecordKindError,
    )
    from meho_backplane.operations.dispatcher import (
        _result_digest_from_descriptor,
        _result_objects_from_descriptor,
        _result_scalars_from_descriptor,
    )
    from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

    tenant_id = "00000000-0000-0000-0000-00000000c0c0"
    store = ResultHandleStore(_ValkeyDouble())  # type: ignore[arg-type]
    reducer = JsonFluxReducer(store=store)
    lost: dict[str, list[str]] = {}
    for descriptor in descriptors:
        digest_hint = _result_digest_from_descriptor(descriptor)
        built = _rows_plus_parts_payload(descriptor.response_schema, digest_hint)
        if built is None:
            continue
        payload, parts = built
        context: dict[str, Any] = {
            "op_id": descriptor.op_id,
            "operator_sub": "op-sweep",
            "tenant_id": tenant_id,
        }
        for key, hint in (
            ("result_scalars", _result_scalars_from_descriptor(descriptor)),
            ("result_objects", _result_objects_from_descriptor(descriptor)),
            ("result_digest", digest_hint),
        ):
            if hint is not None:
                context[key] = hint
        summary, handle = await reducer.reduce(payload, descriptor.response_schema, context)
        assert handle is not None, f"{descriptor.op_id}: the synthetic payload did not reduce"
        try:
            stored = await store.fetch_document(
                tenant_id=uuid.UUID(tenant_id), operator_sub="op-sweep", handle_id=handle.handle_id
            )
        except ResultRecordKindError:
            stored = None
        document = stored.document if stored is not None else {}
        missing = sorted(
            key
            for key in parts
            if summary.get(key) != payload[key] and document.get(key) != payload[key]
        )
        if missing:
            lost[descriptor.op_id] = missing
    return lost


@pytest.mark.asyncio
async def test_registered_rows_plus_parts_ops_keep_their_parts_on_reduction(
    stub_embedding_service: AsyncMock,
) -> None:
    """#5139: no registered typed or multi-step op loses a part when it spills."""
    from meho_backplane.operations.dispatcher import _result_digest_from_descriptor

    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptors = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.source_kind.in_(("typed", "composite"))
                    )
                )
            )
            .scalars()
            .all()
        )

    qualifying = [
        d
        for d in descriptors
        if _rows_plus_parts_payload(d.response_schema, _result_digest_from_descriptor(d))
    ]
    assert len(qualifying) >= 50, "the sweep found too few rows-plus-parts ops to check"
    assert any(d.source_kind == "composite" for d in qualifying)
    qualifying_ids = {d.op_id for d in qualifying}
    stale = sorted(set(_PARTS_HELD_BACK) - qualifying_ids)
    assert not stale, f"remove stale _PARTS_HELD_BACK entries: {stale}"
    lost = await _lost_parts([d for d in qualifying if d.op_id not in _PARTS_HELD_BACK])
    assert not lost, f"rows-plus-parts ops lose parts when the list spills: {lost}"


@pytest.mark.asyncio
async def test_rows_plus_parts_sweep_catches_a_lost_part() -> None:
    """The sweep fails for a credential-class op, whose parts are never kept."""
    descriptor = type("Descriptor", (), {})()
    descriptor.op_id = "vault.kv.list"
    descriptor.response_schema = {
        "type": "object",
        "properties": {"keys": {"type": "array"}, "metadata": {"type": "object"}},
    }
    descriptor.llm_instructions = {}

    assert await _lost_parts([descriptor]) == {"vault.kv.list": ["metadata"]}
