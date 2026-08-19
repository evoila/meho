# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vrops8 (legacy vROps 8.x) reconcile lane (#3067) — the subclass op-set drift guard.

``Vrops8Connector`` is a thin **subclass** of the modern ``vrops-rest`` impl: it
introduces **no new hand-coded paths** (the ``/suite-api/api/*`` constants live on
the modern ``vcf_operations.connector`` module and are already served-set-reconciled
against the pinned ``vcf-operations-9.0`` OpenAPI 3.0 spec by
:mod:`tests.test_connectors_vcf_operations_spec_reconcile`). The vROps Suite API
paths are **stable across the 8.x↔9.x boundary** (``major`` stays ``8`` across the
whole 8.x line; the 8.10 Aria rebrand is name-only), so those already-reconciled 9.x
paths serve the 8.x impl too — path existence is version-stable even where response
schemas drift.

**Evidenced exclusion — no committable 8.x spec.** vROps / Aria Operations 8.x
publishes only a **per-instance Swagger 2.0** document
(``/suite-api/doc/v2/api-docs``); there is no OpenAPI 3.x and no committable
``vmware/*`` artifact for 8.x, so MEHO's OA3-only ingest (#2090) can't consume it —
hence the typed read core (reused from the modern sibling). Full evidence +
activation trigger live in the ``vrops-vrops8`` entry of
``docs/decisions/spec-reconcile-guards-standard.md``.

So the reconcile guard unique to the subclass is the **op-set drift guard**: pin
that the 8.x impl reuses *exactly* the modern typed op-set, verbatim. If the modern
connector grows a 9.x-only typed op, the 8.x impl (whose registrar reuses the modern
tuple) would silently inherit it — this test reds first, forcing a conscious decision
about whether the new op is 8.x-valid. Parse-only (no DB / shelf / containers), so it
runs in the required unit sweep.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_operations.typed_ops import VROPS_TYPED_OPS
from meho_backplane.connectors.vrops8 import connector as _connector
from meho_backplane.connectors.vrops8 import typed_ops as _typed_ops
from meho_backplane.connectors.vrops8.connector import Vrops8Connector
from meho_backplane.operations._handler_resolve import is_unbound_method

#: The modern audited typed read set the 8.x impl reuses verbatim (#2303/#2838).
#: A change to the modern set reds this pin so the 8.x applicability is reviewed.
_EXPECTED_REUSED_OP_IDS = {
    "vrops.liveness",
    "vrops.alert.list",
    "vrops.resource.query",
    "vrops.resource.stats",
}


def test_vrops8_reuses_modern_typed_op_ids_verbatim() -> None:
    """Pin the reused op-set: the 8.x impl registers exactly the modern typed op_ids.

    The 8.x registrar (:func:`register_vrops8_typed_operations`) iterates
    ``VROPS_TYPED_OPS`` (imported from the modern module) and upserts each op_id under
    the ``(vrops, 8.0, vrops-vrops8)`` triple. Pinning the modern set here means a new
    modern op can't silently join the 8.x surface unreviewed.
    """
    assert {op.op_id for op in VROPS_TYPED_OPS} == _EXPECTED_REUSED_OP_IDS


def test_vrops8_introduces_no_new_hand_coded_paths() -> None:
    """The subclass adds no ``_*_PATH`` constants — its surface is the modern (reconciled) one.

    Guards the "subclass introduces nothing un-reconciled" invariant: if a future
    change adds an 8.x-specific request path, this fails and the author must add a
    served-set (or manifest-pin) reconcile for it rather than shipping an unguarded path.
    """
    for module in (_connector, _typed_ops):
        offending = [
            name
            for name, value in vars(module).items()
            if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
        ]
        assert offending == [], (
            f"{module.__name__} introduced un-reconciled path constants: {offending}"
        )


def test_vrops8_inherits_every_typed_handler_and_is_rebindable() -> None:
    """Every reused op's handler resolves on the subclass AND is MRO-rebindable to it.

    Two guards on the load-bearing dispatch mechanism (the #3067 review's finding 4):

    * ``getattr(Vrops8Connector, handler_attr)`` returns the inherited base-class method
      for every op — otherwise the registrar would ``AttributeError`` at boot.
    * ``is_unbound_method(handler, Vrops8Connector)`` is ``True`` — the exact predicate
      the dispatcher uses to decide it must rebind the (base-class-authored, shared)
      ``handler_ref`` onto the resolver-chosen ``Vrops8Connector`` instance. If a
      regression made the inherited handler non-MRO-matchable (e.g. it stopped living in
      a class ``__dict__`` on the MRO), an 8.x dispatch would run unbound / on the wrong
      instance and silently present the 9.x scheme — this pins it red instead.
    """
    for op in VROPS_TYPED_OPS:
        handler = getattr(Vrops8Connector, op.handler_attr, None)
        assert callable(handler), (
            f"Vrops8Connector missing handler for {op.op_id} ({op.handler_attr})"
        )
        assert is_unbound_method(handler, Vrops8Connector), (
            f"{op.op_id} handler is not MRO-rebindable onto Vrops8Connector — a dispatch "
            "would not rebind to the 8.x instance/scheme"
        )
