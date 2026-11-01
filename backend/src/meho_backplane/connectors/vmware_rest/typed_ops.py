# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) ops for :class:`VmwareRestConnector`.

A typed op (``source_kind="typed"``) is a bound method on
:class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
that reads or writes through the connector's own authenticated session.
It needs no ingested catalog, so it works on a fresh boot. The sibling
precedent is :mod:`meho_backplane.connectors.argocd.ops`.

This module holds what every typed op shares (:class:`VmwareTypedOp`,
two small value helpers) and the registrars. It keeps **no list of
operations**. Each ``typed_ops_<feature>.py`` module in this package
exports its own rows, and this module collects them at import time:

* ``TYPED_OPS`` -- a tuple of :class:`VmwareTypedOp` rows.
* ``WHEN_TO_USE_BY_GROUP`` -- the curated ``when_to_use`` text for each
  ``group_key`` those rows use.

:func:`_collect_typed_ops` walks the package with :mod:`pkgutil` (the
way :mod:`meho_backplane.connectors.registry` finds connector
subpackages), imports every ``typed_ops_*`` module, and joins the rows
sorted by op id into :data:`VMWARE_TYPED_OPS`. A module that defines
neither name is a helper and is skipped. Two modules that claim the same
op id, or the same group key with different text, fail at import. So a
pull request that adds a typed op adds or changes its own module only.
The guard test ``test_connectors_vmware_rest_typed_ops_collect.py``
fails when a module's rows are not collected.

The feature modules import :class:`VmwareTypedOp` and the helpers from
here, and this module imports them back while collecting. The
collection therefore runs at the bottom of this file, after those names
exist. ``vmware.host.usage`` lives in :mod:`typed_ops_host_usage`; its
implementation is re-exported here because the connector method imports
it from this module.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VMWARE_TYPED_OPS",
    "VMWARE_TYPED_WHEN_TO_USE_BY_GROUP",
    "VmwareTypedOp",
    "host_usage_impl",
    "register_vmware_typed_operations",
    "register_vmware_typed_operations_8_0",
]

_log = structlog.get_logger(__name__)

_PACKAGE = __name__.rpartition(".")[0]
#: Feature modules of this package start with this prefix.
TYPED_OPS_MODULE_PREFIX = "typed_ops_"


@dataclass(frozen=True)
class VmwareTypedOp:
    """Metadata for one vmware typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vmware_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method``
    path. Mirrors
    :class:`~meho_backplane.connectors.argocd.ops.ArgoCdOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` on a pre-7 REST envelope, else *payload*.

    vSphere's Automation REST straddles a bare-array modern shape and a
    legacy ``{"value": [...]}`` envelope (some vcsim builds). The caller
    only wants the list either way.
    """
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number/numeric-string to int; anything else -> None.

    VI-JSON serialises the ``xsd:long`` ``memorySize`` and the ``xsd:int``
    quickStats counters as JSON numbers, but some intermediaries render a
    64-bit value as a JSON string. Accept both; reject bools (``True`` is
    an ``int`` subclass in Python and must not read as ``1`` here).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def typed_op_module_names() -> tuple[str, ...]:
    """The ``typed_ops_*`` modules of this package, in name order."""
    package = importlib.import_module(_PACKAGE)
    return tuple(
        sorted(
            name
            for _, name, ispkg in pkgutil.iter_modules(package.__path__)
            if not ispkg and name.startswith(TYPED_OPS_MODULE_PREFIX)
        )
    )


def _collect_typed_ops(
    modules: Iterable[ModuleType],
) -> tuple[tuple[VmwareTypedOp, ...], dict[str, str]]:
    """Join the ``TYPED_OPS`` rows and ``WHEN_TO_USE_BY_GROUP`` texts of *modules*.

    Rows come back sorted by op id. A duplicate op id, or one group key
    with two different texts, raises :class:`ValueError` so the clash is
    found at import and not after the rows are in the database.
    """
    rows: dict[str, tuple[str, VmwareTypedOp]] = {}
    texts: dict[str, tuple[str, str]] = {}
    for module in modules:
        for op in getattr(module, "TYPED_OPS", ()):
            if op.op_id in rows:
                raise ValueError(
                    f"vmware typed op {op.op_id!r} is defined twice: in "
                    f"{rows[op.op_id][0]} and in {module.__name__}"
                )
            rows[op.op_id] = (module.__name__, op)
        for key, text in getattr(module, "WHEN_TO_USE_BY_GROUP", {}).items():
            if key in texts and texts[key][1] != text:
                raise ValueError(
                    f"vmware typed op group {key!r} has two when_to_use texts: in "
                    f"{texts[key][0]} and in {module.__name__}"
                )
            texts.setdefault(key, (module.__name__, text))
    ops = tuple(rows[op_id][1] for op_id in sorted(rows))
    return ops, {key: text for key, (_, text) in texts.items()}


def _import_typed_op_modules() -> list[ModuleType]:
    return [importlib.import_module(f"{_PACKAGE}.{name}") for name in typed_op_module_names()]


# The feature modules import ``VmwareTypedOp`` and the helpers above from
# this module, so the collection must run after those names exist.
VMWARE_TYPED_OPS: tuple[VmwareTypedOp, ...]
#: Curated ``when_to_use`` blurb per typed-op group.
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
VMWARE_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str]
VMWARE_TYPED_OPS, VMWARE_TYPED_WHEN_TO_USE_BY_GROUP = _collect_typed_ops(_import_typed_op_modules())

# ``VmwareRestConnector.host_usage`` imports the implementation from here.
from meho_backplane.connectors.vmware_rest.typed_ops_host_usage import (  # noqa: E402
    host_usage_impl,
)


async def register_vmware_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
    version: str | None = None,
) -> None:
    """Upsert every op in :data:`VMWARE_TYPED_OPS` into ``endpoint_descriptor``.

    ``version`` picks the catalog; ``None`` means the 9.0 catalog.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    has walked every connector subpackage, so the descriptor rows land
    before the first dispatch. Idempotent across pod restarts (the helper
    skips the embedding recompute on unchanged summary / description /
    tags). Mirrors :func:`register_vmware_composite_operations` and the
    argocd typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar,
    so each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests
    # should not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    version = version or VmwareRestConnector.version
    for op in VMWARE_TYPED_OPS:
        handler = getattr(VmwareRestConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VmwareRestConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VmwareRestConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to VMWARE_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=VmwareRestConnector.product,
            version=version,
            impl_id=VmwareRestConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "vmware_typed_operations_registered",
        count=len(VMWARE_TYPED_OPS),
        product=VmwareRestConnector.product,
        version=version,
        impl_id=VmwareRestConnector.impl_id,
    )


async def register_vmware_typed_operations_8_0(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """The same typed ops on ``vmware-rest-8.0``, where 8.0.x targets resolve (#3789).

    The dispatcher binds the :class:`VmwareRestConnector` method handlers to
    the resolved :class:`VmwareRest80Connector` instance (a subclass).
    """
    from meho_backplane.connectors.vmware_rest.connector import VmwareRest80Connector

    await register_vmware_typed_operations(
        embedding_service=embedding_service, version=VmwareRest80Connector.version
    )
