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

The ``approval.rerun`` row of a re-run refused to a caller who did not
make the request has ``requester_sub`` set to ``None``: the row does
not name the requester, and the caller is only ``principal_sub``.

Rows in the older form have no ``requester_sub``. Their
``payload['principal_sub']`` names the requester, while
``payload['principal_name']`` names the person who acted. The audit log
is append-only, so those rows stay as they are. Each reader shows them
through :func:`approval_payload_view`: the requester's id is shown in
``requester_sub``, and ``principal_sub`` shows the row's
``operator_sub``, the person that ``principal_name`` belongs to. This
holds for every such row of an approval request (it has an
``approval_request_id``), also when the requester acted on the row
themselves, so every reader finds the requester in ``requester_sub``.
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
    it has an ``approval_request_id``, a string ``principal_sub`` and no
    ``requester_sub`` key. A row with the key, also with the value
    ``None``, and every other row come back unchanged.
    """
    view = dict(payload)
    if method != "APPROVAL" or "requester_sub" in view or "approval_request_id" not in view:
        return view
    stored_sub = view.get("principal_sub")
    if not isinstance(stored_sub, str):
        return view
    view["requester_sub"] = stored_sub
    view["principal_sub"] = operator_sub
    return view
