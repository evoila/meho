# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stable composite + typed handler shapes for register-composite tests.

The dispatcher's ``import_handler`` walks the persisted
``handler_ref`` dotted path via :func:`importlib.import_module` plus
chained :func:`getattr`; the round-trip only works for module-level
callables and bound methods of module-level classes. Keeping the
handlers here -- rather than at the test module's top level -- means
the tests' ``handler_ref`` assertions stay readable
(``"tests.fixtures.composites.handlers.<name>"`` is short and
location-stable).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.composite import DispatchChild


async def composite_module_level_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Module-level composite handler -- the typical composite shape.

    Annotated against :class:`DispatchChild` so static type checkers
    treat the ``dispatch_child(...)`` calls inside composites as
    keyword-checked. The body is a no-op echo: tests that only need
    the handler to *exist* (signature validation, dotted-path
    round-trip, registration tests) don't dispatch through it.
    """
    return {"ok": True, "params": params, "operator_sub": operator.sub}


async def composite_typed_shaped_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A handler with the *typed* signature -- no ``dispatch_child`` parameter.

    Used by the cross-rejection test that passes this to
    :func:`register_composite_operation` and asserts the helper raises
    :class:`HandlerSignatureError` rather than silently registering a
    composite that would crash at first dispatch with a
    :exc:`TypeError` (missing keyword).
    """
    return {"ok": True, "params": params}


async def typed_sub_op_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler the end-to-end composite test dispatches into.

    The composite's ``dispatch_child(...)`` call routes here through
    the production dispatcher; the handler echoes its params + target
    id so the test can assert (a) the sub-op fired with the composite's
    params, (b) the audit row links to the composite's parent_audit_id.
    """
    return {
        "echo": params,
        "target_id": str(getattr(target, "id", None)),
    }


async def composite_dispatch_child_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Composite that dispatches a typed sub-op exactly once.

    Used by the end-to-end test: registered via
    :func:`register_composite_operation`, dispatched through the
    production :func:`dispatch` entrypoint, asserts the sub-op fired
    and the audit row carries ``parent_audit_id``.
    """
    result = await dispatch_child(
        connector_id=params["sub_connector_id"],
        op_id=params["sub_op_id"],
        params=params.get("sub_params", {}),
    )
    return {"sub_status": result.status, "sub_result": result.result}


class CompositeHandlerHost:
    """A class with an async composite method.

    Bound-method handler shape, parallel to ``SampleHandlerClass`` in
    the typed register suite. Exercises the ``self``-drop branch in
    :func:`_handler_parameter_names`.
    """

    async def composite_bound_method(
        self,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
        dispatch_child: DispatchChild,
    ) -> dict[str, Any]:
        return {"ok": True, "params": params}

    async def typed_bound_method(
        self,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return {"ok": True, "params": params}
