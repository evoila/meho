# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry for connector-owned corrections to ingested operation shapes.

Some vendor specs describe an operation wrongly: the parsed request body
does not match what the live server accepts. The parser stays
vendor-neutral, so a connector can register a correction here. The
correction gets the catalog ``version`` and one parsed proto, and returns
either the same proto (no change) or a fixed copy. It runs before the
safety floor (:mod:`.safety_floors`) on both the register and the dry-run
paths.
"""

from __future__ import annotations

from collections.abc import Callable

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = ["apply_spec_correction", "register_ingest_spec_correction"]

IngestSpecCorrection = Callable[[str, EndpointDescriptorProto], EndpointDescriptorProto]
_CORRECTIONS: dict[tuple[str, str], IngestSpecCorrection] = {}


def register_ingest_spec_correction(
    *, product: str, impl_id: str, correction: IngestSpecCorrection
) -> None:
    """Register a connector-owned correction during the connector module import."""
    _CORRECTIONS[(product, impl_id)] = correction


def apply_spec_correction(
    *, product: str, version: str, impl_id: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Apply the connector's correction, or return *proto* unchanged."""
    correction = _CORRECTIONS.get((product, impl_id))
    return correction(version, proto) if correction is not None else proto
