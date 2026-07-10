# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Boot-time ExecutionProfile stamping for shipped catalog rows (#2288).

Initiative #2271 / Goal #221. The profile-stamping seam
(:meth:`~meho_backplane.operations.ingest.service.ReviewService.record_profile_stamp`,
G0.28-T5 #1971) is what turns a reviewed :class:`ExecutionProfile` into a
**dispatchable**
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` registered
under the connector's ``(product, version, impl_id)`` v2 key. Until this
module it had no production caller — shipped profile-backed catalog rows were
boot-validated package data that never became connector classes, so the
#1964 arc never actually raised the ingest ceiling.

:func:`stamp_catalog_profiled_connectors` closes that gap: at boot, once
:func:`~meho_backplane.operations.ingest.catalog.validate_shipped_artifacts`
has proven every shipped spec / profile parses, it walks every catalog row
carrying a ``profile_resource``, synthesises a ``ProfiledRestConnector``
subclass from the (already-validated) profile, and stamps it. The result: a
fresh deploy has profiled connector classes registered and resolvable for
every profile-backed catalog row.

Two invariants are load-bearing:

* **Stamping never enables dispatch.** ``record_profile_stamp`` registers the
  class but leaves every op ``is_enabled=False`` / ``review_status='staged'``;
  the review gate (#1971) stays the interlock. Boot stamping is registration,
  not enablement.
* **Occupied triples no-op.** A ``(product, version, impl_id)`` already served
  by a hand-coded class (vmware's ``VmwareRestConnector``, sddc's
  ``SddcManagerConnector`` until T4) or an existing profiled class is left
  untouched — ``record_profile_stamp`` returns ``False`` and this module logs
  it at debug, not as an error. The tri-state resolver keeps the hand-coded
  class winning regardless (#1750/#1798), so the no-op is correct, not a gap.

The stamp path does **no** network I/O: it reads package-data bytes (already
in memory from the boot validator's read), parses YAML, synthesises a class,
and writes one audit row per newly-registered triple. Boot-time cost is
negligible.
"""

from __future__ import annotations

import uuid

import structlog
import yaml

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.profile import ExecutionProfile
from meho_backplane.operations.ingest.catalog import (
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_catalog,
    load_profile_resource,
)
from meho_backplane.operations.ingest.connector_registration import synthesise_profiled_class
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "BOOT_STAMP_OPERATOR_SUB",
    "stamp_catalog_profiled_connectors",
]

_log = structlog.get_logger(__name__)

#: ``operator_sub`` the boot-time stamp audit rows attribute to. A profile
#: stamp is a system action, not an operator review action, so it carries a
#: stable synthetic sub (mirroring the ``system:*`` subs the topology
#: scheduler and memory-expiry sweeper use) rather than a real principal.
BOOT_STAMP_OPERATOR_SUB = "system:boot-profile-stamp"


def _boot_operator() -> Operator:
    """Build the synthetic operator the boot-time stamp runs as.

    ``raw_jwt`` is empty — the stamp reads no per-target vendor credential, so
    it never forwards a token downstream. ``TENANT_ADMIN`` is required because
    the stamp targets the built-in (``tenant_id=None``) scope, which
    :meth:`ReviewService._authorize_scope` gates on ``tenant_admin``; the
    ``tenant_id=uuid.UUID(int=0)`` nil sentinel is the established
    system-context tenant (``audit_log.tenant_id`` carries no FK, so the nil
    sentinel is safe for the attribution-only audit row).
    """
    return Operator(
        sub=BOOT_STAMP_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _stamp_entry(service: ReviewService, entry: ConnectorSpecEntry) -> bool:
    """Synthesise + stamp the profiled connector for one profile-backed *entry*.

    Returns ``True`` when a new profiled class was registered, ``False`` when
    the triple was already occupied (idempotent no-op). Derives the
    dispatch-canonical triple the way ``record_profile_stamp`` does (from the
    connector_id) so the synthesised class's resolver-facing attributes agree
    with the registry key it lands under — the catalog's raw ``product`` may
    not round-trip (the ``_fixture`` mechanism row parses to ``fixture``).
    """
    assert entry.profile_resource is not None  # caller-guaranteed
    raw = load_profile_resource(entry.profile_resource)
    profile = ExecutionProfile.model_validate(yaml.safe_load(raw))
    connector_id = f"{entry.impl_id}-{entry.version}"
    product, version, impl_id = parse_connector_id(connector_id)
    connector_class = synthesise_profiled_class(
        product=product, version=version, impl_id=impl_id, profile=profile
    )
    registered = await service.record_profile_stamp(
        connector_id, tenant_id=None, connector_class=connector_class
    )
    fields = {
        "connector_id": connector_id,
        "product": product,
        "version": version,
        "impl_id": impl_id,
        "profile_resource": entry.profile_resource,
    }
    if registered:
        _log.info("boot_profile_stamped", connector_class=connector_class.__name__, **fields)
    else:
        _log.debug("boot_profile_stamp_skipped_occupied", **fields)
    return registered


async def stamp_catalog_profiled_connectors(
    catalog: ConnectorSpecCatalog | None = None,
    *,
    operator: Operator | None = None,
) -> int:
    """Register a ``ProfiledRestConnector`` per profile-backed catalog row.

    Walks *catalog* (the shipped catalog when ``None``) and stamps a
    synthesised
    :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` for every
    entry carrying a ``profile_resource``, under the built-in
    (``tenant_id=None``) scope.

    Idempotent and gated: a triple already served by a hand-coded (or already
    stamped) class no-ops (logged at debug); stamping never enables an op
    (#1971). Returns the count of rows that registered a *new* profiled class
    (occupied-triple no-ops excluded). Re-parses only the package-data bytes
    :func:`~meho_backplane.operations.ingest.catalog.validate_shipped_artifacts`
    already validated at boot — no network I/O.
    """
    cat = catalog if catalog is not None else load_catalog()
    service = ReviewService(operator if operator is not None else _boot_operator())

    stamped = 0
    for entry in cat.entries:
        if entry.profile_resource is None:
            continue
        if await _stamp_entry(service, entry):
            stamped += 1

    return stamped
