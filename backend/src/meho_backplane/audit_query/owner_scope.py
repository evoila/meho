# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which audit rows a caller may read: a service login reads only its own.

A person reads every audit row of the tenant. A service login (an agent, a
runner, an add-on, any client-credentials login) reads only the rows of its
own calls: the rows whose ``operator_sub`` is the login's own ``sub``. The
rule holds on every audit read (list, count, filter, single row, session
tree), whatever role the login carries.

The ``shape="tree"`` replay of ``query_audit`` holds only the rows of the
caller's own calls, for a person too. Each stored row names the login of the
checked token, so a session id the client sends opens no row of another
login.
"""

from __future__ import annotations

from meho_backplane.auth.operator import Operator, is_human_principal

__all__ = ["own_rows_sub"]


def own_rows_sub(operator: Operator) -> str | None:
    """The login id *operator*'s audit reads are limited to, or ``None``.

    ``None`` for a person (``principal_kind=user``): a person reads every
    row of the tenant. The login's own ``sub`` for every machine kind
    (agent, service, runner), so the read returns only its own rows.
    """
    if is_human_principal(operator):
        return None
    return operator.sub
