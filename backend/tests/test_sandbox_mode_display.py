# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How sandbox mode shows itself (#4622).

* ``/ready`` carries a top-level ``sandbox_mode`` for every caller, also
  without the metrics token, and ``features.approval_queue.sandbox_mode``.
* ``GET /api/v1/health`` carries ``sandbox_mode``.
* Every console page shows the banner with the mode on and no banner with
  it off.
* The step list marks delete steps grantable with the mode on and keeps
  today's answers with it off (copies of the ``test_governed_subops.py``
  tests with the mode on).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.api.v1.health import HealthResponse, VaultStatus, build_health_response
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.features import build_features_block
from meho_backplane.health import ProbeResult, clear_probes, register_probe
from meho_backplane.main import app
from meho_backplane.operations.governed_subops import (
    build_governed_subops_response,
    classify_subop_grantability,
    governed_subops_for,
    registered_governed_subop_surfaces,
    reset_governed_subop_registry,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.templating import get_jinja_env, reset_templating_for_testing
from tests._sandbox_mode_helpers import turn_sandbox_mode_off, turn_sandbox_mode_on

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000004622")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_templating_for_testing()
    yield
    get_settings.cache_clear()
    reset_templating_for_testing()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "on"])
def test_ready_shows_the_mode_at_the_top_level(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "on":
        turn_sandbox_mode_on(monkeypatch)
    else:
        turn_sandbox_mode_off(monkeypatch)
    clear_probes()
    register_probe("stub", lambda: ProbeResult(name="stub", ok=True))
    try:
        body = client.get("/ready").json()
    finally:
        clear_probes()
    assert body["status"] == "ready"
    assert body["sandbox_mode"] == mode
    assert body["features"]["approval_queue"]["sandbox_mode"] == mode


def test_ready_shows_the_mode_without_the_metrics_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the metrics token set, ``features`` is hidden but the mode is not."""
    turn_sandbox_mode_on(monkeypatch)
    monkeypatch.setenv("METRICS_AUTH_TOKEN", "secret-token")
    get_settings.cache_clear()
    body = client.get("/ready").json()
    assert "features" not in body
    assert body["sandbox_mode"] == "on"


@pytest.mark.parametrize("mode", ["off", "on"])
def test_features_block_reports_the_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "on":
        turn_sandbox_mode_on(monkeypatch)
    else:
        turn_sandbox_mode_off(monkeypatch)
    block = build_features_block(get_settings())
    assert block["approval_queue"]["sandbox_mode"] == mode


# ---------------------------------------------------------------------------
# GET /api/v1/health (the body ``meho status`` reads)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "on"])
async def test_health_response_carries_the_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "on":
        turn_sandbox_mode_on(monkeypatch)
    else:
        turn_sandbox_mode_off(monkeypatch)

    async def _vault(*_args: object, **_kwargs: object) -> VaultStatus:
        return VaultStatus(reachable=True, read_ok=True, detail=None)

    monkeypatch.setattr("meho_backplane.api.v1.health._probe_federation", _vault)
    operator = Operator(
        sub="op-status",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    response = await build_health_response(operator)
    assert response.sandbox_mode == mode
    assert HealthResponse.model_fields["sandbox_mode"].default == "off"


# ---------------------------------------------------------------------------
# Console banner on every page
# ---------------------------------------------------------------------------


def _page_templates() -> list[str]:
    env = get_jinja_env()
    names = [
        name
        for name in env.list_templates()
        if name.endswith(".html") and 'extends "base.html"' in env.loader.get_source(env, name)[0]  # type: ignore[union-attr]
    ]
    assert names, "no page template extends base.html"
    return names


@pytest.mark.parametrize("mode", ["off", "on"])
def test_every_console_page_shows_the_banner_only_with_the_mode_on(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "on":
        turn_sandbox_mode_on(monkeypatch)
    else:
        turn_sandbox_mode_off(monkeypatch)
    html = get_jinja_env().get_template("base.html").render(ready=True)
    assert ("data-sandbox-banner" in html) is (mode == "on")
    if mode == "on":
        assert "agents may delete systems here without a person" in html
    # The banner lives in base.html, so every page that extends it shows it.
    assert len(_page_templates()) > 10


def test_banner_counts_expiring_delete_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_on(monkeypatch)
    template = get_jinja_env().get_template("base.html")
    one = template.render(ready=True, sandbox_expiring_delete_grants=1)
    assert "1 delete grant expires within 7 days" in one
    three = template.render(ready=True, sandbox_expiring_delete_grants=3)
    assert "3 delete grants expire within 7 days" in three
    none = template.render(ready=True, sandbox_expiring_delete_grants=0)
    assert "within 7 days" not in none


# ---------------------------------------------------------------------------
# The step list: delete steps are grantable with the mode on
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_subop_registry() -> Iterator[None]:
    reset_governed_subop_registry()
    yield
    reset_governed_subop_registry()


def test_classify_flags_delete_shaped_with_the_mode_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copy of ``test_classify_flags_delete_shaped`` with the mode on: a delete leg is grantable."""
    turn_sandbox_mode_on(monkeypatch)
    assert classify_subop_grantability("POST:/vcenter/vm").grantable is True
    delete_leg = classify_subop_grantability("DELETE:/vcenter/vm/{vm}")
    assert delete_leg.grantable is True
    assert delete_leg.ungrantable_reason is None
    destroy = classify_subop_grantability("POST:/VirtualMachine/{moId}/Destroy_Task")
    assert destroy.grantable is True


def test_classify_keeps_todays_answers_with_the_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    turn_sandbox_mode_off(monkeypatch)
    delete_leg = classify_subop_grantability("DELETE:/vcenter/vm/{vm}")
    assert delete_leg.grantable is False
    assert "delete-shaped" in (delete_leg.ungrantable_reason or "")


def test_vmware_wiring_is_derived_from_manifests_with_the_mode_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy of ``test_vmware_wiring_is_derived_from_manifests``: delete legs become grantable."""
    from meho_backplane.connectors.vmware_rest.composites import _write
    from meho_backplane.connectors.vmware_rest.composites._register import (
        register_vmware_governed_subops,
    )

    turn_sandbox_mode_on(monkeypatch)
    register_vmware_governed_subops()
    surfaces = registered_governed_subop_surfaces()
    assert "vmware.composite.vm.create" in surfaces
    assert "vmware.composite.vm.destroy" in surfaces

    vm_create = governed_subops_for("vmware.composite.vm.create")
    assert vm_create is not None
    assert set(_write._SUB_OPS_VM_CREATE) <= set(vm_create.sub_op_ids)
    assert "DELETE:/vcenter/vm/{vm}" in vm_create.sub_op_ids
    assert all(classify_subop_grantability(c).grantable for c in vm_create.sub_op_ids)

    vm_destroy = governed_subops_for("vmware.composite.vm.destroy")
    assert vm_destroy is not None
    assert all(classify_subop_grantability(c).grantable for c in vm_destroy.sub_op_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "on"])
async def test_step_list_response_follows_the_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from meho_backplane.connectors.vmware_rest.composites._register import (
        register_vmware_governed_subops,
    )

    if mode == "on":
        turn_sandbox_mode_on(monkeypatch)
    else:
        turn_sandbox_mode_off(monkeypatch)
    register_vmware_governed_subops()
    response = await build_governed_subops_response(
        tenant_id=_TENANT, op_id="vmware.composite.vm.destroy"
    )
    assert response is not None
    by_op = {child.op_id: child for child in response.governed_subops}
    destroy = by_op["POST:/VirtualMachine/{moId}/Destroy_Task"]
    assert destroy.grantable is (mode == "on")
    assert (destroy.ungrantable_reason is None) is (mode == "on")
