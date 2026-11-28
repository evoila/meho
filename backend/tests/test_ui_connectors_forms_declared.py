# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The target form shows the extra settings and the default port a connector declares.

Covers: the fragment for a product with a required and an optional setting, the
declared port (filled, kept when typed, replaced when it was only the old
default), the 422 for a missing required setting with the typed values kept,
storing ``extras`` on create, showing and changing stored ``extras`` on edit,
a product with two declared classes (the choice and ``preferred_impl_id``), a
product with no declaration, and the role gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.target_form import TargetField

from .test_ui_connectors_forms import (  # noqa: F401  (the autouse fixture)
    _OP_ADMIN,
    _OP_OPERATOR,
    _PRODUCT,
    _TENANT_A,
    _bff_env,
    _client_with_role,
    _form_headers,
    _load_target,
    _seed_target,
    _seed_tenant,
)


class _Base(Connector):
    version = "1.0"
    supported_version_range = None

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError  # pragma: no cover

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError  # pragma: no cover

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError  # pragma: no cover


class _Plain(_Base):
    product = _PRODUCT
    impl_id = _PRODUCT


class _Declared(_Base):
    product = "declprod"
    impl_id = "declprod"
    summary = "Reads the Declared service."
    default_port = 8123
    target_fields = (
        TargetField(
            key="tenant_code", label="Tenant code", help="Given by the vendor.", required=True
        ),
        TargetField(key="site", label="Site name"),
        TargetField(key="retries", label="Retries", kind="number"),
        TargetField(key="mode", label="Mode", kind="choice", choices=("fast", "safe")),
        TargetField(key="verify", label="Check the certificate", kind="boolean", default=True),
    )


class _TwoA(_Base):
    product = "twoprod"
    impl_id = "twoprod-a"
    default_port = 1111
    target_fields = (TargetField(key="a_field", label="Setting of A", required=True),)


class _TwoB(_Base):
    product = "twoprod"
    impl_id = "twoprod-b"
    default_port = 2222
    target_fields = (TargetField(key="b_field", label="Setting of B"),)


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product=_PRODUCT, version="1.0", impl_id=_PRODUCT, cls=_Plain)
    register_connector_v2(product="declprod", version="1.0", impl_id="declprod", cls=_Declared)
    register_connector_v2(product="twoprod", version="1.0", impl_id="twoprod-a", cls=_TwoA)
    register_connector_v2(product="twoprod", version="1.0", impl_id="twoprod-b", cls=_TwoB)
    yield
    clear_registry()


def _admin() -> Any:
    _seed_tenant(_TENANT_A, "tenant-a")
    return _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )


def _create_data(**extra: str) -> dict[str, str]:
    return {
        "name": "t1",
        "product": "declprod",
        "host": "h.example.test",
        "auth_model": "shared_service_account",
        **extra,
    }


