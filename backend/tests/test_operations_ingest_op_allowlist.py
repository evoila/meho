# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Declarative per-product ingest op allowlist filter (security review T3-F01).

Unit-level coverage of
:func:`meho_backplane.operations.ingest.op_allowlist.apply_op_allowlist` — the
pure filter the ingest path runs before persistence. The DB-backed proof that
a wide spec ingests to exactly the allowlisted ops (and that the ingest result
carries the dropped count) lives in
``test_connectors_meho_automation.py``; the catalog-data + drift-guard
coverage lives in ``test_operations_ingest_catalog.py``.
"""

from __future__ import annotations

from meho_backplane.operations.ingest.op_allowlist import apply_op_allowlist
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

_MEHOAUTO = ("mehoauto", "0.1.0")


def _proto(method: str, path: str) -> EndpointDescriptorProto:
    return EndpointDescriptorProto(op_id=f"{method}:{path}", method=method, path=path)


def test_product_without_an_allowlist_keeps_every_op() -> None:
    """Regression guard (T3-F01 crit. c): a product that declares no allowlist
    is unaffected — every parsed op is kept, nothing is dropped."""
    ops = [
        _proto("GET", "/api/vcenter/vm"),
        _proto("DELETE", "/api/vcenter/vm/{vm}"),
        _proto("POST", "/api/vcenter/vm"),
    ]
    result = apply_op_allowlist(product="vmware", version="9.0", operations=ops)
    assert result.kept == tuple(ops)
    assert result.dropped == ()


def test_unknown_product_keeps_every_op() -> None:
    """A product absent from the catalog behaves as 'no allowlist'."""
    ops = [_proto("DELETE", "/whatever")]
    result = apply_op_allowlist(product="not-a-real-product", version="0.0", operations=ops)
    assert result.kept == tuple(ops)
    assert result.dropped == ()


def test_allowlisted_product_drops_ops_outside_the_allowlist() -> None:
    ops = [
        _proto("POST", "/api/v1/runs"),  # kept
        _proto("DELETE", "/api/v1/tenants/{tenant_id}"),  # dropped
        _proto("POST", "/api/v1/fleet/import"),  # dropped
        _proto("POST", "/api/v1/blueprints/{blueprint_id}/validate"),  # kept
    ]
    result = apply_op_allowlist(product=_MEHOAUTO[0], version=_MEHOAUTO[1], operations=ops)
    assert {op.op_id for op in result.kept} == {
        "POST:/api/v1/runs",
        "POST:/api/v1/blueprints/{blueprint_id}/validate",
    }
    assert {(d.method, d.path) for d in result.dropped} == {
        ("DELETE", "/api/v1/tenants/{tenant_id}"),
        ("POST", "/api/v1/fleet/import"),
    }


def test_key_normalisation_lowercase_verb_and_query_string() -> None:
    """Method is upper-cased and any query string is stripped before matching —
    the same keying the connector-owned safety floor uses."""
    ops = [
        _proto("post", "/api/v1/runs?dry_run=true"),  # matches after normalise
        _proto("get", "/api/v1/runs"),  # different verb -> dropped
    ]
    result = apply_op_allowlist(product=_MEHOAUTO[0], version=_MEHOAUTO[1], operations=ops)
    assert [op.op_id for op in result.kept] == ["post:/api/v1/runs?dry_run=true"]
    assert [(d.method, d.path) for d in result.dropped] == [("GET", "/api/v1/runs")]


def test_empty_operations_is_a_noop() -> None:
    result = apply_op_allowlist(product=_MEHOAUTO[0], version=_MEHOAUTO[1], operations=[])
    assert result.kept == ()
    assert result.dropped == ()
