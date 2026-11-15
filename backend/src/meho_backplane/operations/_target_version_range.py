# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Refuse an operation on a target whose version the connector does not serve.

A connector states the target versions it serves in
:attr:`~meho_backplane.connectors.base.Connector.supported_version_range`.
Many connectors also register a fallback entry that accepts every version,
so a target with no known version can still be probed. The resolver picks
that fallback for a target whose known version is outside the range, too.

:func:`version_outside_range` finds that case. ``dispatch`` refuses the
call with ``no_connector`` right after the connector-mismatch check, before
the policy gate and any wait for approval, and writes one audit row
(:func:`refuse_version_outside_range`). ``preview_dispatch`` returns the
same error, in the same place, without an audit row
(:func:`version_range_preview_envelope`). The error names the range.

A target with no known version, or a version that is not a version number,
is not refused here: MEHO cannot tell that it is outside the range. The
probe routes do not run this check, so a target can be probed again after
an upgrade.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import (
    OperationResult,
    resolve_connector_or_label,
    resolve_target_version,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import audit_rejection_safe, with_audit_receipt

__all__ = [
    "VersionOutsideRange",
    "refuse_version_outside_range",
    "version_outside_range",
    "version_range_preview_envelope",
]

_NO_CONNECTOR: Final = "no_connector"


@dataclass(frozen=True)
class VersionOutsideRange:
    """The target's version and the range of the connector picked for it."""

    message: str
    product: str | None
    target_version: str
    supported_version_range: str
    connector_id: str

    def extras(self, op_id: str) -> dict[str, Any]:
        """The ``extras`` of the error answer."""
        return {
            "error_code": _NO_CONNECTOR,
            "op_id": op_id,
            "product": self.product,
            "version": self.target_version,
            "supported_version_range": self.supported_version_range,
            "connector_id": self.connector_id,
        }


def version_outside_range(target: Any) -> VersionOutsideRange | None:
    """The refusal when *target*'s known version is outside its connector's range.

    ``None`` when there is no target, the target resolves no single
    connector (the connector step answers that), the connector states no
    range, the target has no readable version, or the version is in range.
    """
    if target is None:
        return None
    cls, label, _message = resolve_connector_or_label(target)
    if label is not None or cls is None or not cls.supported_version_range:
        return None
    target_version = resolve_target_version(target)
    if target_version is None:
        return None
    try:
        spec = SpecifierSet(cls.supported_version_range)
        parsed = Version(target_version)
    except (InvalidSpecifier, InvalidVersion):
        return None
    if parsed in spec:
        return None
    connector_id = f"{cls.impl_id}-{cls.version}"
    name = getattr(target, "name", None)
    target_label = f"Target '{name}'" if isinstance(name, str) and name else "This target"
    raw_product = getattr(target, "product", None)
    return VersionOutsideRange(
        message=(
            f"{target_label} runs version {target_version}. Connector {connector_id} "
            f"serves only versions {cls.supported_version_range}. Upgrade the target, "
            "or probe it again if its version changed."
        ),
        product=raw_product if isinstance(raw_product, str) and raw_product else None,
        target_version=target_version,
        supported_version_range=cls.supported_version_range,
        connector_id=connector_id,
    )


async def refuse_version_outside_range(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    params_hash: str,
    started: float,
) -> OperationResult | None:
    """The audited ``no_connector`` result, or ``None`` when the version fits."""
    outside = version_outside_range(target)
    if outside is None:
        return None
    duration_ms = (time.monotonic() - started) * 1000.0
    receipt = await audit_rejection_safe(
        audit_id=uuid.uuid4(),
        operator=operator,
        descriptor=descriptor,
        target=target,
        params_hash=params_hash,
        result_status=_NO_CONNECTOR,
        duration_ms=duration_ms,
    )
    result = OperationResult(
        status="error",
        op_id=descriptor.op_id,
        error=f"{_NO_CONNECTOR}: {outside.message}",
        duration_ms=duration_ms,
        extras=outside.extras(descriptor.op_id),
    )
    return with_audit_receipt(result, receipt)


def version_range_preview_envelope(
    *, descriptor: EndpointDescriptor, connector_id: str, target: Any
) -> dict[str, Any] | None:
    """The preview error envelope for a version outside the range, or ``None``."""
    outside = version_outside_range(target)
    if outside is None:
        return None
    return {
        "status": "error",
        "op_id": descriptor.op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "error": f"{_NO_CONNECTOR}: {outside.message}",
        "extras": outside.extras(descriptor.op_id),
    }
