# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test helper: let the console ``operator`` role check pass without a real token.

Many console tests seed a session row with a made-up access token and test
page content, not the role check. Pages behind
:func:`~meho_backplane.ui.auth.middleware.require_ui_operator` verify the
role in that token, so those tests call :func:`allow_ui_operator_role` from
an autouse fixture.

Only the ``operator`` minimum is relaxed. The ``tenant_admin`` check
(:func:`~meho_backplane.ui.auth.middleware.require_ui_admin`) still runs for
real, so a test that expects ``403`` on an admin page keeps working. The
role check itself is tested with real tokens in
``test_ui_operator_read_floor.py``.
"""

from __future__ import annotations

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.ui.auth import middleware
from meho_backplane.ui.auth.middleware import UISessionContext


def allow_ui_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``require_ui_operator`` pass for any session; keep every other role check."""
    real_check = middleware._require_ui_role

    async def _check(
        session: UISessionContext,
        *,
        minimum: TenantRole,
        detail: str,
        log_prefix: str,
    ) -> UISessionContext:
        if minimum is TenantRole.OPERATOR:
            return session
        return await real_check(session, minimum=minimum, detail=detail, log_prefix=log_prefix)

    monkeypatch.setattr(middleware, "_require_ui_role", _check)
