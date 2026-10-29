# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard: an operation with a secret-named field keeps its bodies out of traces.

The flight recorder stores no request or answer body for an operation it knows
sends a secret: a credential operation of ``classify_op``, an operation with a
secret tag word (for example ``credential``), a secret op-id pattern, or a
delete (``classify_body_exclusion``). Someone has to mark each such operation.
This test finds the ones nobody marked.

It runs every queued registrar against capture shims, the same way as
``test_broadcast_classifier_coverage.py``, so it sees every typed and
multi-step operation of the in-tree connectors without a database. It looks at
field names only. An operation is flagged when its parameter schema has:

* a field named like a password, secret, key or token, at any depth or as a
  key of an example call. The name check is the shared
  ``is_secret_param_name``, the one the broadcast scrub uses;
* a field whose name ends in ``spec`` or ``specs``: a vendor spec that MEHO
  sends as it is and that can hold passwords (a domain spec, an SddcSpec).

A flagged operation must keep its bodies out of traces. If it sends no secret,
it goes on :data:`_EXEMPT` with a one-line reason. Fix a failure by marking the
operation (the tag ``credential`` is the usual way), not by renaming the field.

Not checked: ingested operations (``METHOD:/path``). They come from a vendor
API file at run time, not from a registrar.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple

import pytest

import meho_backplane.operations.typed_register as typed_register
from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings


class _Op(NamedTuple):
    """One registered operation: its id, parameter schema and tags."""

    op_id: str
    parameter_schema: Any
    tags: tuple[str, ...]


#: JSON Schema types that cannot hold a secret (``bind_secret_id`` is a flag).
_NON_SECRET_JSON_TYPES = frozenset({"boolean", "integer", "number"})

