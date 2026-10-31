# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Nsx4Connector -- the NSX 4.x implementation (``nsx-rest-4.2``).

NSX has two API generations that MEHO serves with two implementations:

* :class:`~meho_backplane.connectors.nsx.connector.NsxConnector`
  (``nsx-rest-9.0``) for NSX 9.x, the VCF-9 line. It also owns the
  ``("nsx", "", "")`` wildcard, so a target with no detected version
  lands there.
* :class:`Nsx4Connector` (``nsx-rest-4.2``) for the standalone NSX 4.x
  line (4.0, 4.1, 4.2).

The API differs by generation: transit gateways came with NSX 9.0 and
the Virtual Network Appliance with 9.1, and many paths changed shape.
One implementation per generation lets each one grow its own operations
and its own spec reconcile lane, as the dual-impl decision
(``docs/decisions/versioned-connector-dual-impl.md``) asks.

Why a thin subclass
-------------------

The session login (``POST /api/session/create``), the fingerprint
(``GET /api/v1/node``) and the ten typed reads use the same paths on
NSX 4.1, 4.2 and 9.x. So this class inherits all of it and changes only
the version identity and the version band. The two bands do not
overlap (``>=4.0,<9.0`` here, ``>=9.0,<10.0`` on the 9.x class), so the
resolver picks exactly one class for a target with a detected version.

The dispatcher rebinds the inherited typed-read handlers to the instance
the resolver chose
(:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`
walks the MRO), so a 4.x target runs the reads on an
:class:`Nsx4Connector` instance with its own session cache.

The ``impl_id`` stays ``nsx-rest``: the connector id is ``nsx-rest-4.2``
and :func:`~meho_backplane.operations._lookup.parse_connector_id` parses
it back to ``("nsx", "4.2", "nsx-rest")``. Same shape as the
``vmware-rest-8.0`` / ``vmware-rest-9.0`` pair.
"""

from __future__ import annotations

from meho_backplane.connectors.nsx.connector import NsxConnector

__all__ = ["Nsx4Connector"]


class Nsx4Connector(NsxConnector):
    """NSX 4.x read connector -- a thin subclass of the NSX 9.x connector.

    Overrides only the version identity and the version band; see the
    module docstring for why everything else is inherited.
    """

    version = "4.2"
    supported_version_range = ">=4.0,<9.0"
