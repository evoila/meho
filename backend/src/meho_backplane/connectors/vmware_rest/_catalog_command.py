# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog-command helper for the vmware-rest connector.

G0.14-T10 (#1151). The
:func:`~meho_backplane.connectors.vmware_rest.composites._preflight.preflight_l2_dependencies`
helper raises a
:class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
exception when one of a composite's L2 sub-ops is not registered.
That exception carries the operator-facing CLI command to run to
ingest the catalog entry that lands the missing ops.

This module owns the per-(product, version) command construction so a
future version label change ships in one place rather than scattered
across every composite handler. The shape is the operator-visible
CLI verb shipped in #405 (G0.7-T5) and re-affirmed by T9 (#1150)
once server-side catalog-driven ingest is the canonical entrypoint:

    meho connector ingest --catalog vmware/9.0

Single-version v0.6.x deploys hardcode ``9.0``; the module exposes a
``version``-parameterised entrypoint so the helper can grow alongside
the catalog without changing call sites.
"""

from __future__ import annotations

__all__ = ["catalog_command_for_vmware_rest"]


def catalog_command_for_vmware_rest(version: str) -> str:
    """Return the ``meho connector ingest --catalog ...`` command for vmware/*.

    Parameters
    ----------
    version:
        The connector version label (``"9.0"`` for the v0.6.x default).
        Pass-through to the ``--catalog`` argument value (``vmware/9.0``).

    Returns
    -------
    str
        The exact CLI invocation an operator should run, with the
        catalog argument resolved. Used in the
        :class:`CompositeL2DependencyMissing` exception text and in the
        :func:`~meho_backplane.operations._errors.result_composite_l2_missing`
        result's structured ``catalog_command`` field.

    Notes
    -----
    The command shape matches the verb shipped in #405 (G0.7-T5)
    documented in
    ``backend/src/meho_backplane/operations/ingest/catalog.yaml``'s
    header comment. Operators with the in-cluster CLI verb available
    can run this verbatim; operators using ``POST
    /api/v1/connectors/ingest`` directly should land T9 (#1150)
    ``{"catalog_entry": "vmware/<version>"}`` instead.

    Build-time-only caveat (G0.18-T7 #1360): non-dry-run ingest of an
    un-grouped catalog entry needs an injected ``LlmClient`` for the
    grouping pass, and the chassis ships no production adapter
    today -- on deployed backplanes the returned command fails closed
    with HTTP 503 / ``LlmClientUnavailable`` until an operator wires
    :func:`meho_backplane.api.v1.connectors_ingest.set_llm_client_factory`
    at FastAPI lifespan startup. The
    :func:`~meho_backplane.operations._errors.result_composite_l2_missing`
    envelope's human message names this limitation so operators don't
    follow the suggested command into the 503; see
    ``docs/codebase/spec-ingestion.md`` section "LLM-client wiring
    (build-time-only today)".
    """
    return f"meho connector ingest --catalog vmware/{version}"
