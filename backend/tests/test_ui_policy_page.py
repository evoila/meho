# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Policy" page of the web console (``GET /ui/policy``), read only.

Each test runs the whole path: the person signs in through the console's
own sign-in and callback routes (the identity provider is faked), then
calls the real console app (:data:`meho_backplane.main.app`) with the
session cookie the callback set. The data helpers come from the review
drawer suite (its autouse fixture applies by import).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app as console_app
from meho_backplane.policy import fixed_rules
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME
from meho_backplane.ui.routes.policy import POLICY_NEEDS_ADMIN, build_policy_router
from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token, public_jwks
from tests.test_ui_auth_flow import _mock_oidc_metadata, _patch_oauth_token_transport
from tests.test_ui_connectors_registry_review import (  # noqa: F401 -- the autouse fixture applies by import
    _CONNECTOR_ID,
    _OP_ADMIN,
    _OP_ID,
    _OP_OPERATOR,
    _TENANT_A,
    _bff_env,
    _edit_headers,
    _seed_connector,
    _seed_tenant,
    get_settings_cache_clear,
)

_MENU_LINK = 'href="/ui/policy"'
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings_cache_clear()


@contextmanager
def _signed_in(
    monkeypatch: pytest.MonkeyPatch,
    role: TenantRole,
    *,
    sub: str = _OP_ADMIN,
    platform_admin: bool | None = None,
) -> Iterator[tuple[TestClient, str]]:
    """Sign in through ``/ui/auth/login`` and ``/ui/auth/callback``.

    Yields the client, which holds the session cookie, and the CSRF token.
    """
    key = make_rsa_keypair("policy-page-kid")
    access_token = mint_token(
        key,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=role.value,
        platform_admin=platform_admin,
        fresh_sign_in=True,
    )
    clear_jwks_cache()
    with respx.mock(assert_all_called=False) as mock_router:
        _mock_oidc_metadata(mock_router, jwks=public_jwks(key))
        _patch_oauth_token_transport(
            monkeypatch,
            httpx2.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "made-up-refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        )
        client = TestClient(console_app, base_url="https://testserver", follow_redirects=False)
        login = client.get("/ui/auth/login?return_to=/ui/")
        assert login.status_code in (302, 303, 307), login.text
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = client.get(f"/ui/auth/callback?code=made-up-code&state={state}")
        assert callback.status_code == 302, callback.text
        assert SESSION_COOKIE_NAME in callback.cookies
        # A console page sets the CSRF cookie on its answer.
        client.get("/ui/")
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        assert csrf, "the console set no CSRF cookie"
        yield client, csrf


def _get_as(
    monkeypatch: pytest.MonkeyPatch, role: TenantRole, *paths: str, sub: str = _OP_ADMIN
) -> list[str | int]:
    """GET each of *paths* as a person who signed in with *role*; status and text of each."""
    with _signed_in(monkeypatch, role, sub=sub) as (client, _csrf):
        answers: list[str | int] = []
        for path in paths:
            response = client.get(path)
            answers += [response.status_code, response.text]
        return answers


def _audit_row_count() -> int:
    async def _count() -> int:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(func.count()).select_from(AuditLog))
            return int(result.scalar_one())

    return asyncio.run(_count())


def test_page_lists_every_fixed_rule_with_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(monkeypatch, TenantRole.TENANT_ADMIN, "/ui/policy")
    assert status == 200, text
    assert isinstance(text, str)
    assert "data-policy-fixed-rules" in text
    for rule_id, sentence in fixed_rules.FIXED_RULES.items():
        assert f'data-policy-fixed-rule="floor:{rule_id}"' in text
        assert f">floor:{rule_id}<" in text
        assert sentence.replace("'", "&#39;") in text
    assert text.count("data-policy-fixed-rule=") == len(fixed_rules.FIXED_RULES)


def test_fixed_rules_come_from_the_module_not_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page reads ``FIXED_RULES`` when it renders: a changed module shows on the page."""
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    monkeypatch.setattr(
        fixed_rules,
        "FIXED_RULES",
        MappingProxyType({"made_up_rule": "A made-up rule only this test knows."}),
    )
    status, text = _get_as(monkeypatch, TenantRole.TENANT_ADMIN, "/ui/policy")
    assert status == 200, text
    assert 'data-policy-fixed-rule="floor:made_up_rule"' in text
    assert "A made-up rule only this test knows." in text
    assert "floor:credential_waits" not in text


def test_page_lists_the_risk_level_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit made with today's per-operation edit shows on the page, with reason, who and when."""
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, safety_level="caution", requires_approval=True)
    with _signed_in(monkeypatch, TenantRole.TENANT_ADMIN) as (client, csrf):
        empty = client.get("/ui/policy")
        edit = client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous", "reason": "touches shared storage"},
            headers=_edit_headers(csrf),
        )
        page = client.get("/ui/policy")
    assert empty.status_code == 200, empty.text
    assert "data-policy-risk-edits-empty" in empty.text
    assert edit.status_code == 200, edit.text
    assert page.status_code == 200, page.text
    assert "data-policy-risk-edits-empty" not in page.text
    row = re.search(r'<tr data-policy-risk-edit data-op-id="([^"]+)">(.*?)</tr>', page.text, re.S)
    assert row is not None, page.text
    assert row.group(1) == _OP_ID
    cells = row.group(2)
    assert _CONNECTOR_ID in cells
    assert "dangerous" in cells
    assert "touches shared storage" in cells
    assert _OP_ADMIN in cells
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", cells)


