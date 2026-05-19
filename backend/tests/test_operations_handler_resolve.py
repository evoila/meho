# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for #697 — connector-bound handler binding.

Two defects closed:

* :func:`is_unbound_method` used a ``__qualname__.startswith`` string
  heuristic that returned ``False`` when the resolved connector instance
  was a **subclass** of the class defining the handler (the bind9 E2E
  harness seeds ``_SeededBind9Connector`` under the ``Bind9Connector``
  registry key). The handler was then invoked **unbound**, surfacing as
  a misleading ``handler_unreachable``.

* :func:`dispatch_typed` silently dropped a leading ``self`` and called
  the handler anyway (guaranteed ``TypeError``, masked upstream as
  ``handler_unreachable``). It now fails loud and accurately.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.bind9.connector import Bind9Connector
from meho_backplane.operations._branches import dispatch_typed
from meho_backplane.operations._handler_resolve import import_handler, is_unbound_method


class _SeededBind9Connector(Bind9Connector):
    """Mirrors the bind9 E2E harness Phase-6 subclass-seed pattern."""


def _module_level_handler(operator: object, target: object, params: dict) -> None:
    """A non-connector module-level handler (must NOT be treated as unbound)."""


class TestIsUnboundMethodMroAware:
    def test_handler_on_exact_class(self) -> None:
        handler = import_handler("meho_backplane.connectors.bind9.connector.Bind9Connector.about")
        assert is_unbound_method(handler, Bind9Connector) is True

    def test_handler_on_base_resolved_via_subclass_instance(self) -> None:
        # The #697 case: instance is a subclass; handler is defined on the
        # base. The old startswith heuristic returned False here.
        handler = import_handler("meho_backplane.connectors.bind9.connector.Bind9Connector.about")
        assert is_unbound_method(handler, _SeededBind9Connector) is True

    def test_module_level_handler_is_not_unbound(self) -> None:
        assert is_unbound_method(_module_level_handler, Bind9Connector) is False

    def test_already_bound_method_is_not_unbound(self) -> None:
        bound = _SeededBind9Connector().about  # bound method
        assert is_unbound_method(bound, _SeededBind9Connector) is False


@pytest.mark.asyncio
async def test_dispatch_typed_fails_loud_on_unbound_self_handler() -> None:
    """An unbound connector method reaching dispatch_typed must raise a clear
    error — never a silent mis-call masked as handler_unreachable."""

    async def about(self, target, params):
        return {"ok": True}

    with pytest.raises(RuntimeError, match=r"still unbound \(first parameter 'self'\)"):
        await dispatch_typed(
            handler=about,
            operator=object(),
            target=object(),
            params={},
        )
