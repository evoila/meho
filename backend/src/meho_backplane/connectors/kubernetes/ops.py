# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`KubernetesConnector`.

This module holds :class:`KubernetesOp` (the shape of one registration
row) and :data:`KUBERNETES_OPS`. It keeps **no list of operations**.

Each ``ops_<feature>.py`` module in this package exports its own rows as
``OPS``: a tuple of :class:`KubernetesOp`. :func:`collect_ops` walks the
package with :mod:`pkgutil` (the way :mod:`meho_backplane.connectors.registry`
finds connector subpackages), imports every ``ops_*`` module, and joins the
rows sorted by op id. A module without ``OPS`` is a helper and adds
nothing. A row that is not a :class:`KubernetesOp`, or an op id that two
rows share, fails at import. So a pull request that adds an op adds its
own module (or changes its feature's module) and no shared list here. The
guard test ``test_connectors_k8s_ops_collect.py`` fails when a module's
rows are not collected.

The connector's ``register_operations()`` classmethod walks
:data:`KUBERNETES_OPS` once per process at lifespan startup and upserts
the descriptors through
:func:`~meho_backplane.operations.typed_register.register_typed_operation`.
The handler is a bound method on :class:`KubernetesConnector`, named by
the row's ``handler_attr``. The ``when_to_use`` text of each group stays
hand-written in ``connector.py`` (``_WHEN_TO_USE_BY_GROUP``), one entry
per group.

The feature modules import :class:`KubernetesOp` from here, and this
module imports them back while collecting. So the collection runs at the
bottom of this file, after :class:`KubernetesOp` exists.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

__all__ = ["KUBERNETES_OPS", "OPS_MODULE_PREFIX", "KubernetesOp", "collect_ops"]

#: Feature modules of this package start with this prefix.
OPS_MODULE_PREFIX = "ops_"


@dataclass(frozen=True)
class KubernetesOp:
    """Metadata for one K8s op the connector registers at startup.

    The fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate.

    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
    that exposes the async handler. The connector resolves the bound
    method against itself at registration time so
    :func:`~meho_backplane.operations.typed_register.derive_handler_ref`
    serialises a ``module.ClassName.method`` dotted path the dispatcher
    can resolve back via :func:`importlib.import_module` + chained
    :func:`getattr`.
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


def collect_ops(package: ModuleType) -> tuple[KubernetesOp, ...]:
    """Collect ``OPS`` from every ``ops_*`` module of *package*, sorted by op id.

    A module without ``OPS`` adds nothing. A row that is not a
    :class:`KubernetesOp` raises :class:`TypeError`, and an op id that two
    rows share raises :class:`ValueError`, so a wrong module fails at
    start-up and not at dispatch.
    """
    rows: dict[str, tuple[str, KubernetesOp]] = {}
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if info.ispkg or not info.name.startswith(OPS_MODULE_PREFIX):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for op in getattr(module, "OPS", ()):
            if not isinstance(op, KubernetesOp):
                raise TypeError(
                    f"{module.__name__}.OPS holds a {type(op).__name__}, not a KubernetesOp"
                )
            if op.op_id in rows:
                raise ValueError(
                    f"kubernetes op {op.op_id!r} is in {rows[op.op_id][0]} and in {module.__name__}"
                )
            rows[op.op_id] = (module.__name__, op)
    return tuple(rows[op_id][1] for op_id in sorted(rows))


def _kubernetes_ops() -> tuple[KubernetesOp, ...]:
    """Return the rows of every feature module of this package."""
    return collect_ops(importlib.import_module(__name__.rpartition(".")[0]))


KUBERNETES_OPS: tuple[KubernetesOp, ...] = _kubernetes_ops()
