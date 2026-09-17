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

import ast
import asyncio
import inspect
import sys
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

import meho_backplane.operations.typed_register as typed_register
from meho_backplane.broadcast.events import (
    _CREDENTIAL_WRITE_OPS,
    _is_secret_param_name,
    classify_op,
)
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.connectors.vmware_rest.composites import _guest
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.redaction.flight_recorder import classify_body_exclusion

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


def test_rke2_node_write_ops_classify_as_expected() -> None:
    """G-Node/RKE2-T3 #2430 -- the two node-write ops land in the right class.

    ``rke2.node.service.restart`` carries no secret in its params (a single
    allow-listed unit) and classifies plain ``write`` via the new ``.restart``
    write-suffix. ``rke2.node.config.update`` carries a token-bearing
    ``patch`` and is pinned to ``credential_write`` so its broadcast collapses
    to aggregate-only rather than shipping a written join token in full.
    """
    assert classify_op("rke2.node.service.restart") == "write"
    assert classify_op("rke2.node.config.update") == "credential_write"


def test_rke2_node_write_ops_are_registered_and_pinned(
    registered_ops: list[tuple[str, dict[str, Any] | None]],
) -> None:
    """Both node-write ops are in the registered set and none is unpinned-secret."""
    ids = {op_id for op_id, _ in registered_ops}
    assert "rke2.node.service.restart" in ids
    assert "rke2.node.config.update" in ids
    unpinned = {op_id for op_id, _, _ in _unpinned_secret_bearing_ops(registered_ops)}
    assert "rke2.node.config.update" not in unpinned


def _funcs_reaching(module: Any, target: str) -> set[str]:
    """Names of module-level functions that call *target*, directly or transitively.

    The guest login secret is never a declared schema property — it is the
    ephemeral vim ``NamePasswordAuthentication`` block ``_guest_auth`` builds
    — so the schema-walking sweep above cannot see it. Instead of a hardcoded
    op-id list, discover the login-bearing property structurally: a composite
    is login-bearing iff its handler reaches ``_guest_auth`` (``program.run``
    reaches it transitively via ``_start_guest_program``). A new sibling is
    then covered automatically the moment it wires the guest login in.
    """
    tree = ast.parse(inspect.getsource(module))
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if isinstance(fn, ast.Name):
                        names.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        names.add(fn.attr)
            calls[node.name] = names
    reaching: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if name in reaching:
                continue
            if target in callees or (callees & reaching):
                reaching.add(name)
                changed = True
    return reaching


#: The one guest composite that legitimately performs no in-guest login and so
#: sends no credential. It is the hard-coded exception in the set-difference
#: backstop of :func:`_assert_guest_login_composites_body_excluded`, so
#: :func:`test_guest_net_show_is_login_free` pins the invariant directly for the
#: day it ever gains a login.
_GUEST_NO_LOGIN_OP = "vmware.composite.vm.guest.net.show"


def _assert_guest_login_composites_body_excluded() -> None:
    """The drift-guard body: every login-bearing guest composite is body-excluded.

    Factored out of :func:`test_login_bearing_guest_composites_never_record_body`
    so the negative proof
    (:func:`test_drift_guard_bites_on_unpinned_login_bearing_composite`) can trip
    the *same* code path. A red-path test that re-implemented the checks would
    prove nothing about the guard that actually runs in CI.

    Scope of the reachability half (why the boundary is correct today): the walk
    covers exactly the ``_guest`` module (handed to :func:`_funcs_reaching`) and
    the ``group_key == "guest_ops"`` composites. Every op whose secret rides the
    downstream vim ``NamePasswordAuthentication`` request body — rather than an
    op param or a declared schema property, the shapes the classifier-coverage
    sweep and the recorder's pattern nets already catch — lives in that one
    module and that one group. A guest-login composite added to a *different*
    module or group would sit outside this walk; if the guest-ops family ever
    spreads, widen both halves together (the ``group_key`` registry filter and
    the module passed to ``_funcs_reaching``).

    Reads the module-level ``_COMPOSITES`` by global lookup so a test can
    monkeypatch a synthetic entry into the walked registry and watch the guard
    bite (:func:`test_drift_guard_bites_on_unpinned_login_bearing_composite`).
    """
    guest = [s for s in _COMPOSITES if s.group_key == "guest_ops"]
    assert guest, "guest_ops group must be non-empty (registry discovery broke)"
    login_funcs = _funcs_reaching(_guest, "_guest_auth")
    assert login_funcs, "no guest composite resolves _guest_auth (AST walk broke)"
    login_bearing = [s for s in guest if s.handler.__name__ in login_funcs]
    assert {s.op_id for s in guest} - {s.op_id for s in login_bearing} == {_GUEST_NO_LOGIN_OP}
    for spec in login_bearing:
        assert classify_op(spec.op_id) == "credential_write", spec.op_id
        excl = classify_body_exclusion(spec.op_id)
        assert excl.excluded is True and excl.family == "secret-bearing", spec.op_id


