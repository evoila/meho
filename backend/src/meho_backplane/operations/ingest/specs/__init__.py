# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO-authored OpenAPI specs shipped as package data (#1964 T1 #1975).

A catalog row (:class:`~meho_backplane.operations.ingest.catalog.ConnectorSpecEntry`)
whose ``upstream`` the backend can't dereference — an HTML developer
portal (Broadcom for vmware/sddc) or an fqdn-templated appliance URL —
carries a ``spec_resource`` naming a ``.yaml`` / ``.json`` file in this
package instead. The catalog-driven ingest route loads the bytes via
:func:`importlib.resources.files` and feeds them inline through
:class:`~meho_backplane.operations.ingest.api_schemas.SpecSource.content`,
bypassing the fetch.

The directory is force-included in the wheel
(``backend/pyproject.toml`` ``[tool.hatch.build.targets.wheel.force-include]``)
so the specs survive into a deployed container, mirroring the alembic
precedent. Every shipped spec is dry-run-parsed at startup by
:func:`~meho_backplane.operations.ingest.catalog.validate_shipped_artifacts`
with the same :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`
the live ingest uses, so a malformed spec crashes boot (and CI's app-boot
smoke) rather than 500-ing the first ``--catalog`` ingest.

T1 (#1975) ships the mechanism plus ``_fixture_minimal.yaml`` — a tiny
valid OpenAPI 3.1 spec that exercises the boot-time validator end to end.
T2 (#1976) authors the real vmware/sddc specs.
"""
