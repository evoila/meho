# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recovery codes for the one-time code of the two local accounts.

The realm stage makes the first admin and the second person with the
required action ``CONFIGURE_TOTP``. Two realm settings add recovery codes
(Keycloak 26.3 and later; twelve codes, each works once):

* the required action "Configure OTP" gets the option "Add recovery codes";
* the step "Recovery Authentication Code Form" of the browser flow goes from
  Disabled to Alternative.

Keycloak reads the realm file at its first start only, so the stage sets both
through the Admin REST API on every run. A run that finds both set changes
nothing. Changing them needs ``manage-realm``. An account without it gets one
line and the stage goes on.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from meho_backplane.core.keycloak_api import REALM, Admin, expect

__all__ = ["ensure_recovery_codes"]

_BASE = f"/admin/realms/{REALM}/authentication"
_BROWSER_FLOW = "browser"
_TOTP_ACTION = "CONFIGURE_TOTP"
#: Config key of the "Add recovery codes" option of "Configure OTP".
ADD_RECOVERY_CODES = "add-recovery-codes"
#: Provider of the step "Recovery Authentication Code Form".
RECOVERY_STEP = "auth-recovery-authn-code-form"


def _say(line: str) -> None:
    print(line, flush=True)


async def _ensure_option(admin: Admin) -> str:
    """Turn on "Add recovery codes" of "Configure OTP": ``kept``, ``set`` or ``no right``."""
    url = f"{_BASE}/required-actions/{_TOTP_ACTION}"
    reply = await admin.call("GET", url)
    if reply.status == 403:
        return "no right"
    action: dict[str, Any] = dict(
        expect(reply, 200, what=f"cannot read required action {_TOTP_ACTION}").data
    )
    config = dict(action.get("config") or {})
    if config.get(ADD_RECOVERY_CODES) == "true":
        return "kept"
    config[ADD_RECOVERY_CODES] = "true"
    reply = await admin.call("PUT", url, json={**action, "config": config})
    if reply.status == 403:
        return "no right"
    expect(reply, 204, what=f"cannot set recovery codes on required action {_TOTP_ACTION}")
    return "set"


async def _ensure_step(admin: Admin) -> str:
    """Set the recovery step to Alternative: ``kept``, ``set``, ``missing`` or ``no right``."""
    url = f"{_BASE}/flows/{quote(_BROWSER_FLOW, safe='')}/executions"
    reply = await admin.call("GET", url)
    if reply.status == 403:
        return "no right"
    steps = expect(reply, 200, what=f"cannot list the steps of login flow {_BROWSER_FLOW}").data
    step = next((s for s in steps if s.get("providerId") == RECOVERY_STEP), None)
    if step is None:
        return "missing"
    if step.get("requirement") == "ALTERNATIVE":
        return "kept"
    reply = await admin.call("PUT", url, json={**step, "requirement": "ALTERNATIVE"})
    if reply.status == 403:
        return "no right"
    expect(reply, 204, what="cannot set the recovery code step of the browser flow")
    return "set"


async def ensure_recovery_codes(admin: Admin) -> None:
    """Set the two realm settings for recovery codes. Safe to run again."""
    option = await _ensure_option(admin)
    step = await _ensure_step(admin)
    if "no right" in (option, step):
        _say("recovery codes: not set (no right to change login settings)")
        return
    if step == "missing":
        _say("recovery codes: not set (the browser flow has no recovery code step)")
        return
    _say(f"recovery codes: {'kept' if option == step == 'kept' else 'set'}")
