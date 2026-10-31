# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build the error for a descriptor lookup miss in dispatch and preview (#4031).

``dispatch`` and ``preview_dispatch`` both look up the descriptor for
*(connector_id, op_id)* first. When nothing matches, they call
:func:`op_miss_result`, which tells the two failure cases apart:

* ``unknown_connector`` -- the connector id matches no connector the
  caller can see (no operation rows, no registered class). Same test the
  ``list_operation_groups`` / ``search_operations`` meta-tools use. The
  error names up to 5 close ids (``extras.did_you_mean``, #4136).
* ``connector_package_not_installed`` -- the enabled op exists, but the
  connector package that wrote it is not installed (#5037).
* ``unknown_op`` -- the connector exists but has no such enabled op. The
  error names the other caller-visible connectors that have the op
  (``extras.connectors_with_op``), or points at ``search_operations``
  when there are none. When the call has a target, a connector whose
  catalog check refuses that target is not named (#5069), so the error
  never sends the caller to a connector that would refuse the call.

The extra lookups run only on this error path; a successful dispatch
never pays for them.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from meho_backplane.connectors import OperationResult
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._catalog_guard import catalog_refuses_target
from meho_backplane.operations._errors import (
    result_connector_package_not_installed,
    result_unknown_connector,
    result_unknown_op,
)
from meho_backplane.operations._lookup import (
    close_connector_ids,
    connector_class_registered,
    connector_exists,
    count_known_ops,
    find_connectors_with_op,
    find_uninstalled_package_owner,
)

__all__ = ["op_miss_result"]


async def op_miss_result(
    *,
    tenant_id: UUID,
    connector_id: str,
    op_id: str,
    triple: tuple[str, str, str],
    started: float,
    target: Any = None,
) -> OperationResult:
    """Return the error result for a descriptor lookup miss.

    *triple* is the parsed *(product, version, impl_id)* of
    *connector_id*. *started* is the caller's ``time.monotonic()`` start
    mark, used for ``duration_ms``. *target* is the call's target, or
    ``None``; with a target, connectors that refuse it are not named.
    """
    product, version, impl_id = triple
    package = await find_uninstalled_package_owner(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=op_id
    )
    if package is not None:
        return result_connector_package_not_installed(
            op_id, connector_id, package, _elapsed_ms(started)
        )
    known_op_count = await count_known_ops(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
    )
    if (
        known_op_count == 0
        and not await connector_exists(
            tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
        )
        and not connector_class_registered(product=product, version=version, impl_id=impl_id)
    ):
        did_you_mean = await close_connector_ids(tenant_id=tenant_id, connector_id=connector_id)
        return result_unknown_connector(
            connector_id, op_id, _elapsed_ms(started), did_you_mean=did_you_mean
        )

    def refuses_target(row: EndpointDescriptor) -> bool:
        return catalog_refuses_target(
            product=row.product,
            version=row.version,
            impl_id=row.impl_id,
            source_kind=row.source_kind,
            target=target,
        )

    others = await find_connectors_with_op(
        tenant_id=tenant_id,
        op_id=op_id,
        exclude=triple,
        skip=refuses_target if target is not None else None,
    )
    return result_unknown_op(
        op_id,
        known_op_count,
        _elapsed_ms(started),
        connector_id=connector_id,
        connectors_with_op=others,
    )


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
