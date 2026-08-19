# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vrli8 (legacy vRLI 8.x) reconcile lane (#3068) — the subclass op-set drift guard.

``Vrli8Connector`` is a thin **subclass** of the modern ``vrli-rest`` impl: it
introduces **no new hand-coded paths** (the vRLI ``/api/v2/*`` surface lives on the
modern ``vcf_logs`` module — the ``vrli.event.query`` constraint path, the session
mint, the ``/api/v2/version`` fingerprint — and is already guarded by
:mod:`tests.test_connectors_vcf_logs_spec_reconcile`). The vRLI v2 API is stable
across the 8.x↔9.x boundary (it shipped in vRLI 8.0), so the modern reconcile covers
the 8.x paths too.

**Evidenced exclusion — no committable 8.x spec.** vRLI 8.x publishes no committable
OpenAPI 3.x or Swagger 2.0 file (Broadcom's developer portal serves gated HTML; the
only machine-readable surface is a per-appliance runtime Swagger UI at
``/rest-api``; VMware's own archived client was hand-coded). MEHO's OA3-only ingest
(#2090) can't consume any of that — hence the typed read core (reused from the modern
sibling). Full evidence lives in the ``vrli-vrli8`` entry of
``docs/decisions/spec-reconcile-guards-standard.md``.

So the guard unique to the subclass is the **op-set drift guard**: pin that the 8.x
impl reuses *exactly* the modern typed op-set. If the modern connector grows a
9.x-only typed op, the 8.x impl (whose registrar reuses the modern tuple) would
silently inherit it — this test reds first, forcing a conscious 8.x-applicability
decision. Parse-only, so it runs in the required unit sweep.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_logs.typed_ops import VRLI_TYPED_OPS
from meho_backplane.connectors.vrli8 import connector as _connector
from meho_backplane.connectors.vrli8 import typed_ops as _typed_ops
from meho_backplane.connectors.vrli8.connector import Vrli8Connector
from meho_backplane.operations._handler_resolve import is_unbound_method

#: The modern typed read op the 8.x impl reuses verbatim (#2295). vRLI types only
#: the event query; the rest of its browse surface is generic-ingested on 9.x (no
#: committable 8.x spec to ingest — see the module docstring).
_EXPECTED_REUSED_OP_IDS = {"vrli.event.query"}


def test_vrli8_reuses_modern_typed_op_ids_verbatim() -> None:
    """Pin the reused op-set: the 8.x impl registers exactly the modern typed op_ids.

    A new modern typed op can't silently join the 8.x surface unreviewed — this pin
    reds so its 8.x applicability is decided consciously.
    """
    assert {op.op_id for op in VRLI_TYPED_OPS} == _EXPECTED_REUSED_OP_IDS


def test_vrli8_introduces_no_new_hand_coded_paths() -> None:
    """The subclass adds no ``_*_PATH`` constants — its surface is the modern (reconciled) one."""
    for module in (_connector, _typed_ops):
        offending = [
            name
            for name, value in vars(module).items()
            if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
        ]
        assert offending == [], (
            f"{module.__name__} introduced un-reconciled path constants: {offending}"
        )


def test_vrli8_inherits_every_typed_handler_and_is_rebindable() -> None:
    """Every reused op's handler resolves on the subclass AND is MRO-rebindable to it.

    Guards the load-bearing dispatch mechanism: ``is_unbound_method(handler,
    Vrli8Connector)`` is the exact predicate the dispatcher uses to decide it must
    rebind the (base-class-authored, shared) ``handler_ref`` onto the resolver-chosen
    ``Vrli8Connector`` instance. A regression making the inherited ``event_query``
    non-MRO-matchable would silently dispatch on the wrong instance — this pins it red.
    """
    for op in VRLI_TYPED_OPS:
        handler = getattr(Vrli8Connector, op.handler_attr, None)
        assert callable(handler), (
            f"Vrli8Connector missing handler for {op.op_id} ({op.handler_attr})"
        )
        assert is_unbound_method(handler, Vrli8Connector), (
            f"{op.op_id} handler is not MRO-rebindable onto Vrli8Connector"
        )
