# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Upgrade step: the built-in network checks need no approval again.

For a short time the nine built-in network checks (``net.*``) carried the
caller-address marker. Registration then stored them at risk level
``caution`` with approval on. Registration never lowers a stored value, so
on an existing install these rows would keep ``caution`` and sensors could
not use the checks.

:func:`restore_built_in_check_defaults` sets such a row back to the values
the check registers now (``safe``, no approval). It changes a row only when
all of this is true:

* the row is one of the nine built-in checks;
* the row is enabled;
* the row holds exactly the values that marker wrote: ``caution``, approval
  on, and the marker in its stored parameters;
* the new parameters carry no marker.

Any other row keeps its values. So a restriction an operator set by hand
stays: a disabled row, a stricter level, or a row with other values.
Operator overrides live in their own table and are not touched.
"""

from __future__ import annotations

from typing import Final

import structlog

from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.caller_address import has_caller_address

__all__ = ["BUILT_IN_CHECK_OPS", "restore_built_in_check_defaults"]

_BUILT_IN_CHECK_KEY: Final[tuple[str, str, str]] = ("net", "1.x", "net-probe")

BUILT_IN_CHECK_OPS: Final[frozenset[str]] = frozenset(
    {
        "net.dns_lookup",
        "net.http_probe",
        "net.ntp_check",
        "net.path_mtu",
        "net.ping",
        "net.ssh_keyscan",
        "net.tcp_check",
        "net.tls_inspect",
        "net.trace",
    }
)


def restore_built_in_check_defaults(
    existing: EndpointDescriptor,
    *,
    parameter_schema: object,
    safety_level: str,
    requires_approval: bool,
) -> bool:
    """Set a built-in check row back to its new values; return whether it changed."""
    if (existing.product, existing.version, existing.impl_id) != _BUILT_IN_CHECK_KEY:
        return False
    if existing.op_id not in BUILT_IN_CHECK_OPS or existing.tenant_id is not None:
        return False
    if not existing.is_enabled:
        return False
    if existing.safety_level != "caution" or existing.requires_approval is not True:
        return False
    if not has_caller_address(existing.parameter_schema) or has_caller_address(parameter_schema):
        return False
    existing.safety_level = safety_level
    existing.requires_approval = requires_approval
    structlog.get_logger().info(
        "built_in_check_defaults_restored",
        op_id=existing.op_id,
        safety_level=safety_level,
        requires_approval=requires_approval,
    )
    return True