def test_page_has_no_save_and_no_form(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, safety_level="caution", requires_approval=True)
    with _signed_in(monkeypatch, TenantRole.TENANT_ADMIN) as (client, csrf):
        client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous"},
            headers=_edit_headers(csrf),
        )
        response = client.get("/ui/policy")
    assert response.status_code == 200, response.text
    text = response.text
    page = re.search(r"<section[^>]*data-policy-page.*?</section>\s*</section>", text, re.S)
    assert page is not None, text
    content = page.group(0)
    assert "data-policy-risk-edit " in content
    assert "<form" not in content
    assert "<button" not in content
    assert "<input" not in content
    assert not re.search(r"hx-(post|put|patch|delete)", content)
    assert "Save" not in content
    # The router itself has no route that writes.
    methods = {m for route in build_policy_router().routes for m in getattr(route, "methods", ())}
    assert methods == {"GET"}


def test_switch_off_hides_the_page_and_its_menu_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off, a direct call answers as an address that does not exist."""
    _seed_tenant(_TENANT_A, "tenant-a")
    page_status, page_text, other_status, other_text, missing_status, missing_text = _get_as(
        monkeypatch,
        TenantRole.TENANT_ADMIN,
        "/ui/policy",
        "/ui/connectors/overrides",
        "/ui/no-such-page",
    )
    assert (page_status, page_text) == (missing_status, missing_text)
    assert page_status == 404
    assert "data-policy-page" not in str(page_text)
    assert other_status == 200, other_text
    assert _MENU_LINK not in str(other_text)


def test_switch_on_shows_the_menu_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(monkeypatch, TenantRole.TENANT_ADMIN, "/ui/connectors/overrides")
    assert status == 200, text
    assert _MENU_LINK in str(text)


@pytest.mark.parametrize(
    ("role", "sub", "platform_admin"),
    [
        pytest.param(TenantRole.READ_ONLY, _OP_OPERATOR, None, id="case1-reader"),
        pytest.param(TenantRole.OPERATOR, _OP_OPERATOR, None, id="case2-operator"),
        pytest.param(TenantRole.TENANT_ADMIN, _OP_ADMIN, None, id="case3-admin"),
        pytest.param(TenantRole.TENANT_ADMIN, _OP_ADMIN, True, id="case4-platform-admin"),
    ],
)
def test_switch_off_policy_address_answers_as_an_address_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, role: TenantRole, sub: str, platform_admin: bool | None
) -> None:
    """Switch off: every method gets the answer of ``/ui/no-such-page``, and no audit row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with _signed_in(monkeypatch, role, sub=sub, platform_admin=platform_admin) as (client, csrf):
        headers = {"X-CSRF-Token": csrf}
        for method in _METHODS:
            missing = client.request(method, "/ui/no-such-page", headers=headers)
            before = _audit_row_count()
            page = client.request(method, "/ui/policy", headers=headers)
            assert _audit_row_count() == before, method
            assert page.status_code == missing.status_code, method
            assert page.content == missing.content, method
            assert page.status_code == 404, method


@pytest.mark.parametrize(
    ("role", "sub", "status", "marker"),
    [
        pytest.param(
            TenantRole.TENANT_ADMIN, _OP_ADMIN, 200, "data-policy-risk-edits", id="case5-admin"
        ),
        pytest.param(
            TenantRole.READ_ONLY, _OP_OPERATOR, 403, "operator_required", id="case6-reader"
        ),
        pytest.param(
            TenantRole.OPERATOR, _OP_OPERATOR, 403, POLICY_NEEDS_ADMIN, id="case7-operator"
        ),
    ],
)
def test_switch_on_policy_page_answers_by_role(
    monkeypatch: pytest.MonkeyPatch, role: TenantRole, sub: str, status: int, marker: str
) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    got_status, text = _get_as(monkeypatch, role, "/ui/policy", sub=sub)
    assert got_status == status, text
    assert marker in str(text)
    if status == 200:
        assert "data-policy-fixed-rules" in str(text)


@pytest.mark.parametrize(
    ("role", "refusal"),
    [(TenantRole.OPERATOR, POLICY_NEEDS_ADMIN), (TenantRole.READ_ONLY, "operator_required")],
)
def test_page_is_refused_to_a_person_who_may_not_read_the_overrides(
    monkeypatch: pytest.MonkeyPatch, role: TenantRole, refusal: str
) -> None:
    """The same check as the override report: an Operator or a Reader gets 403 on both."""
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, safety_level="caution", requires_approval=True)
    status, text, report_status, report_text = _get_as(
        monkeypatch, role, "/ui/policy", "/ui/connectors/overrides", sub=_OP_OPERATOR
    )
    assert report_status == 403
    assert status == 403
    assert isinstance(text, str)
    assert refusal in text
    assert "data-policy-fixed-rules" not in text
    assert "data-policy-risk-edits" not in text
    assert "floor:" not in text
    assert _MENU_LINK not in text
    assert _MENU_LINK not in str(report_text)


def test_fixed_rules_lead_line_says_what_a_fixed_rule_is(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(monkeypatch, TenantRole.TENANT_ADMIN, "/ui/policy")
    assert status == 200, text
    words = " ".join(str(text).split())
    assert (
        "A fixed rule is one of MEHO's own checks. These checks decide whether a call runs "
        "or waits for a person. Nothing after them lets a call run where they make it wait: "
        "no standing grant, no run approval and no policy rule." in words
    )
    assert "These rules always hold" not in words
    assert "No policy and no admin can lower them" not in words


def test_first_line_names_both_lists_and_says_the_page_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(monkeypatch, TenantRole.TENANT_ADMIN, "/ui/policy")
    assert status == 200, text
    words = " ".join(str(text).split())
    first_line = (
        "This page shows two lists: the fixed rules and the risk-level edits. It changes nothing."
    )
    assert first_line in words
    old_line = "The rules that decide which operations run at once and which wait for a person"
    assert old_line not in words
