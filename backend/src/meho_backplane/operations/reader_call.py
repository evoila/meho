# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer of the general call doors to a caller with the Reader role.

REST ``POST /api/v1/operations/call`` and the MCP tool ``call_operation``
need the ``operator`` role. A Reader is refused there, and MEHO stores
nothing and runs nothing. Which refusal the Reader gets depends on the
operation, so the door looks it up first, the same way :func:`dispatch`
does (the connector id, then
:func:`~meho_backplane.operations._lookup.lookup_descriptor`):

* an operation that asks for a change
  (:func:`~meho_backplane.operations.role_floors.asks_for_change`): the
  code and the sentence of
  :data:`~meho_backplane.operations.role_floors.READER_CHANGE_SENTENCE`;
* a ``safe`` operation that needs no approval, or an operation that cannot
  be found: the plain role answer of the door.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import TenantRole

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = ["reader_call_refusal"]


async def reader_call_refusal(operator: Operator, arguments: object) -> str | None:
    """The Reader sentence when *arguments* ask for a change, or ``None``.

    *arguments* is the body of the call: ``connector_id``, ``op_id`` and
    ``params``. ``None`` means the door gives its plain role answer: the
    caller is not a Reader, the arguments name no operation, the operation
    cannot be found, or the call is a plain read.
    """
    if operator.tenant_role is not TenantRole.READ_ONLY or not isinstance(arguments, dict):
        return None
    connector_id = arguments.get("connector_id")
    op_id = arguments.get("op_id")
    raw_params = arguments.get("params")
    if not isinstance(connector_id, str) or not isinstance(op_id, str):
        return None
    if not connector_id or not op_id:
        return None
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
    from meho_backplane.operations._secret_values import split_reveal_secret
    from meho_backplane.operations.credential_read import raise_descriptor_for_reveal
    from meho_backplane.operations.role_floors import asks_for_change, reader_change_refusal

    params, reveal_secret = split_reveal_secret(params)
    product, version, impl_id = parse_connector_id(connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
    if descriptor is None:
        return None
    raise_descriptor_for_reveal(descriptor, reveal_secret=reveal_secret)
    return reader_change_refusal(
        operator.tenant_role, is_change=asks_for_change(descriptor, params)
    )
