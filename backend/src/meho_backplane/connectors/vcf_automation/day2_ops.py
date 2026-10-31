# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the day-2 ops, written once for two connectors.

Aria Automation 8.18 and VCF Automation 9 serve the same deployment and
IaaS APIs. :func:`day2_specs` builds the op metadata for one op id prefix;
each connector turns the specs into its own op type:

* ``vcfa-rest-9.0`` -- prefix ``vcfa.tenant`` (:data:`VCFA_DAY2_OPS`, and
  the delete in :mod:`.deploy_ops`).
* ``vcfa-vra8-8.0`` -- prefix ``vcfa.vra8`` (``VRA8_DAY2_OPS`` in
  :mod:`meho_backplane.connectors.vra8.typed_ops`).

The ops (suffix after the prefix):

* ``deployment.actions.list`` -- safe
* ``deployment.action.run`` -- dangerous, approval (built-in actions only)
* ``resource.action.run`` -- dangerous, approval (built-in machine actions only)
* ``deployment.lease.change`` -- caution, approval
* ``deployment.delete`` -- destructive, approval, preview first
* ``machine.list`` / ``block_device.list`` -- safe
* ``machine.create`` / ``block_device.create`` -- dangerous, approval

Handlers live in :mod:`._day2`; the specs in :mod:`._day2_specs` (deployments)
and :mod:`._day2_specs_iaas` (machines and disks).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation._day2_specs import Day2OpSpec, deployment_specs
from meho_backplane.connectors.vcf_automation._day2_specs_iaas import iaas_specs
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = [
    "VCFA_DAY2_GROUP",
    "VCFA_DAY2_OPS",
    "VCFA_DEPLOYMENT_DELETE",
    "Day2OpSpec",
    "day2_specs",
    "vcfa_op",
]

VCFA_DAY2_GROUP: Final[str] = "vcfa-tenant-day2"


def day2_specs(prefix: str) -> tuple[Day2OpSpec, ...]:
    """Every day-2 op spec, with every op id in the texts under *prefix*."""
    return (*deployment_specs(prefix), *iaas_specs(prefix))


def vcfa_op(spec: Day2OpSpec, group_key: str) -> VcfaTypedOp:
    """A ``vcfa-rest`` op (``vcfa.tenant.*``) from one spec; the handler gets ``tenant_``."""
    return VcfaTypedOp(
        op_id=f"vcfa.tenant.{spec.name}",
        handler_attr=f"tenant_{spec.handler}",
        plane="tenant",
        path=spec.path,
        summary=spec.summary,
        description=spec.description,
        parameter_schema=spec.parameter_schema,
        response_schema=spec.response_schema,
        group_key=group_key,
        tags=(*spec.tags, "tenant"),
        safety_level=spec.safety_level,
        requires_approval=spec.requires_approval,
        llm_instructions=spec.llm_instructions,
    )


_VCFA_SPECS: Final = {spec.name: spec for spec in day2_specs("vcfa.tenant")}

#: The ``vcfa-rest`` day-2 ops. The delete is ``VCFA_DEPLOY_OPS`` (it was there first).
VCFA_DAY2_OPS: Final[tuple[VcfaTypedOp, ...]] = tuple(
    vcfa_op(spec, VCFA_DAY2_GROUP)
    for name, spec in _VCFA_SPECS.items()
    if name != "deployment.delete"
)
#: The ``vcfa-rest`` deployment delete, written from the shared spec.
VCFA_DEPLOYMENT_DELETE: Final[VcfaTypedOp] = vcfa_op(
    _VCFA_SPECS["deployment.delete"], "vcfa-tenant-writes"
)

validate_typed_ops(VCFA_DAY2_OPS)
validate_typed_ops((VCFA_DEPLOYMENT_DELETE,))
