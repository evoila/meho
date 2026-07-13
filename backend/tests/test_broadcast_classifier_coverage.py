# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Classifier-coverage sweep over the registered typed-op set (meho-internal #151).

Whether a secret-bearing write op collapses to aggregate-only on the
broadcast feed depends on a human having pinned it into
``_CREDENTIAL_WRITE_OPS`` / ``_CREDENTIAL_MINT_OPS`` in
``broadcast/events.py``. That allowlist has drifted before
(``vault.kv.put`` shipped classified plain ``write`` and broadcast the
written secret in full until G11.7-T1 #1401 hand-added it), and nothing
failed CI when it did. This module is the enforcement: it enumerates
every op the in-tree connectors register (typed + composite), walks
each op's ``parameter_schema`` for secret-shaped property names, and
fails when a secret-bearing op still classifies to a full-detail class
(``write`` / ``other``).

Enumeration works by stubbing
:func:`~meho_backplane.operations.typed_register.register_typed_operation`
and
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
with capture shims and invoking every queued registrar — the exact set
the FastAPI lifespan runs at boot — so the sweep needs no DB and no
embedding model, and a newly added connector is covered automatically
the moment it queues its registrar.

Honest limitation: the sweep is name-heuristic over declared schema
properties. A secret riding inside a generically-named container
(``vault.kv.put``'s ``data``) is invisible to it — that gap is covered
by the runtime layer
(:func:`~meho_backplane.broadcast.events.scrub_broadcast_params` +
aggregate-collapse in ``publish_broadcast``), which is why both layers
exist.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

import meho_backplane.operations.typed_register as typed_register
from meho_backplane.broadcast.events import _is_secret_param_name, classify_op
from meho_backplane.connectors.registry import _eager_import_connectors

#: Op classes whose broadcast ships full request params by default
#: (decision #3). A secret-bearing registered op landing in one of
#: these is exactly the allowlist-drift this sweep exists to catch.
_FULL_DETAIL_CLASSES = frozenset({"write", "other"})

#: JSON Schema ``type`` values that cannot carry secret material.
#: ``bind_secret_id`` (boolean) / ``secret_id_ttl`` (integer) on the
#: Vault AppRole write surface are configuration attributes *about* a
#: credential, not the credential — flagging them would fail the sweep
#: on vetted ops forever.
_NON_SECRET_JSON_TYPES = frozenset({"boolean", "integer", "number"})


def _capture_registered_ops() -> list[tuple[str, dict[str, Any] | None]]:
    """Run every queued registrar against capture shims; return (op_id, schema).

    The shims bind ``*args/**kwargs`` against the real helpers'
    signatures so positional call sites capture identically to
    keyword ones. Connector modules import the helpers both lazily
    (``from ... import`` inside the registrar body — resolved from
    ``typed_register`` at call time) and at module top (bound once at
    import time), so the swap walks ``sys.modules`` and replaces
    every attribute that *is* one of the real helpers; everything is
    restored in ``finally``.
    """
    captured: list[tuple[str, dict[str, Any] | None]] = []
    real_typed = typed_register.register_typed_operation
    real_composite = typed_register.register_composite_operation
    typed_sig = inspect.signature(real_typed)
    composite_sig = inspect.signature(real_composite)

    async def _capture_typed(*args: Any, **kwargs: Any) -> None:
        bound = typed_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        captured.append((bound.arguments["op_id"], bound.arguments.get("parameter_schema")))

    async def _capture_composite(*args: Any, **kwargs: Any) -> None:
        bound = composite_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        captured.append((bound.arguments["op_id"], bound.arguments.get("parameter_schema")))

    _eager_import_connectors()
    patched: list[tuple[Any, str, Any]] = []
    for module in list(sys.modules.values()):
        for attr_name in list(vars(module) or {}):
            current = getattr(module, attr_name, None)
            if current is real_typed:
                setattr(module, attr_name, _capture_typed)
                patched.append((module, attr_name, real_typed))
            elif current is real_composite:
                setattr(module, attr_name, _capture_composite)
                patched.append((module, attr_name, real_composite))
    try:

        async def _run_all() -> None:
            for registrar in typed_register._TYPED_OP_REGISTRARS:
                await registrar(embedding_service=None)

        asyncio.run(_run_all())
    finally:
        for module, attr_name, original in patched:
            setattr(module, attr_name, original)
    return captured


def _secret_bearing_properties(schema: Any) -> list[str]:
    """Collect secret-shaped property names declared anywhere in *schema*.

    Walks every ``properties`` mapping recursively (nested objects,
    array ``items``, ``oneOf`` arms). A property counts as
    secret-bearing when its name trips
    :func:`~meho_backplane.broadcast.events._is_secret_param_name` —
    the same predicate the runtime scrub uses, so the static and
    runtime layers agree on the vocabulary — unless its declared JSON
    type cannot carry secret material.
    """
    hits: list[str] = []
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                declared = sub.get("type") if isinstance(sub, Mapping) else None
                if _is_secret_param_name(str(name)) and declared not in _NON_SECRET_JSON_TYPES:
                    hits.append(str(name))
                hits.extend(_secret_bearing_properties(sub))
        for key, value in schema.items():
            if key != "properties":
                hits.extend(_secret_bearing_properties(value))
    elif isinstance(schema, list):
        for item in schema:
            hits.extend(_secret_bearing_properties(item))
    return hits


def _unpinned_secret_bearing_ops(
    ops: list[tuple[str, dict[str, Any] | None]],
) -> list[tuple[str, str, list[str]]]:
    """Return (op_id, op_class, secret_props) rows violating the pin rule."""
    violations: list[tuple[str, str, list[str]]] = []
    for op_id, schema in ops:
        op_class = classify_op(op_id)
        if op_class not in _FULL_DETAIL_CLASSES:
            continue
        secret_props = sorted(set(_secret_bearing_properties(schema or {})))
        if secret_props:
            violations.append((op_id, op_class, secret_props))
    return violations


@pytest.fixture(scope="module")
def registered_ops() -> Iterator[list[tuple[str, dict[str, Any] | None]]]:
    yield _capture_registered_ops()


def test_sweep_actually_enumerates_the_registered_set(
    registered_ops: list[tuple[str, dict[str, Any] | None]],
) -> None:
    """Guard against a vacuous pass — the registrar queue must be seen.

    The in-tree connector set registers ~150 ops; a sweep that saw a
    handful means the eager import or the capture shim broke, and the
    coverage test below would pass without covering anything.
    """
    assert len(registered_ops) >= 100
    classes = {classify_op(op_id) for op_id, _ in registered_ops}
    assert "write" in classes
    assert "credential_write" in classes


def test_every_secret_bearing_registered_op_is_pinned_to_a_credential_class(
    registered_ops: list[tuple[str, dict[str, Any] | None]],
) -> None:
    """The classifier-coverage lint: allowlist drift fails CI here.

    A registered op whose parameter schema declares a secret-shaped
    property must classify to a ``credential_*`` class (pinned in
    ``_CREDENTIAL_WRITE_OPS`` / ``_CREDENTIAL_MINT_OPS``) so its
    broadcast collapses to aggregate-only. Fix a failure by pinning
    the op, not by renaming the parameter.
    """
    violations = _unpinned_secret_bearing_ops(registered_ops)
    assert violations == [], (
        "Secret-bearing registered ops broadcasting full params — pin them "
        "into a credential_* allowlist in broadcast/events.py: "
        f"{violations!r}"
    )


def test_deliberately_unpinned_fixture_op_fails_the_sweep() -> None:
    """The sweep must actually bite on an unpinned secret-bearing op."""
    fixture_op: tuple[str, dict[str, Any] | None] = (
        "acme.credstore.create",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "password": {"type": "string"},
            },
        },
    )
    violations = _unpinned_secret_bearing_ops([fixture_op])
    assert violations == [("acme.credstore.create", "write", ["password"])]


def test_rke2_token_rotate_is_pinned_credential_mint() -> None:
    """rke2.token.rotate mints a token server-side -> credential_mint (#2429).

    ``.rotate`` is not a write/read suffix, so without the explicit pin the
    op would classify ``other`` and broadcast full detail. The pin collapses
    its broadcast to aggregate-only (defence-in-depth: the handler already
    never returns the token, but the class must match the semantics).
    """
    assert classify_op("rke2.token.rotate") == "credential_mint"


def test_boolean_and_integer_attrs_do_not_trip_the_sweep() -> None:
    """AppRole-style config attributes stay unflagged (vetted full detail)."""
    fixture_op: tuple[str, dict[str, Any] | None] = (
        "acme.role.write",
        {
            "type": "object",
            "properties": {
                "bind_secret_id": {"type": "boolean"},
                "secret_id_ttl": {"type": "integer"},
            },
        },
    )
    assert _unpinned_secret_bearing_ops([fixture_op]) == []
