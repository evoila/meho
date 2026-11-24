# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The check that the realm's ``browser`` flow is Keycloak's default login (#6213).

The touch flow op binds the console only when this check finds no
difference. The shapes below are written out by hand from Keycloak's
``DefaultAuthenticationFlows.browserFlow``, so they check the module's
list and do not repeat it.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.keycloak.ops_authflow_default_login import (
    ORGANIZATIONS_ON_REASON,
    default_login_difference,
    organizations_difference,
)

_PASSKEY_CONDITION = {"credentials": "webauthn-passwordless"}


def _steps(*rows: tuple[Any, ...]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Rows of ``(level, name, requirement[, settings])``; a capitalised name is a sub-flow."""
    steps: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for row in rows:
        level, name, requirement = row[:3]
        listed: dict[str, Any] = {"level": level, "requirement": requirement}
        if name[0].isupper() or name == "forms":
            listed |= {"authenticationFlow": True, "displayName": name}
        else:
            listed |= {"authenticationFlow": False, "providerId": name}
        steps.append((listed, row[3] if len(row) > 3 else None))
    return steps


#: A new realm in Keycloak 26.0 to 26.2.
_KEYCLOAK_26_0 = (
    (0, "auth-cookie", "ALTERNATIVE"),
    (0, "auth-spnego", "DISABLED"),
    (0, "identity-provider-redirector", "ALTERNATIVE"),
    (0, "forms", "ALTERNATIVE"),
    (1, "auth-username-password-form", "REQUIRED"),
    (1, "Browser - Conditional OTP", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "auth-otp-form", "REQUIRED"),
)

#: A new realm in Keycloak 26.8, every default feature on.
_KEYCLOAK_26_8 = (
    (0, "auth-cookie", "ALTERNATIVE"),
    (0, "auth-spnego", "DISABLED"),
    (0, "identity-provider-redirector", "ALTERNATIVE"),
    (0, "Organization", "ALTERNATIVE"),
    (1, "Browser - Conditional Organization", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "organization", "ALTERNATIVE"),
    (0, "forms", "ALTERNATIVE"),
    (1, "auth-username-password-form", "REQUIRED"),
    (1, "Browser - Conditional 2FA", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "conditional-credential", "REQUIRED", _PASSKEY_CONDITION),
    (2, "auth-otp-form", "ALTERNATIVE"),
    (2, "webauthn-authenticator", "DISABLED"),
    (2, "auth-recovery-authn-code-form", "DISABLED"),
)

#: A new realm in Keycloak 26.3: no passkey condition, no organization step.
_KEYCLOAK_26_3 = (
    (0, "auth-cookie", "ALTERNATIVE"),
    (0, "auth-spnego", "DISABLED"),
    (0, "identity-provider-redirector", "ALTERNATIVE"),
    (0, "forms", "ALTERNATIVE"),
    (1, "auth-username-password-form", "REQUIRED"),
    (1, "Browser - Conditional 2FA", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "auth-otp-form", "ALTERNATIVE"),
    (2, "webauthn-authenticator", "DISABLED"),
    (2, "auth-recovery-authn-code-form", "DISABLED"),
)


@pytest.mark.parametrize("shape", [_KEYCLOAK_26_0, _KEYCLOAK_26_3, _KEYCLOAK_26_8])
def test_default_login_of_each_keycloak_26_version_matches(shape: tuple[Any, ...]) -> None:
    assert default_login_difference(_steps(*shape)) is None


def test_company_login_default_on_the_redirect_step_matches() -> None:
    rows = list(_KEYCLOAK_26_8)
    rows[2] = (*rows[2], {"defaultProvider": "company"})
    assert default_login_difference(_steps(*rows)) is None


def test_changed_requirement_is_named() -> None:
    rows = list(_KEYCLOAK_26_8)
    rows[9] = (1, "Browser - Conditional 2FA", "REQUIRED")
    difference = default_login_difference(_steps(*rows))
    assert difference is not None
    assert "step 10" in difference
    assert "'Browser - Conditional 2FA' (REQUIRED)" in difference


def test_turned_on_step_is_a_difference() -> None:
    rows = list(_KEYCLOAK_26_8)
    rows[1] = (0, "auth-spnego", "ALTERNATIVE")
    assert default_login_difference(_steps(*rows)) is not None


def test_extra_step_is_a_difference() -> None:
    rows = [*_KEYCLOAK_26_0, (1, "auth-conditional-otp-form", "REQUIRED")]
    difference = default_login_difference(_steps(*rows))
    assert difference is not None


def test_missing_step_is_a_difference() -> None:
    assert default_login_difference(_steps(*_KEYCLOAK_26_0[:-1])) is not None


@pytest.mark.parametrize(
    ("index", "settings"),
    [
        (4, {"some": "setting"}),
        (2, {"defaultProvider": "company", "other": "x"}),
        (11, {"credentials": "webauthn-passwordless", "included": "true"}),
        (11, {"credentials": "otp"}),
    ],
)
def test_other_settings_are_a_difference(index: int, settings: dict[str, str]) -> None:
    rows = list(_KEYCLOAK_26_8)
    rows[index] = (*rows[index][:3], settings)
    difference = default_login_difference(_steps(*rows))
    assert difference is not None
    assert "other settings" in difference


def test_passkey_condition_without_settings_is_a_difference() -> None:
    rows = list(_KEYCLOAK_26_8)
    rows[11] = rows[11][:3]
    assert default_login_difference(_steps(*rows)) is not None


def test_organization_step_with_organizations_on_is_named() -> None:
    """Level 1 of the touch flow has no organization step."""
    assert organizations_difference(_steps(*_KEYCLOAK_26_8), True) == ORGANIZATIONS_ON_REASON
    assert organizations_difference(_steps(*_KEYCLOAK_26_8), False) is None


def test_organizations_on_without_the_organization_step_matches() -> None:
    assert organizations_difference(_steps(*_KEYCLOAK_26_3), True) is None
