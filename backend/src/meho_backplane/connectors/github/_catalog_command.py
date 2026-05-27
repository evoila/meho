# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog-command helper for the gh-rest connector.

Mirrors :mod:`meho_backplane.connectors.vmware_rest._catalog_command`
(G0.14-T10 / #1183). The
:func:`~meho_backplane.connectors.github.composites._preflight.preflight_l2_dependencies`
helper raises a
:class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
exception when one of a composite's L2 sub-ops is not registered. That
exception carries the operator-facing CLI command to run to ingest the
catalog entry that lands the missing ops.

The catalog row for the GitHub REST API is keyed under ``product=gh,
version=v3, impl_id=gh-rest`` (see
``backend/src/meho_backplane/operations/ingest/catalog.yaml`` row added
by G3.11-T3 #1228). The operator command shape matches the verb shipped
in #405 (G0.7-T5) and re-affirmed by T9 (#1182):

    meho connector ingest --catalog gh/v3

The catalog version label (``v3``) intentionally differs from the
connector-registry version slot (``3``) -- the latter is constrained by
:func:`~meho_backplane.operations._lookup.parse_connector_id`'s
digit-prefix regex (``^[0-9][A-Za-z0-9._]*$``) while the catalog YAML
preserves the operator-visible "v3" label GitHub itself uses for its
REST API. The helper takes the catalog label as input (not the registry
slot) so a future version bump ships in one place.
"""

from __future__ import annotations

__all__ = ["catalog_command_for_github_rest"]


def catalog_command_for_github_rest(catalog_version: str = "v3") -> str:
    """Return the ``meho connector ingest --catalog gh/<version>`` command.

    Parameters
    ----------
    catalog_version:
        The catalog row's ``version`` label (``"v3"`` for the v0.7.x
        default that matches the row in ``catalog.yaml`` G3.11-T3
        #1228). Pass-through to the ``--catalog`` argument value
        (``gh/v3``). Distinct from the registry's digit-prefix version
        slot (``"3"``) the parser requires; the catalog label is what
        operators type at the CLI.

    Returns
    -------
    str
        The exact CLI invocation an operator should run, with the
        catalog argument resolved. Used in the
        :class:`CompositeL2DependencyMissing` exception text and in the
        :func:`~meho_backplane.operations._errors.result_composite_l2_missing`
        result's structured ``catalog_command`` field.
    """
    return f"meho connector ingest --catalog gh/{catalog_version}"
