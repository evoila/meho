# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry for connector-owned corrections to ingested operation shapes.

Some vendor specs describe an operation wrongly: the parsed request body
does not match what the live server accepts, or the summary is missing.
The parser stays vendor-neutral, so a connector can register corrections
here:

* a per-operation correction gets the catalog ``version`` and one parsed
  proto, and returns either the same proto (no change) or a fixed copy;
* a batch correction gets the catalog ``version`` and every proto of one
  spec, for fixes that must compare operations with each other.

Both run before the safety floor (:mod:`.safety_floors`) on both the
register and the dry-run paths, through :func:`apply_spec_corrections`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = [
    "apply_spec_correction",
    "apply_spec_corrections",
    "register_ingest_batch_correction",
    "register_ingest_spec_correction",
]

IngestSpecCorrection = Callable[[str, EndpointDescriptorProto], EndpointDescriptorProto]
IngestBatchCorrection = Callable[
    [str, Sequence[EndpointDescriptorProto]], tuple[EndpointDescriptorProto, ...]
]
_CORRECTIONS: dict[tuple[str, str], IngestSpecCorrection] = {}
_BATCH_CORRECTIONS: dict[tuple[str, str], IngestBatchCorrection] = {}


def register_ingest_spec_correction(
    *, product: str, impl_id: str, correction: IngestSpecCorrection
) -> None:
    """Register a connector-owned correction during the connector module import."""
    _CORRECTIONS[(product, impl_id)] = correction


def register_ingest_batch_correction(
    *, product: str, impl_id: str, correction: IngestBatchCorrection
) -> None:
    """Register a connector-owned correction that sees every op of one spec."""
    _BATCH_CORRECTIONS[(product, impl_id)] = correction


def apply_spec_correction(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Apply the connector's per-operation correction, or return *proto* unchanged."""
    correction = _CORRECTIONS.get((product, impl_id))
    return correction(version, proto) if correction is not None else proto


def apply_spec_corrections(
    *,
    product: str,
    version: str,
    impl_id: str,
    protos: Sequence[EndpointDescriptorProto],
) -> tuple[EndpointDescriptorProto, ...]:
    """Apply the per-operation correction to each proto, then the batch correction."""
    corrected = tuple(
        apply_spec_correction(product=product, version=version, impl_id=impl_id, proto=proto)
        for proto in protos
    )
    batch_correction = _BATCH_CORRECTIONS.get((product, impl_id))
    return batch_correction(version, corrected) if batch_correction is not None else corrected