#: The last word of a field name that marks a vendor spec.
_SPEC_WORDS = frozenset({"spec", "specs"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Operations with a flagged field that send no secret. One line each.
_EXEMPT: dict[str, str] = {
    "argocd.app.set": "An Argo CD app spec names a Git source and a cluster; no login in it.",
    "k8s.deployment.list": "continue_token is the paging cursor of the list, not a secret.",
    "k8s.job.list": "continue_token is the paging cursor of the list, not a secret.",
    "k8s.workload.list": "continue_token is the paging cursor of the list, not a secret.",
    "k8s.pod.list": "continue_token is the paging cursor of the list, not a secret.",
    "sddc.network_pool.create": "A network pool spec holds VLANs, subnets and IP ranges only.",
    "vmware.composite.namespace.create": "Its specs name storage policies, libraries, VM classes.",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings reads (the delete patterns come from Settings)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _capture_shim(real: Callable[..., Any], captured: list[_Op]) -> Callable[..., Any]:
    """A stand-in for a register helper that only records the operation."""
    signature = inspect.signature(real)

    async def _capture(*args: Any, **kwargs: Any) -> None:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        tags = tuple(str(tag) for tag in arguments.get("tags") or ())
        captured.append(_Op(arguments["op_id"], arguments.get("parameter_schema"), tags))

    return _capture


def _registered_ops() -> list[_Op]:
    """Run every queued registrar against capture shims; return each registered op.

    Connector modules import the register helpers at module top and inside
    the registrar, so the swap walks ``sys.modules`` and replaces every
    attribute that is one of the real helpers. Everything is put back after.
    """
    captured: list[_Op] = []
    real_typed = typed_register.register_typed_operation
    real_composite = typed_register.register_composite_operation
    typed_shim = _capture_shim(real_typed, captured)
    composite_shim = _capture_shim(real_composite, captured)

    _eager_import_connectors()
    patched: list[tuple[Any, str, Any]] = []
    for module in list(sys.modules.values()):
        for name in list(vars(module) or {}):
            current = getattr(module, name, None)
            if current is real_typed:
                setattr(module, name, typed_shim)
                patched.append((module, name, real_typed))
            elif current is real_composite:
                setattr(module, name, composite_shim)
                patched.append((module, name, real_composite))
    try:

        async def _run_all() -> None:
            for registrar in typed_register._TYPED_OP_REGISTRARS:
                await registrar(embedding_service=None)

        asyncio.run(_run_all())
    finally:
        for module, name, original in patched:
            setattr(module, name, original)
    return captured


def _is_spec_name(name: str) -> bool:
    """True when the last word of *name* is ``spec`` or ``specs`` (``clusterSpecs``)."""
    words = _CAMEL_BOUNDARY.sub("_", name).replace("-", "_").lower().split("_")
    return words[-1] in _SPEC_WORDS


def _secret_example_keys(example: Any) -> list[str]:
    """Keys of an example call that look like a secret and hold text."""
    found: list[str] = []
    if isinstance(example, Mapping):
        for key, value in example.items():
            if is_secret_param_name(str(key)) and isinstance(value, str):
                found.append(str(key))
            found.extend(_secret_example_keys(value))
    elif isinstance(example, list):
        for item in example:
            found.extend(_secret_example_keys(item))
    return found


def _flagged_fields(schema: Any) -> list[str]:
    """Field names in *schema* that look like a secret or a vendor spec."""
    found: list[str] = []
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                declared = sub.get("type") if isinstance(sub, Mapping) else None
                secret = is_secret_param_name(str(name)) and declared not in _NON_SECRET_JSON_TYPES
                if secret or _is_spec_name(str(name)):
                    found.append(str(name))
                found.extend(_flagged_fields(sub))
        for key, value in schema.items():
            if key == "examples":
                found.extend(_secret_example_keys(value))
            elif key != "properties":
                found.extend(_flagged_fields(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_flagged_fields(item))
    return found


def _unmarked(ops: list[_Op]) -> dict[str, list[str]]:
    """Flagged operations whose bodies a trace would still keep: op id -> field names."""
    unmarked: dict[str, list[str]] = {}
    for op in ops:
        fields = sorted(set(_flagged_fields(op.parameter_schema or {})))
        if fields and not classify_body_exclusion(op.op_id, tags=op.tags).excluded:
            unmarked[op.op_id] = fields
    return unmarked


@pytest.fixture(scope="module")
def registered_ops() -> list[_Op]:
    return _registered_ops()


def test_the_guard_sees_the_registered_operations(registered_ops: list[_Op]) -> None:
    """Not empty: the registrars ran, and the known secret operations are flagged."""
    assert len(registered_ops) >= 100
    flagged = {op.op_id for op in registered_ops if _flagged_fields(op.parameter_schema or {})}
    assert {
        "sddc.host.commission",
        "sddc.domain.validate",
        "sddc.domain.create",
        "installer.sddc.spec.validate",
        "installer.sddc.bringup.start",
        "installer.sddc.bringup.retry",
        "installer.composite.sddc.bringup",
        "installer.system.depot.set",
    } <= flagged


def test_every_operation_with_a_secret_or_spec_field_keeps_its_bodies_out_of_traces(
    registered_ops: list[_Op],
) -> None:
    missing = {
        op_id: fields for op_id, fields in _unmarked(registered_ops).items() if op_id not in _EXEMPT
    }
    assert missing == {}, (
        "These operations have a field named like a secret or a vendor spec, but a "
        "trace would keep their bodies. Give each the tag 'credential', or add it to "
        f"_EXEMPT with a one-line reason if it sends no secret: {missing!r}"
    )


def test_every_exemption_is_still_needed(registered_ops: list[_Op]) -> None:
    """An exempt operation that is gone, lost its field or is now marked leaves the list."""
    stale = sorted(set(_EXEMPT) - set(_unmarked(registered_ops)))
    assert stale == [], f"Remove these operations from _EXEMPT: {stale}"


def test_the_guard_reports_an_unmarked_operation() -> None:
    """Red path: each kind of flagged field is reported until the operation is marked."""
    password_field = _Op(
        "acme.user.create",
        {"type": "object", "properties": {"password": {"type": "string"}}},
        ("write",),
    )
    spec_field = _Op(
        "acme.cluster.deploy",
        {"type": "object", "properties": {"clusterSpec": {"type": "object"}}},
        ("write",),
    )
    example_key = _Op(
        "acme.depot.set",
        {
            "type": "object",
            "properties": {"settings": {"type": "object"}},
            "examples": [{"settings": {"account": {"password": "<depot-password>"}}}],
        },
        ("write",),
    )
    ops = [password_field, spec_field, example_key]

    assert _unmarked(ops) == {
        "acme.user.create": ["password"],
        "acme.cluster.deploy": ["clusterSpec"],
        "acme.depot.set": ["password"],
    }
    assert _unmarked([op._replace(tags=(*op.tags, "credential")) for op in ops]) == {}
