# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Policy" page of the web console (``GET /ui/policy``), read only.

Each test runs the whole path: a console session with a real signed
token, the real console routes, and the answer of the page. The harness
comes from the review drawer suite (its autouse fixture applies by
import).
"""

from __future__ import annotations

import re
from types import MappingProxyType

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.policy import fixed_rules
from meho_backplane.ui.routes.policy import POLICY_NEEDS_ADMIN, build_policy_router
from tests.test_ui_connectors_registry_review import (  # noqa: F401 -- the autouse fixture applies by import
    _CONNECTOR_ID,
    _OP_ADMIN,
    _OP_ID,
    _OP_OPERATOR,
    _TENANT_A,
    _bff_env,
    _client_with_role,
    _edit_headers,
    _seed_connector,
    _seed_tenant,
    get_settings_cache_clear,
)

_MENU_LINK = 'href="/ui/policy"'


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings_cache_clear()


def _get_as(role: TenantRole, *paths: str, sub: str = _OP_ADMIN) -> list[str | int]:
    """GET each of *paths* as a signed-in person with *role*; status and text of each."""
    client, mock, _csrf = _client_with_role(tenant_id=_TENANT_A, operator_sub=sub, role=role)
    try:
        answers: list[str | int] = []
        for path in paths:
            response = client.get(path)
            answers += [response.status_code, response.text]
        return answers
    finally:
        mock.stop()


def test_page_lists_every_fixed_rule_with_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(TenantRole.TENANT_ADMIN, "/ui/policy")
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
    status, text = _get_as(TenantRole.TENANT_ADMIN, "/ui/policy")
    assert status == 200, text
    assert 'data-policy-fixed-rule="floor:made_up_rule"' in text
    assert "A made-up rule only this test knows." in text
    assert "floor:credential_waits" not in text


def test_page_lists_the_risk_level_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit made with today's per-operation edit shows on the page, with reason, who and when."""
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, safety_level="caution", requires_approval=True)
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        empty = client.get("/ui/policy")
        edit = client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous", "reason": "touches shared storage"},
            headers=_edit_headers(csrf),
        )
        page = client.get("/ui/policy")
    finally:
        mock.stop()
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
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous"},
            headers=_edit_headers(csrf),
        )
        response = client.get("/ui/policy")
    finally:
        mock.stop()
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


def test_switch_off_hides_the_page_and_its_menu_entry() -> None:
    """With the switch off, a direct call answers 404, as an address that does not exist."""
    _seed_tenant(_TENANT_A, "tenant-a")
    page_status, page_text, other_status, other_text, missing_status, _ = _get_as(
        TenantRole.TENANT_ADMIN, "/ui/policy", "/ui/connectors/overrides", "/ui/no-such-page"
    )
    assert page_status == 404
    assert page_status == missing_status
    assert isinstance(page_text, str)
    assert "data-policy-page" not in page_text
    assert other_status == 200, other_text
    assert _MENU_LINK not in str(other_text)


def test_switch_on_shows_the_menu_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(TenantRole.TENANT_ADMIN, "/ui/connectors/overrides")
    assert status == 200, text
    assert _MENU_LINK in str(text)


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
    status, text, report_status, _report = _get_as(
        role, "/ui/policy", "/ui/connectors/overrides", sub=_OP_OPERATOR
    )
    assert report_status == 403
    assert status == 403
    assert isinstance(text, str)
    assert refusal in text
    assert "data-policy-fixed-rules" not in text
    assert "data-policy-risk-edits" not in text
    assert "floor:" not in text


def test_switch_off_answers_404_before_the_admin_check() -> None:
    """With the switch off, an Operator gets the same 404, not the 403 of the admin check."""
    _seed_tenant(_TENANT_A, "tenant-a")
    status, _text = _get_as(TenantRole.OPERATOR, "/ui/policy", sub=_OP_OPERATOR)
    assert status == 404


def test_switch_off_still_refuses_a_read_only_session() -> None:
    """The read floor of every console page comes first: a Reader gets 403, as on every page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    status, text = _get_as(TenantRole.READ_ONLY, "/ui/policy", sub=_OP_OPERATOR)
    assert status == 403
    assert "operator_required" in str(text)
