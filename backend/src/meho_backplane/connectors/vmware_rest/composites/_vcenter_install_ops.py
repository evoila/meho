# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the three vCenter install composites (#4394).

``_register.py`` collects :data:`COMPOSITES`, and ``_governed_subops.py`` reads
:data:`VCENTER_INSTALL_GOVERNED_SUBOPS`. All three changes are ``dangerous``
with ``requires_approval=True``. The deploy and the install send a password
from Vault to vCenter, so ``broadcast/events.py`` pins them as
``credential_write``: the flight recorder keeps no request body of them, the
dispatcher keeps no vendor text of a failed call, and the broadcast carries
no params.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import (
    _vcenter_answer,
    _vcenter_deploy,
    _vcenter_install,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._vcenter_deploy_schemas import (
    APPLIANCE_DEPLOY_LLM_INSTRUCTIONS,
    APPLIANCE_DEPLOY_PARAMETER_SCHEMA,
    APPLIANCE_DEPLOY_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._vcenter_install_schemas import (
    DEPLOYMENT_ANSWER_LLM_INSTRUCTIONS,
    DEPLOYMENT_ANSWER_PARAMETER_SCHEMA,
    DEPLOYMENT_ANSWER_RESPONSE_SCHEMA,
    INSTALL_LLM_INSTRUCTIONS,
    INSTALL_PARAMETER_SCHEMA,
    INSTALL_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import INSTALL_GROUP_KEY

__all__ = ["COMPOSITES", "VCENTER_INSTALL_GOVERNED_SUBOPS"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=_vcenter_deploy.APPLIANCE_DEPLOY_OP_ID,
        handler=_vcenter_deploy.vcenter_appliance_deploy_composite,
        summary="Import the vCenter appliance file onto a cluster or host (install step 1).",
        description=(
            "Imports the vCenter appliance file (OVA) from a content library onto a cluster "
            "or host of this vCenter, with the size, disk mode, network settings and host name, "
            "then powers it on. The root password comes from Vault. Use async mode: the copy "
            "takes long."
        ),
        parameter_schema=APPLIANCE_DEPLOY_PARAMETER_SCHEMA,
        response_schema=APPLIANCE_DEPLOY_RESPONSE_SCHEMA,
        group_key=INSTALL_GROUP_KEY,
        tags=["composite", "write", "vcenter-install", "content-library"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=APPLIANCE_DEPLOY_LLM_INSTRUCTIONS,
        preview=_vcenter_deploy.vcenter_appliance_deploy_preview,
    ),
    CompositeSpec(
        op_id=_vcenter_install.INSTALL_OP_ID,
        handler=_vcenter_install.vcenter_install_composite,
        summary="Check the install spec of a new vCenter appliance and start its setup.",
        description=(
            "On the new appliance target: checks the install spec, then starts the setup with "
            "a new SSO domain or a join to an existing one. The SSO password comes from Vault. "
            "Returns accepted at once; follow it with vmware.vcenter.deployment.status."
        ),
        parameter_schema=INSTALL_PARAMETER_SCHEMA,
        response_schema=INSTALL_RESPONSE_SCHEMA,
        group_key=INSTALL_GROUP_KEY,
        tags=["composite", "write", "vcenter-install"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=INSTALL_LLM_INSTRUCTIONS,
        preview=_vcenter_install.vcenter_install_preview,
    ),
    CompositeSpec(
        op_id=_vcenter_answer.ANSWER_OP_ID,
        handler=_vcenter_answer.vcenter_deployment_answer_composite,
        summary="Answer a question the setup of a new vCenter appliance asks.",
        description=(
            "On the new appliance target: answers one open question of the setup (for "
            "example: trust this certificate?), so the setup goes on. The answer must be one "
            "of the choices the question offers; MEHO checks this first."
        ),
        parameter_schema=DEPLOYMENT_ANSWER_PARAMETER_SCHEMA,
        response_schema=DEPLOYMENT_ANSWER_RESPONSE_SCHEMA,
        group_key=INSTALL_GROUP_KEY,
        tags=["composite", "write", "vcenter-install"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=DEPLOYMENT_ANSWER_LLM_INSTRUCTIONS,
        preview=_vcenter_answer.vcenter_deployment_answer_preview,
        classifier_other=True,
    ),
)

#: composite op id -> the vendor calls it may make (REST and VI-JSON).
VCENTER_INSTALL_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _vcenter_deploy.APPLIANCE_DEPLOY_OP_ID: (
        _vcenter_deploy._SUB_OPS_VCENTER_APPLIANCE_DEPLOY
        + _vcenter_deploy._VIM_SUB_OPS_VCENTER_APPLIANCE_DEPLOY
    ),
    _vcenter_install.INSTALL_OP_ID: _vcenter_install._SUB_OPS_VCENTER_INSTALL,
    _vcenter_answer.ANSWER_OP_ID: _vcenter_answer._SUB_OPS_VCENTER_DEPLOYMENT_ANSWER,
}
