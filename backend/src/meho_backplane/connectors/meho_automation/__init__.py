# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingest-time curation for the meho-automation add-on generic connector.

The meho-automation add-on is registered as a **profile-backed generic
connector** (catalog product ``mehoauto`` / version ``0.1.0`` / impl
``mehoauto-rest``, ExecutionProfile
``connectors/profiles/meho_automation_minimal.yaml``). It has **no
hand-coded connector class** — the dispatchable
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` is
synthesised from the shipped profile at boot by
:func:`~meho_backplane.operations.ingest.boot_stamp.stamp_catalog_profiled_connectors`,
and its ops are ingested at registration time from the add-on's published
``/openapi.json`` (no spec is vendored in this repo).

This package therefore exists **only** to register the connector-owned
ingest safety floor (:mod:`.ingest_safety`), the same way
:mod:`meho_backplane.connectors.vmware_rest` registers its floor as an
import side effect. It is eager-imported at boot by
:func:`~meho_backplane.connectors.registry._eager_import_connectors` (which
imports every ``connectors/<product>/`` subpackage); the floor registration
fires as a top-level import side effect of :mod:`.ingest_safety`. There is
no ``register_connector_v2`` call here — a profile-backed connector's class
is stamped from the catalog, not hand-registered.
"""

from __future__ import annotations

# Side-effect import: registers the ingest safety floor for the
# (mehoauto, mehoauto-rest) connector at module import.
from meho_backplane.connectors.meho_automation import ingest_safety as _ingest_safety  # noqa: F401
