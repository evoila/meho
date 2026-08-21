# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) write implementations for :class:`InstallerConnector`.

The two governed bring-up *submit* primitives (#3078). The vendor bring-up
API is natively asynchronous — each POST returns its tracking object in
seconds and progress is polled via the short status GETs in
:mod:`.typed_reads` — so every call here is short, the standard
park → approve → resume machinery governs the write as-is, and no
long-blocking loop or async dispatch machinery exists in this module. The
multi-hour orchestration (submit → durable poll) belongs to the
deploy-automation add-on, not this connector.

* ``installer.sddc.spec.validate`` — ``POST /v1/sddcs/validations`` with the
  caller's ``SddcSpec``, returning the vendor ``Validation`` object (poll
  ``installer.sddc.validation.status`` to a terminal ``executionStatus``).
  A dry-run on the appliance that mutates no estate —
  ``safety_level="caution"``, no approval.
* ``installer.sddc.bringup.start`` — ``POST /v1/sddcs`` with the
  ``SddcSpec``, returning the ``SddcTask`` the moment the deploy is accepted
  (poll ``installer.sddc.bringup.status``). The estate mutation —
  ``safety_level="dangerous"`` + ``requires_approval=True``: the dispatcher
  parks the call for approval *before* this handler runs, and the park-time
  preview (registered in :mod:`.bringup`, shared with the composite) echoes
  SDDC identity + network blast-radius only, never a password.

Each write is issued directly on the connector's own authenticated token
session via :meth:`HttpConnector._post_json`. A raw ``401`` (the Installer's
expired-token signal) propagates as :class:`httpx.HTTPStatusError` to the
dispatcher's #2067 recovery arm, which evicts the cached session token via
the connector's public :meth:`InstallerConnector.invalidate_session` hook and
re-dispatches once. That retry is safe even for these non-idempotent POSTs:
a ``401`` means the request was rejected at auth *before* the server
processed it, so the first attempt had no effect — the same argument
:meth:`InstallerConnector._post_json_with_session_retry` documents for the
composite's internal sub-calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "INSTALLER_BRINGUP_START_OP_ID",
    "INSTALLER_SPEC_VALIDATE_OP_ID",
    "TYPED_WRITE_DECLARED_OP_IDS",
    "installer_sddc_bringup_start_impl",
    "installer_sddc_spec_validate_impl",
]

#: ``installer.sddc.spec.validate`` — submit an ``SddcSpec`` validation dry-run.
INSTALLER_SPEC_VALIDATE_OP_ID: Final[str] = "installer.sddc.spec.validate"
#: ``installer.sddc.bringup.start`` — start the management-domain bring-up.
INSTALLER_BRINGUP_START_OP_ID: Final[str] = "installer.sddc.bringup.start"

# --- Hand-coded wire paths — the spec-reconcile lane's introspection source
# (module constants introspected by value, the #2944 pattern). ---
#: ``POST`` — submit an ``SddcSpec`` for a non-mutating validation dry-run.
_SPEC_VALIDATE_PATH = "/v1/sddcs/validations"
#: ``POST`` — start the bring-up (the estate mutation).
_BRINGUP_START_PATH = "/v1/sddcs"

#: The exact ``METHOD:/path`` set these write primitives hand-code. The
#: reconcile lane (:mod:`tests.test_connectors_vcf_installer_spec_reconcile`)
#: asserts each is served by the pinned 9.1 OpenAPI, and pins this set so it
#: can't go vacuous.
TYPED_WRITE_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"POST:{_SPEC_VALIDATE_PATH}",
        f"POST:{_BRINGUP_START_PATH}",
    }
)


async def installer_sddc_spec_validate_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.spec.validate`` — ``POST /v1/sddcs/validations``.

    Submits ``params["spec"]`` (validated as a JSON object by the op's
    parameter schema before dispatch) verbatim and returns the vendor
    ``Validation`` — typically ``202`` with ``executionStatus="IN_PROGRESS"``
    and the ``id`` to poll via ``installer.sddc.validation.status``. Mutates
    nothing on the estate.
    """
    return await connector._post_json(
        target, _SPEC_VALIDATE_PATH, operator=operator, json=params["spec"]
    )


async def installer_sddc_bringup_start_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.bringup.start`` — ``POST /v1/sddcs``.

    Submits ``params["spec"]`` verbatim and returns the vendor ``SddcTask``
    the moment the deploy is accepted (``202``) — the bring-up itself runs
    for hours on the appliance; poll ``installer.sddc.bringup.status`` with
    the returned ``id`` to a terminal state. The op is registered
    ``dangerous`` + ``requires_approval=True``, so the dispatcher has already
    parked and an approver has already resumed this call by the time the
    handler runs.
    """
    return await connector._post_json(
        target, _BRINGUP_START_PATH, operator=operator, json=params["spec"]
    )
