# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which credential path a machine login may give a new target in ``targets.register``.

A new target stores a credential path (``secret_ref``). A later call on the
target reads the credential at that path and uses it with the target's
``host``. So the pair of host and path decides where a stored credential
goes.

A person (a ``user`` login) may register any path inside the tenant. The
call of a machine login (an agent, a service login, a runner) can run
without a person: a standing grant or an approved multi-step run covers
it. Such a call may use only the new target's own path,
``tenants/<tenant_id>/<name>``
(:func:`~meho_backplane.connectors.vault.tenant_paths.tenant_secret_ref`),
or no path at all (``secret_ref: null``). The own path must also not be
the path of another target of the tenant, live or removed. Every other
path needs a person to approve this one call: the call then runs with
the verdict ``needs-approval`` in
:data:`~meho_backplane.operations._audit.policy_decision_var`, and the
check lets it pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
from meho_backplane.db.models import PermissionVerdict, Target
from meho_backplane.operations._audit import policy_decision_var

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator
    from meho_backplane.targets.schemas import TargetCreate

__all__ = ["machine_secret_ref_refusal"]


def _same_path_form(path: str) -> str:
    """Return *path* without blanks and empty segments, to compare two paths."""
    return "/".join(segment for segment in path.strip().split("/") if segment)


def _person_decides(operator: Operator) -> bool:
    """Whether a person stands behind this call: a person's own call, or one a person approved."""
    if operator.principal_kind is PrincipalKind.USER:
        return True
    return policy_decision_var.get() == PermissionVerdict.NEEDS_APPROVAL.value


async def machine_secret_ref_refusal(
    session: AsyncSession, operator: Operator, body: TargetCreate
) -> str | None:
    """Return why MEHO refuses the credential path of this registration, or ``None``.

    ``None`` for a person's call, for a call a person approved, for a call
    with no path, and for the new target's own path when no other target
    of the tenant uses it. Every other machine-login call gets a refusal
    that names the path and what to do instead.
    """
    if _person_decides(operator):
        return None
    own = tenant_secret_ref(operator.tenant_id, body.name)
    ref = body.secret_ref if "secret_ref" in body.model_fields_set else own
    if ref is None:
        return None
    if _same_path_form(ref) != _same_path_form(own):
        return (
            f"secret_ref {ref!r} is not the new target's own credential path {own!r}. "
            "A machine login can register a target only with its own path or with "
            "no path, unless a person approves the call. Leave out secret_ref, or "
            "ask a person to register the target."
        )
    rows = await session.scalars(
        select(Target.secret_ref).where(
            Target.tenant_id == operator.tenant_id,
            Target.secret_ref.is_not(None),
        )
    )
    wanted = _same_path_form(own)
    if any(_same_path_form(stored) == wanted for stored in rows if stored is not None):
        return (
            f"the credential path {own!r} already belongs to another target of the "
            "tenant. A machine login cannot register a target on it unless a person "
            "approves the call. Pick another name, or ask a person to register the target."
        )
    return None
