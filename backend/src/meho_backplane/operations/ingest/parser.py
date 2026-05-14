# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-facing connector_id parser.

The CLI / REST / MCP surfaces accept a single-string connector
identifier (``"vmware-rest-9.0"``) so operators don't have to type
the three underlying fields separately. This module is the single
source of truth for how that string maps onto the
``(product, version, impl_id)`` triple the
:class:`~meho_backplane.db.models.OperationGroup` and
:class:`~meho_backplane.db.models.EndpointDescriptor` natural keys
expect.

The function itself is intentionally tiny (one branch + one regex
shape encoded as a hand-rolled scan) so it can live in a leaf module
without depending on the rest of the package; the consumer surface
(``meho_backplane.operations.ingest``) re-exports it via
``__init__.py``.
"""

from __future__ import annotations

__all__ = ["parse_connector_id"]


def parse_connector_id(connector_id: str) -> tuple[str, str, str]:
    """Split an operator-facing connector_id into ``(product, version, impl_id)``.

    Convention from ``docs/architecture/connectors.md``: the
    operator-facing identifier is ``<impl_id>-<version>`` where
    ``version`` starts with a digit. The first dash that precedes a
    digit-starting suffix separates ``impl_id`` from ``version``;
    ``product`` is the first dash-segment of ``impl_id`` (or the full
    ``impl_id`` when no dash is present).

    Worked examples (from the architecture doc's connector inventory):

    * ``"vmware-rest-9.0"`` → ``("vmware", "9.0", "vmware-rest")``
    * ``"nsx-4.2"``         → ``("nsx", "4.2", "nsx")``
    * ``"harbor-2.x"``      → ``("harbor", "2.x", "harbor")``
    * ``"hetzner-robot-2026-04"`` → ``("hetzner", "2026-04", "hetzner-robot")``
    * ``"vault-1.x"``       → ``("vault", "1.x", "vault")``
    * ``"k8s-1.x"``         → ``("k8s", "1.x", "k8s")``

    The "first dash before a digit" heuristic is what makes the last
    example unambiguous: ``hetzner-robot-2026-04`` could otherwise be
    split as ``(hetzner-robot-2026, 04)`` or
    ``(hetzner-robot, 2026-04)``; the convention picks the latter.

    Parameters
    ----------
    connector_id:
        The operator-facing identifier.

    Returns
    -------
    tuple[str, str, str]
        ``(product, version, impl_id)``.

    Raises
    ------
    ValueError
        ``connector_id`` does not match the convention (no dash, or
        no dash followed by a digit). Callers translate this to
        :class:`ConnectorNotFoundError` for the operator-facing API;
        the parse helper itself returns a precise exception so
        callers can log the offending input verbatim.
    """
    for i, ch in enumerate(connector_id):
        if ch == "-" and i + 1 < len(connector_id) and connector_id[i + 1].isdigit():
            impl_id = connector_id[:i]
            version = connector_id[i + 1 :]
            if not impl_id:
                raise ValueError(
                    f"connector_id {connector_id!r}: empty impl_id segment",
                )
            first_dash = impl_id.find("-")
            product = impl_id[:first_dash] if first_dash != -1 else impl_id
            return product, version, impl_id
    raise ValueError(
        f"connector_id {connector_id!r}: expected <impl_id>-<version> "
        "where version starts with a digit",
    )
