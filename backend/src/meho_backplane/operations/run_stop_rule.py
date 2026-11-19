# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may stop a hosted agent run or a background operation run.

One rule for both kinds of run:

* The principal the run acts for (its ``identity_sub``) may stop it. A
  scheduled or event run acts for the agent's own login, so that login
  may stop it.
* A person with the ``tenant_admin`` role, or a platform admin, may stop
  any run of the tenant.
* Everyone else is refused: an agent or service login stops only its own
  runs, and an operator stops only their own runs.

The role floor of each run kind (``operator`` or more) is checked before
this rule.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal

__all__ = ["MACHINE_LOGIN_REASON", "PERSON_REASON", "run_stop_refusal"]

#: The refusal for an agent or service login and a run of another principal.
MACHINE_LOGIN_REASON: Final[str] = (
    "an agent or service login may stop only the runs it started itself"
)

#: The refusal for a person who neither started the run nor is an admin.
PERSON_REASON: Final[str] = "only the person who started this run, or a tenant admin, may stop it"


def run_stop_refusal(operator: Operator, owner_sub: str | None) -> str | None:
    """Why *operator* may not stop a run that acts for *owner_sub*; ``None`` = allowed.

    *owner_sub* is the run's ``identity_sub``. ``None`` (a row written
    before the column was filled) has no owner, so only an admin may stop
    it.
    """
    if owner_sub is not None and owner_sub == operator.sub:
        return None
    if not is_human_principal(operator):
        return MACHINE_LOGIN_REASON
    if operator.tenant_role is TenantRole.TENANT_ADMIN or operator.platform_admin:
        return None
    return PERSON_REASON
