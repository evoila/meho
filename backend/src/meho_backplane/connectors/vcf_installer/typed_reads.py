# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`InstallerConnector`.

The bring-up *poll* primitives — the bring-up task status and the spec
validation status — plus the air-gapped depot-lifecycle *reads* (#3121:
depot settings, trusted certificates, bundle inventory) are registered as
typed ops (``source_kind="typed"``) so they dispatch on a fresh boot with
zero catalog state. Each function here is the body a thin bound-method shim
on :class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.vcf_installer.typed_ops`, and the write twins
(``spec.validate`` / ``bringup.start`` / ``bringup.retry`` / ``depot.set`` /
``trusted-certificates.add`` / ``bundles.download``) live in
:mod:`.typed_writes`.

Each read is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._get_json`. A raw ``401`` (the Installer's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` to the
dispatcher's #2067 recovery arm, which evicts the cached session token via
the connector's public :meth:`InstallerConnector.invalidate_session` hook and
re-dispatches once — so the handlers do not wrap the call in the internal
``_get_json_with_session_retry`` helper (that helper serves the
fingerprint/probe path, which the dispatcher does not drive).

**Secret hygiene:** the vendor ``DepotSettings`` object carries account
``password`` fields. :func:`installer_depot_settings_get_impl` scrubs every
``password`` key from the response before returning — whatever the appliance
echoes, no depot credential ever crosses the governed surface on a read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "installer_bundles_list_impl",
    "installer_depot_settings_get_impl",
    "installer_sddc_bringup_status_impl",
    "installer_sddc_validation_status_impl",
    "installer_trusted_certificates_list_impl",
    "scrub_password_keys",
]

#: The bring-up status vendor path (``{id}`` substituted at call time). A module
#: constant so the spec-reconcile lane introspects it by value — the #2944
#: pattern — instead of mirroring a literal.
_SDDC_STATUS_PATH = "/v1/sddcs/{id}"

#: The spec-validation status vendor path (``{id}`` substituted at call time).
_VALIDATION_STATUS_PATH = "/v1/sddcs/validations/{id}"

#: Depot settings (``DepotSettings``: vmwareAccount / offlineAccount /
#: depotConfiguration) — the read half of #3121's depot pair.
_DEPOT_SETTINGS_PATH = "/v1/system/settings/depot"

#: Appliance outbound trust store (``TrustedCertificate`` page) — the read
#: half of #3121's trusted-certificates pair.
_TRUSTED_CERTIFICATES_PATH = "/v1/sddc-manager/trusted-certificates"

#: Bundle inventory (``PageOfBundle``; populated only after a depot connect +
#: catalog ingest) — the read half of #3121's bundle pair.
_BUNDLES_PATH = "/v1/bundles"


def scrub_password_keys(value: Any) -> Any:
    """Recursively drop every ``password`` key from *value* (dicts/lists).

    The vendor ``DepotSettings`` echo nests credentials under
    ``vmwareAccount`` / ``offlineAccount``; scrubbing by key at every depth is
    redaction that cannot rot when the vendor moves the field.
    """
    if isinstance(value, dict):
        return {k: scrub_password_keys(v) for k, v in value.items() if k != "password"}
    if isinstance(value, list):
        return [scrub_password_keys(v) for v in value]
    return value


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


async def installer_depot_settings_get_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.depot.get`` — read depot settings via
    ``GET /v1/system/settings/depot``.

    Returns the vendor ``DepotSettings`` (``vmwareAccount`` /
    ``offlineAccount`` / ``depotConfiguration``) with every ``password`` key
    scrubbed at every depth — the account *status*/*message* fields (e.g.
    ``DEPOT_CONNECTION_SUCCESSFUL``) survive, credentials never do.
    """
    payload = await connector._get_json(target, _DEPOT_SETTINGS_PATH, operator=operator)
    return cast("dict[str, Any]", scrub_password_keys(payload))


async def installer_trusted_certificates_list_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.trusted-certificates.list`` — read the appliance
    outbound trust store via ``GET /v1/sddc-manager/trusted-certificates``.

    Returns the vendor page of trusted certificates (public material — no
    scrubbing needed).
    """
    return await connector._get_json(target, _TRUSTED_CERTIFICATES_PATH, operator=operator)


async def installer_bundles_list_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.bundles.list`` — read the bundle inventory via
    ``GET /v1/bundles``.

    Returns the vendor ``PageOfBundle`` (``elements[]`` with per-bundle
    ``id`` / ``version`` / ``downloadStatus`` / ``components``). The
    inventory is empty until a depot connect has ingested a catalog; after a
    connect it typically populates within ~a minute. The set is release-sized
    (a 9.1 depot advertises ~180 bundles), so callers drill via the
    ``downloadStatus`` / ``version`` fields rather than dumping elements.
    """
    return await connector._get_json(target, _BUNDLES_PATH, operator=operator)
