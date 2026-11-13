# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``target_secret_not_found`` dispatch result.

Vault has no secret at the target's own ``secret_ref``. The result names
the target and the ref, says whether MEHO built the ref from the target
name (a target added without a ``secret_ref`` gets
``tenants/<tenant_id>/<name>``), and says how to fix it. It never carries
the hvac text or the Vault URL.
"""

from __future__ import annotations

from uuid import UUID

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors._shared.vault_creds import TargetSecretNotFoundError

__all__ = ["result_target_secret_not_found", "target_secret_not_found_parts"]


def target_secret_not_found_parts(
    exc: TargetSecretNotFoundError, *, tenant_id: UUID
) -> tuple[bool, str, str]:
    """Return ``(derived, message, remediation)`` for a target with no secret.

    Shared by the dispatch result and the probe route, so both say the same
    thing. *derived* is true when MEHO built the ref from the target name.
    """
    # Call-time import: ``tenant_paths`` pulls in the Vault ops module, and
    # the dispatcher that imports this module stays light to import.
    from meho_backplane.connectors.vault.tenant_paths import is_derived_secret_ref

    derived = is_derived_secret_ref(
        exc.secret_ref, tenant_id=tenant_id, target_name=exc.target_name
    )
    origin = (
        "This is the default path MEHO builds from the target name for a "
        "target added without a secret_ref."
        if derived
        else "This is the secret_ref set on the target."
    )
    remediation = (
        f"Store the credential in Vault at {exc.secret_ref!r}, or point the "
        f"target at the path where the credential is: "
        f"`meho targets update {exc.target_name} --secret-ref <path>`. "
        "Then run the operation again."
    )
    message = (
        f"target_secret_not_found: Vault has no secret at secret_ref "
        f"{exc.secret_ref!r} of target {exc.target_name!r}. {origin} "
        f"The operation did not run. {remediation}"
    )
    return derived, message, remediation


def result_target_secret_not_found(
    op_id: str,
    exc: TargetSecretNotFoundError,
    *,
    tenant_id: UUID,
    duration_ms: float,
) -> OperationResult:
    """Build the result for a target whose ``secret_ref`` has no secret in Vault."""
    derived, message, remediation = target_secret_not_found_parts(exc, tenant_id=tenant_id)
    return OperationResult(
        status="error",
        op_id=op_id,
        error=message,
        duration_ms=duration_ms,
        extras={
            "error_code": "target_secret_not_found",
            "target": exc.target_name,
            "secret_ref": exc.secret_ref,
            "secret_ref_derived": derived,
            "remediation": remediation,
        },
    )