def test_login_bearing_guest_composites_never_record_body() -> None:
    """#3717 — every guest-ops composite that logs into the guest is body-excluded.

    The guest OS password rides the downstream vim
    ``NamePasswordAuthentication`` request body (not an op param / declared
    property), so the ONLY control that stops the flight recorder recording
    it is a ``credential_*`` classification via ``_CREDENTIAL_WRITE_OPS``.
    The universe is discovered from the registry and the login-bearing
    property from ``_guest_auth`` reachability, so a future login-bearing
    sibling fails this loop until pinned, and a future no-login sibling fails
    the set-difference until consciously reviewed. ``net.show`` is the sole
    guest composite with no in-guest login.
    """
    _assert_guest_login_composites_body_excluded()


def test_guest_net_show_is_login_free() -> None:
    """net.show — the hard-coded set-difference exception — must stay login-free.

    The set-difference backstop in the guard hard-codes ``net.show`` as the one
    guest composite with no in-guest login. That backstop alone is a weak guard
    for *this* op: if a future change wired a guest login into ``net.show``, the
    difference would shrink to the empty set, and a maintainer chasing the
    failure might "fix" it by editing the expected set rather than re-classifying
    the op. So pin the real invariant directly — its handler must not reach
    ``_guest_auth`` — the one login-bearing op the set-difference cannot catch on
    its own. The day it does, this fails and forces a conscious re-classification
    (pin it into ``_CREDENTIAL_WRITE_OPS``).
    """
    login_funcs = _funcs_reaching(_guest, "_guest_auth")
    assert login_funcs, "no guest composite resolves _guest_auth (AST walk broke)"
    spec = next(s for s in _COMPOSITES if s.op_id == _GUEST_NO_LOGIN_OP)
    # Rename-proof: this is the handler actually registered for net.show, so the
    # reachability assertion below is about the real net.show implementation.
    assert spec.handler.__name__ == "guest_net_show_composite"
    assert spec.handler.__name__ not in login_funcs
    # ... and it is correctly NOT pinned credential-bearing today (no login,
    # no credential to protect); the day it gains a login it must be pinned.
    assert _GUEST_NO_LOGIN_OP not in _CREDENTIAL_WRITE_OPS


def test_drift_guard_bites_on_unpinned_login_bearing_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED-PATH: the guard must FAIL on a login-bearing composite that isn't pinned.

    A green :func:`test_login_bearing_guest_composites_never_record_body` is only
    meaningful if the same check would bite when a guest-login composite is added
    without a ``credential_write`` pin. Plant a synthetic ``guest_ops`` composite
    whose handler reaches ``_guest_auth`` (reuse a real login-bearing handler so
    its ``__name__`` is in the AST-discovered ``login_funcs`` — the walk only
    sees functions defined in the ``_guest`` module source) but whose op_id is
    NOT in ``_CREDENTIAL_WRITE_OPS``, inject it into the walked registry via
    monkeypatch (never left registered), and assert the guard raises.
    """
    synthetic_op = "vmware.composite.vm.guest.__drift_probe__.read"
    # Preconditions: the synthetic op is genuinely unpinned, so a guard with
    # teeth must flag it.
    assert synthetic_op not in _CREDENTIAL_WRITE_OPS
    assert classify_op(synthetic_op) != "credential_write"
    synthetic = SimpleNamespace(
        op_id=synthetic_op,
        # A real login-bearing handler: its name IS in login_funcs, so the guard
        # treats the synthetic op as login-bearing and demands a credential_write
        # classification it does not have.
        handler=_guest.guest_process_list_composite,
        group_key="guest_ops",
    )
    assert synthetic.handler.__name__ in _funcs_reaching(_guest, "_guest_auth")
    # Inject into the registry the guard walks (module-global lookup), restored
    # automatically by monkeypatch so nothing stays registered.
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSITES", (*_COMPOSITES, synthetic))
    with pytest.raises(AssertionError):
        _assert_guest_login_composites_body_excluded()
