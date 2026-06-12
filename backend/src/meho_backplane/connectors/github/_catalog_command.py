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
version=3, impl_id=gh-rest`` (see
``backend/src/meho_backplane/operations/ingest/catalog.yaml`` row added
by G3.11-T3 #1228 and canonicalised by G3.11-T8 #1242 -- Resolution A
aligned the catalog ``version`` field with the registry's digit-prefix
form so the dispatcher's tuple lookup against ingested rows resolves
cleanly). The operator command shape matches the verb shipped in
#405 (G0.7-T5) and re-affirmed by T9 (#1182):

    meho connector ingest --catalog gh/3

Pre-T8 #1242 the catalog version label was ``v3`` (the upstream API
label) and the registry slot was ``3``; the helper bridged the two by
taking the catalog label as input. Post-T8 both are ``"3"`` -- the
helper still takes the catalog label as a parameter (not the registry
slot) for forward-compatibility with future version bumps, but the
default value tracks the catalog's canonical form. The upstream "v3"
label is preserved in :class:`FingerprintResult.version` and in the
catalog row's ``notes`` for operator recognition.
"""

from __future__ import annotations

__all__ = ["catalog_command_for_github_rest"]


def catalog_command_for_github_rest(catalog_version: str = "3") -> str:
    """Return the ``meho connector ingest --catalog gh/<version>`` command.

    Parameters
    ----------
    catalog_version:
        The catalog row's ``version`` label (``"3"`` for the v0.7.x
        default that matches the row in ``catalog.yaml`` G3.11-T3
        #1228 as canonicalised by G3.11-T8 #1242). Pass-through to
        the ``--catalog`` argument value (``gh/3``); the default
        matches the post-T8 catalog form so operators ingest the
        same triple the dispatcher resolves.

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
