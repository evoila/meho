# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How the readers of the audit log show the payload of an approval row.

The payload of an approval row (``method='APPROVAL'``) names two people
in two clear pairs:

* ``principal_sub`` / ``principal_name``: the person who acted on this
  row. The same person as the row's ``operator_sub`` column.
* ``requester_sub`` / ``requester_name``: the person who asked for the
  operation.

A decision row also has ``reviewed_by`` / ``reviewed_by_name``.

Rows in the older form have no ``requester_sub``. Their
``payload['principal_sub']`` names the requester, while
``payload['principal_name']`` names the person who acted. The audit log
is append-only, so those rows stay as they are. Each reader shows them
through :func:`approval_payload_view`: the requester's id moves to
``requester_sub``, and ``principal_sub`` shows the row's
``operator_sub``, the person that ``principal_name`` belongs to.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["approval_payload_view"]


def approval_payload_view(
    *, method: str, operator_sub: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The payload of one audit row as a reader shows it.

    A copy of *payload*. Only an approval row in the older form changes:
    its ``principal_sub`` differs from *operator_sub* and it has no
    ``requester_sub``. Every other row comes back unchanged.
    """
    view = dict(payload)
    if method != "APPROVAL" or "requester_sub" in view:
        return view
    stored_sub = view.get("principal_sub")
    if not isinstance(stored_sub, str) or stored_sub == operator_sub:
        return view
    view["requester_sub"] = stored_sub
    view["principal_sub"] = operator_sub
    return view