def test_fragment_shows_summary_fields_and_required_mark() -> None:
    client, mock, _csrf = _admin()
    try:
        response = client.get("/ui/connectors/target-fields", params={"product": "declprod"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Reads the Declared service." in body
    assert 'name="extra__tenant_code"' in body
    assert "Given by the vendor." in body
    assert 'name="extra__site"' in body
    # The required one has the mark and the attribute; the optional one does not.
    assert body.count("required") >= 1
    assert "(optional)" in body
    assert 'name="extra__verify"' in body
    assert "checked" in body  # the declared default of the boolean
    assert 'name="declared_port" value="8123"' in body


def test_fragment_fills_an_empty_port_and_keeps_a_typed_one() -> None:
    client, mock, _csrf = _admin()
    try:
        empty = client.get("/ui/connectors/target-fields", params={"product": "declprod"})
        typed = client.get(
            "/ui/connectors/target-fields", params={"product": "declprod", "port": "9999"}
        )
        old_default = client.get(
            "/ui/connectors/target-fields",
            params={"product": "declprod", "port": "1111", "declared_port": "1111"},
        )
    finally:
        mock.stop()
    assert 'hx-swap-oob="outerHTML"' in empty.text and 'value="8123"' in empty.text
    assert 'value="9999"' in typed.text and 'value="8123"' not in typed.text.split("<label id")[1]
    assert 'value="8123"' in old_default.text.split("<label id")[1]


def test_fragment_for_a_product_with_no_declaration_is_empty() -> None:
    client, mock, _csrf = _admin()
    try:
        response = client.get("/ui/connectors/target-fields", params={"product": _PRODUCT})
        unknown = client.get("/ui/connectors/target-fields", params={"product": "nothing"})
    finally:
        mock.stop()
    assert response.status_code == 200 and response.text.strip() == ""
    assert unknown.status_code == 200 and unknown.text.strip() == ""


def test_fragment_needs_a_tenant_admin() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    try:
        response = client.get("/ui/connectors/target-fields", params={"product": "declprod"})
    finally:
        mock.stop()
    assert response.status_code == 403


def test_create_stores_the_declared_values_in_extras() -> None:
    client, mock, csrf = _admin()
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(
                extra__tenant_code=" acme ",
                extra__site="",
                extra__retries="3",
                extra__mode="safe",
                extra__verify=["false", "true"],
            ),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    row = _load_target(_TENANT_A, "t1")
    assert row is not None
    assert row.extras == {"tenant_code": "acme", "retries": 3, "mode": "safe", "verify": True}
    assert row.preferred_impl_id is None


def test_create_without_a_required_setting_is_422_and_keeps_typed_values() -> None:
    client, mock, csrf = _admin()
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(extra__tenant_code="  ", extra__site="north", port="8443"),
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    body = response.text
    assert 'data-error-for="extra__tenant_code"' in body
    assert "This setting is required." in body
    assert 'value="north"' in body
    assert 'value="8443"' in body
    assert _load_target(_TENANT_A, "t1") is None


def test_create_with_a_bad_number_or_choice_is_422() -> None:
    client, mock, csrf = _admin()
    try:
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(extra__tenant_code="x", extra__retries="many", extra__mode="wild"),
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "Enter a whole number." in response.text
    assert "Pick one of the listed values." in response.text


def test_edit_modal_shows_the_stored_extras() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="e1",
        product="declprod",
        extras={"tenant_code": "acme", "site": "north", "verify": False, "other": "keep"},
    )
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.get("/ui/connectors/e1/edit")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'name="extra__tenant_code"' in body and 'value="acme"' in body
    assert 'value="north"' in body
    assert "Reads the Declared service." in body
    # The stored False beats the declared default True: the box is not checked.
    box = body.split('name="extra__verify"')[2]
    assert "checked" not in box.split("/>")[0]


def test_edit_submit_changes_declared_values_and_keeps_other_extras() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="e1",
        product="declprod",
        extras={"tenant_code": "acme", "site": "north", "other": "keep"},
    )
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    try:
        response = client.patch(
            "/ui/connectors/e1",
            headers=_form_headers(csrf),
            data={
                "product": "declprod",
                "host": "h.example.test",
                "auth_model": "shared_service_account",
                "extra__tenant_code": "beta",
                "extra__site": "",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    row = _load_target(_TENANT_A, "e1")
    assert row is not None
    assert row.extras == {"tenant_code": "beta", "other": "keep", "verify": False}


def test_two_classes_ask_which_one_and_store_preferred_impl_id() -> None:
    client, mock, csrf = _admin()
    try:
        fragment = client.get(
            "/ui/connectors/target-fields",
            params={"product": "twoprod", "preferred_impl_id": "twoprod-b"},
        )
        missing = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(product="twoprod", extra__b_field="x"),
        )
        done = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(product="twoprod", preferred_impl_id="twoprod-b", extra__b_field="x"),
        )
    finally:
        mock.stop()
    assert 'name="preferred_impl_id"' in fragment.text
    assert "twoprod-a" in fragment.text and "twoprod-b" in fragment.text
    # The chosen class decides the settings and the port.
    assert 'name="extra__b_field"' in fragment.text
    assert 'name="extra__a_field"' not in fragment.text
    assert 'value="2222"' in fragment.text
    assert missing.status_code == 422
    assert "Pick one of the listed connectors." in missing.text
    assert done.status_code == 204, done.text
    row = _load_target(_TENANT_A, "t1")
    assert row is not None
    assert row.preferred_impl_id == "twoprod-b"
    assert row.extras == {"b_field": "x"}


def test_a_product_with_no_declaration_creates_as_before() -> None:
    client, mock, csrf = _admin()
    try:
        modal = client.get("/ui/connectors/create")
        response = client.post(
            "/ui/connectors/create",
            headers=_form_headers(csrf),
            data=_create_data(product=_PRODUCT, extra__stray="ignored"),
        )
    finally:
        mock.stop()
    assert 'id="target-declared-fields"' in modal.text
    assert "extra__" not in modal.text
    assert response.status_code == 204, response.text
    row = _load_target(_TENANT_A, "t1")
    assert row is not None
    assert row.extras == {}
