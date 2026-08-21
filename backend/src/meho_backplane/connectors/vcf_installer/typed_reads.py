# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`InstallerConnector`.

The two bring-up *poll* primitives — the bring-up task status and the spec
validation status — are registered as typed ops (``source_kind="typed"``) so
they dispatch on a fresh boot with zero catalog state. Each function here is
the body a thin bound-method shim on
:class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.vcf_installer.typed_ops`, and the submit twins
(``spec.validate`` / ``bringup.start``) live in :mod:`.typed_writes`.

Each read is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._get_json`. A raw ``401`` (the Installer's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` to the
dispatcher's #2067 recovery arm, which evicts the cached session token via
the connector's public :meth:`InstallerConnector.invalidate_session` hook and
re-dispatches once — so the handlers do not wrap the call in the internal
``_get_json_with_session_retry`` helper (that helper serves the
fingerprint/probe path, which the dispatcher does not drive).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "installer_sddc_bringup_status_impl",
    "installer_sddc_validation_status_impl",
]

#: The bring-up status vendor path (``{id}`` substituted at call time). A module
#: constant so the spec-reconcile lane introspects it by value — the #2944
#: pattern — instead of mirroring a literal.
_SDDC_STATUS_PATH = "/v1/sddcs/{id}"

#: The spec-validation status vendor path (``{id}`` substituted at call time).
_VALIDATION_STATUS_PATH = "/v1/sddcs/validations/{id}"


async def installer_sddc_bringup_status_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.bringup.status`` — read one bring-up task via ``GET /v1/sddcs/{id}``.

    The ``id`` param is validated non-empty by the op's parameter schema before
    dispatch; it is path-substituted into the vendor path. Returns the raw
    ``SddcTask`` object (top-level ``status`` + ``sddcSubTasks`` / ``milestones``).
    """
    sddc_id = str(params["id"])
    return await connector._get_json(
        target, _SDDC_STATUS_PATH.replace("{id}", sddc_id), operator=operator
    )


async def installer_sddc_validation_status_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.validation.status`` — read one spec validation via
    ``GET /v1/sddcs/validations/{id}``.

    The ``id`` param (returned by ``installer.sddc.spec.validate``) is
    validated non-empty by the op's parameter schema before dispatch. Returns
    the raw ``Validation`` object — ``executionStatus`` carries the
    ``IN_PROGRESS`` / ``COMPLETED`` lifecycle, ``resultStatus`` the
    ``SUCCEEDED`` / ``WARNING`` / ``FAILED`` verdict, ``validationChecks[]``
    the per-check detail.
    """
    validation_id = str(params["id"])
    return await connector._get_json(
        target, _VALIDATION_STATUS_PATH.replace("{id}", validation_id), operator=operator
    )
