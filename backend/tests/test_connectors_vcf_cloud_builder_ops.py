# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Cloud Builder (VCF 5.x bring-up) operations of ``installer-cb-5.0``.

Covers the seven operations with hand-written answers from the vendor API
reference (made-up names and ids), mocked with respx:

* the risk table: every change waits for approval, every read is ``safe``;
* each change: it waits for approval, it succeeds, a vendor error reaches the
  answer, a running job means no second start, a lost reply says
  ``start_unknown``, and a failure at step two lists what ran;
* the status reads: progress and the failed task, a login that expires
  between the start and the read, a Cloud Builder that does not answer;
* passwords: a spec with a password in it is refused before approval, the
  passwords come from Vault references and never reach an answer.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vcf_cloud_builder import (
    CLOUD_BUILDER_CONNECTOR_ID,
    CLOUD_BUILDER_TYPED_OPS,
    CloudBuilderConnector,
    _jobs,
    register_cloud_builder_typed_operations,
)
from meho_backplane.connectors.vcf_cloud_builder._schemas import (
    REPORT_PARAMS,
    SDDC_RETRY_PARAMS,
    SDDC_START_PARAMS,
    VALIDATION_START_PARAMS,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings

BASE = "https://cb-a.test.invalid"
TENANT = UUID("00000000-0000-0000-0000-0000000000c5")
VAULT_PASSWORD = "Vault-Only-Value-1!"
LICENCE_KEY = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"

#: The table of the Task: op id -> (safety level, requires approval).
RISK_TABLE: dict[str, tuple[str, bool]] = {
    "installer.cb.about": ("safe", False),
    "installer.cb.validation.start": ("caution", True),
    "installer.cb.validation.status": ("safe", False),
    "installer.cb.sddc.start": ("dangerous", True),
    "installer.cb.sddc.status": ("safe", False),
    "installer.cb.sddc.retry": ("dangerous", True),
    "installer.cb.sddc.report": ("safe", False),
}


@dataclass
class _Target:
    name: str = "cb-a"
    host: str = "cb-a.test.invalid"
    port: int | None = 443
    secret_ref: str | None = "cloud-builder/cb-a"
    auth_model: str | None = "shared_service_account"
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    product: str = "installer"
    version: str | None = "5.2"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=lambda: UUID(int=7))
    tenant_id: UUID = TENANT


TARGET = _Target()


def _operator() -> Operator:
    return Operator(
        sub="cb-test-operator",
        name="CB Test Operator",
        email=None,
        raw_jwt="person.jwt.value",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _Loader:
    """Credentials loader stub that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _target: Any, _operator: Operator) -> dict[str, str]:
        self.calls += 1
        return {"username": "admin", "password": f"admin-pass-{self.calls}"}


def _basic(password: str) -> str:
    return "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _cb_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="installer", version="5.0", impl_id="installer-cb", cls=CloudBuilderConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, object]]:
    """Fake Vault for the secret references of a call: path -> secret data."""
    store: dict[str, dict[str, object]] = {"bringup/sddc-root": {"password": VAULT_PASSWORD}}

    async def _read(target: Any, _operator: Operator, *, param: str, mount: str) -> Any:
        del param, mount
        return store[target.secret_ref]

    monkeypatch.setattr(_jobs, "load_param_secret_data", _read)
    return store


@pytest.fixture
def loader() -> _Loader:
    return _Loader()


@pytest.fixture
def connector(loader: _Loader) -> CloudBuilderConnector:
    return CloudBuilderConnector(credentials_loader=loader)


@pytest.fixture
async def registered(monkeypatch: pytest.MonkeyPatch, loader: _Loader) -> CloudBuilderConnector:
    """Register the operations; return the dispatcher's connector instance."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    await register_cloud_builder_typed_operations()
    instance = get_or_create_connector_instance(CloudBuilderConnector)
    assert isinstance(instance, CloudBuilderConnector)
    instance._credentials_loader = loader
    return instance


async def _call(op_id: str, params: dict[str, Any], *, approved: bool = False) -> OperationResult:
    return await dispatch(
        operator=_operator(),
        connector_id=CLOUD_BUILDER_CONNECTOR_ID,
        op_id=op_id,
        target=TARGET,
        params=params,
        _approved=approved,
    )


def _spec() -> dict[str, Any]:
    return {
        "sddcId": "sddc-example",
        "sddcManagerSpec": {"hostname": "sddc-manager-example", "rootUserCredentials": {}},
    }


def _secrets() -> list[dict[str, str]]:
    return [
        {"path": "/sddcManagerSpec/rootUserCredentials/password", "secret_ref": "bringup/sddc-root"}
    ]


def _page(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"elements": list(elements), "pageMetadata": {"totalElements": len(elements)}}


def _sddc_task(status: str, *subtasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "bringup-1",
        "name": "Bringup task example",
        "status": status,
        "creationTimestamp": "2026-10-01T10:00:00Z",
        "sddcSubTasks": list(subtasks),
    }


def _subtask(name: str, status: str, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"name": name, "description": f"{name} step", "status": status, "errors": errors or []}


# ---------------------------------------------------------------- risk table


def test_every_operation_of_the_table_is_registered() -> None:
    assert {op.op_id for op in CLOUD_BUILDER_TYPED_OPS} == set(RISK_TABLE)


@pytest.mark.parametrize("op_id", sorted(RISK_TABLE))
def test_safety_level_and_approval_match_the_table(op_id: str) -> None:
    op = next(op for op in CLOUD_BUILDER_TYPED_OPS if op.op_id == op_id)
    assert (op.safety_level, op.requires_approval) == RISK_TABLE[op_id]


@pytest.mark.asyncio
async def test_registered_descriptors_carry_the_table(registered: CloudBuilderConnector) -> None:
    """The rows MEHO writes at start-up have the safety level and approval of the table."""
    del registered
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(
                    EndpointDescriptor.op_id,
                    EndpointDescriptor.safety_level,
                    EndpointDescriptor.requires_approval,
                    EndpointDescriptor.source_kind,
                ).where(EndpointDescriptor.impl_id == "installer-cb")
            )
        ).all()
    found = {op_id: (safety, approval) for op_id, safety, approval, _kind in rows}
    assert found == RISK_TABLE
    assert {kind for *_rest, kind in rows} == {"typed"}


# ---------------------------------------------------------------- about + login


@pytest.mark.asyncio
async def test_about_reads_the_service_with_basic_login(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        route = mock.get("/v1/sddcs/about").respond(
            200, json={"id": "svc-1", "name": "bringup", "version": "5.2.0.0-1", "status": "UP"}
        )
        result = await connector.about(_operator(), TARGET, {})

    assert result == {"id": "svc-1", "name": "bringup", "version": "5.2.0.0-1", "status": "UP"}
    assert route.calls[0].request.headers["authorization"] == _basic("admin-pass-1")
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_reads_the_version(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/about").respond(200, json={"version": "5.1.1.0-2", "status": "UP"})
        fp = await connector.fingerprint(TARGET, _operator())

    assert fp.reachable is True
    assert (fp.product, fp.version) == ("installer", "5.1.1.0-2")
    await connector.aclose()


# ---------------------------------------------------------------- password rules


@pytest.mark.parametrize("schema", [VALIDATION_START_PARAMS, SDDC_START_PARAMS, SDDC_RETRY_PARAMS])
@pytest.mark.parametrize(
    "field_path",
    [("rootPassword",), ("nested", "adminPassword"), ("hosts", 0, "credentials", "password")],
)
def test_a_spec_with_a_password_value_is_refused_before_approval(
    schema: dict[str, Any], field_path: tuple[Any, ...]
) -> None:
    spec: Any = {}
    node = spec
    for part, nxt in zip(field_path, (*field_path[1:], None), strict=True):
        if nxt is None:
            node[part] = "Plain-Text-Value"
        elif isinstance(nxt, int):
            node[part] = [{}]
            node = node[part]
        elif isinstance(node, list):
            node = node[part]
        else:
            node[part] = {}
            node = node[part]
    params = {"spec": spec, **({"id": "bringup-1"} if "id" in schema["properties"] else {})}
    errors = validate_params(schema, params)
    assert errors
    assert "Plain-Text-Value" not in json.dumps(errors)


@pytest.mark.parametrize("schema", [VALIDATION_START_PARAMS, SDDC_START_PARAMS, SDDC_RETRY_PARAMS])
@pytest.mark.parametrize(
    "spec",
    [
        {"esxLicense": "AAAAA-BBBBB"},
        {"vcenterSpec": {"licenseFile": "AAAAA-BBBBB"}},
        {"vsanSpec": {"licenseFile": "AAAAA-BBBBB"}},
        {"nsxtSpec": {"nsxtLicense": "AAAAA-BBBBB"}},
    ],
)
def test_a_licence_key_value_is_refused_too(schema: dict[str, Any], spec: dict[str, Any]) -> None:
    params = {"spec": spec, **({"id": "bringup-1"} if "id" in schema["properties"] else {})}
    errors = validate_params(schema, params)
    assert errors
    assert "AAAAA-BBBBB" not in json.dumps(errors)


def test_a_spec_without_secret_values_passes() -> None:
    params = {"spec": {**_spec(), "deployWithoutLicenseKeys": True}, "secrets": _secrets()}
    assert validate_params(SDDC_START_PARAMS, params) == []


def test_report_takes_a_bringup_or_a_validation_not_both() -> None:
    assert validate_params(REPORT_PARAMS, {"sddc_id": "b-1", "format": "csv"}) == []
    assert validate_params(REPORT_PARAMS, {"validation_id": "v-1"}) == []
    assert validate_params(REPORT_PARAMS, {"sddc_id": "b-1", "validation_id": "v-1"})
    assert validate_params(REPORT_PARAMS, {"validation_id": "v-1", "format": "csv"})
    assert validate_params(REPORT_PARAMS, {"sddc_id": "b-1", "format": "PDF"})


@pytest.mark.asyncio
async def test_dispatch_refuses_a_plain_password_without_waiting_for_approval(
    registered: CloudBuilderConnector,
) -> None:
    del registered
    result = await _call("installer.cb.sddc.start", {"spec": {"rootPassword": "Plain-Value"}})
    assert result.status == "error"
    assert (result.error or "").startswith("invalid_params"), result
    assert "Plain-Value" not in json.dumps(result.model_dump(mode="json"))


# ---------------------------------------------------------------- waits for approval


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("installer.cb.validation.start", {"spec": _spec(), "secrets": _secrets()}),
        ("installer.cb.sddc.start", {"spec": _spec(), "secrets": _secrets()}),
        ("installer.cb.sddc.retry", {"id": "bringup-1"}),
    ],
)
@pytest.mark.asyncio
async def test_each_change_waits_for_approval(
    registered: CloudBuilderConnector, op_id: str, params: dict[str, Any]
) -> None:
    del registered
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        result = await _call(op_id, params)

    assert result.status == "awaiting_approval", result
    assert not anything.called


# ---------------------------------------------------------------- validation.start


@pytest.mark.asyncio
async def test_validation_start_sends_the_spec_with_the_vault_password(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page())
        post = mock.post("/v1/sddcs/validations").respond(
            202, json={"id": "val-1", "executionStatus": "IN_PROGRESS"}
        )
        result = await connector.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )

    sent = json.loads(post.calls[0].request.content)
    assert sent["sddcManagerSpec"]["rootUserCredentials"]["password"] == VAULT_PASSWORD
    assert result["status"] == "accepted"
    assert result["job"]["id"] == "val-1"
    assert result["job"]["vendor_state"] == "IN_PROGRESS"
    assert result["status_read"] == {
        "op_id": "installer.cb.validation.status",
        "params": {"id": "val-1"},
    }
    assert [s["result"] for s in result["steps"]] == ["done", "done", "done"]
    assert VAULT_PASSWORD not in json.dumps(result)
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_start_does_not_start_a_second_one(
    connector: CloudBuilderConnector,
) -> None:
    running = {"id": "val-0", "executionStatus": "IN_PROGRESS"}
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page(running))
        post = mock.post("/v1/sddcs/validations").respond(202, json={})
        result = await connector.validation_start(_operator(), TARGET, {"spec": _spec()})

    assert not post.called
    assert result["status"] == "already_running"
    assert result["job"]["id"] == "val-0"
    assert result["steps"][2] == {"step": "start", "result": "not_run", "error": None}
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_start_waits_while_a_cancel_is_still_in_progress(
    connector: CloudBuilderConnector,
) -> None:
    """A validation whose cancel runs is still active: no second start goes out."""
    cancelling = {"id": "val-0", "executionStatus": "CANCELLATION_IN_PROGRESS"}
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page(cancelling))
        post = mock.post("/v1/sddcs/validations").respond(202, json={})
        result = await connector.validation_start(_operator(), TARGET, {"spec": _spec()})

    assert not post.called
    assert result["status"] == "already_running"
    assert result["job"] == {
        "kind": "validation",
        "id": "val-0",
        "vendor_state": "CANCELLATION_IN_PROGRESS",
        "target": "cb-a",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_start_returns_the_vendor_error_and_hides_a_secret(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page())
        mock.post("/v1/sddcs/validations").respond(
            400,
            json={
                "errorCode": "SPEC_INVALID",
                "message": "The spec is not valid",
                "remediationMessage": "Fix the field sddcId",
            },
        )
        result = await connector.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
        assert result["status"] == "start_failed"
        assert "The spec is not valid; Fix the field sddcId" in result["error"]
        assert "HTTP 400" in result["error"]

        mock.post("/v1/sddcs/validations").respond(
            400, json={"message": f"bad value {VAULT_PASSWORD}"}
        )
        echoed = await connector.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
    assert VAULT_PASSWORD not in json.dumps(echoed)
    assert "MEHO hid this text" in echoed["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_a_secret_ref_with_no_value_sends_nothing(
    connector: CloudBuilderConnector, vault: dict[str, dict[str, object]]
) -> None:
    vault["bringup/empty"] = {"other": "x"}
    refs = [
        {"path": "/sddcManagerSpec/rootUserCredentials/password", "secret_ref": "bringup/empty"}
    ]
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        result = await connector.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": refs}
        )

    assert not anything.called
    assert result["status"] == "invalid_input"
    assert "no text under the key 'password'" in result["error"]
    assert [s["result"] for s in result["steps"]] == ["failed", "not_run", "not_run"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_a_secret_path_outside_the_spec_sends_nothing(
    connector: CloudBuilderConnector,
) -> None:
    refs = [{"path": "/missing/password", "secret_ref": "bringup/sddc-root"}]
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        result = await connector.sddc_start(_operator(), TARGET, {"spec": _spec(), "secrets": refs})

    assert not anything.called
    assert result["status"] == "invalid_input"
    await connector.aclose()


# ---------------------------------------------------------------- sddc.start


@pytest.mark.asyncio
async def test_sddc_start_is_accepted_not_done(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page(_sddc_task("COMPLETED_WITH_FAILURE")))
        post = mock.post("/v1/sddcs").respond(
            202, json={"id": "bringup-2", "status": "IN_PROGRESS"}
        )
        result = await connector.sddc_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )

    assert post.call_count == 1
    assert result["status"] == "accepted"
    assert result["job"] == {
        "kind": "bringup",
        "id": "bringup-2",
        "vendor_state": "IN_PROGRESS",
        "target": "cb-a",
    }
    assert result["status_read"]["op_id"] == "installer.cb.sddc.status"
    assert "not done" in result["guidance"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_start_returns_the_running_bringup(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page(_sddc_task("IN_PROGRESS")))
        post = mock.post("/v1/sddcs").respond(202, json={})
        result = await connector.sddc_start(_operator(), TARGET, {"spec": _spec()})

    assert not post.called
    assert result["status"] == "already_running"
    assert result["job"]["id"] == "bringup-1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_start_vendor_error_reaches_the_answer(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").respond(
            500, json={"message": "Bringup could not start", "errorCode": "INTERNAL"}
        )
        result = await connector.sddc_start(_operator(), TARGET, {"spec": _spec()})

    assert result["status"] == "start_failed"
    assert "Bringup could not start" in result["error"]
    assert result["steps"][2]["result"] == "failed"
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_start_lost_reply_says_unknown(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").mock(side_effect=httpx.ReadTimeout("lost"))
        result = await connector.sddc_start(_operator(), TARGET, {"spec": _spec()})

    assert result["status"] == "start_unknown"
    assert result["steps"][2]["result"] == "unknown"
    assert "Do not start again blindly" in result["guidance"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_a_failure_at_step_two_lists_what_ran(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/v1/sddcs").respond(400, json={"message": "List refused"})
        post = mock.post("/v1/sddcs").respond(202, json={})
        result = await connector.sddc_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )

    assert not post.called
    assert result["status"] == "check_failed"
    assert [(s["step"], s["result"]) for s in result["steps"]] == [
        ("read_secrets", "done"),
        ("check_running", "failed"),
        ("start", "not_run"),
    ]
    assert "List refused" in result["steps"][1]["error"]
    await connector.aclose()


# ---------------------------------------------------------------- sddc.retry


@pytest.mark.asyncio
async def test_sddc_retry_sends_patch_without_a_body(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=_sddc_task("COMPLETED_WITH_FAILURE"))
        patch = mock.patch("/v1/sddcs/bringup-1").respond(
            202, json={"id": "bringup-1", "status": "IN_PROGRESS"}
        )
        result = await connector.sddc_retry(_operator(), TARGET, {"id": "bringup-1"})

    assert patch.calls[0].request.content == b""
    assert result["status"] == "accepted"
    assert result["status_read"]["params"] == {"id": "bringup-1"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_retry_with_a_fixed_spec_sends_it(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=_sddc_task("COMPLETED_WITH_FAILURE"))
        patch = mock.patch("/v1/sddcs/bringup-1").respond(202, json={"id": "bringup-1"})
        await connector.sddc_retry(
            _operator(), TARGET, {"id": "bringup-1", "spec": _spec(), "secrets": _secrets()}
        )

    sent = json.loads(patch.calls[0].request.content)
    assert sent["sddcManagerSpec"]["rootUserCredentials"]["password"] == VAULT_PASSWORD
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_retry_does_not_patch_a_running_bringup(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=_sddc_task("IN_PROGRESS"))
        patch = mock.patch("/v1/sddcs/bringup-1").respond(202, json={})
        result = await connector.sddc_retry(_operator(), TARGET, {"id": "bringup-1"})

    assert not patch.called
    assert result["status"] == "already_running"
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_retry_vendor_error_reaches_the_answer(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=_sddc_task("COMPLETED_WITH_SUCCESS"))
        mock.patch("/v1/sddcs/bringup-1").respond(
            405, json={"message": "Retry is not allowed for this bringup"}
        )
        result = await connector.sddc_retry(_operator(), TARGET, {"id": "bringup-1"})

    assert result["status"] == "start_failed"
    assert "Retry is not allowed for this bringup" in result["error"]
    await connector.aclose()


# ---------------------------------------------------------------- sddc.status


@pytest.mark.asyncio
async def test_sddc_status_gives_progress_and_the_failed_task(
    connector: CloudBuilderConnector,
) -> None:
    task = _sddc_task(
        "COMPLETED_WITH_FAILURE",
        _subtask("Deploy vCenter", "COMPLETED_WITH_SUCCESS"),
        _subtask(
            "Deploy NSX",
            "COMPLETED_WITH_FAILURE",
            [{"message": "NSX deploy failed", "remediationMessage": "Check the NSX network"}],
        ),
        _subtask("Deploy SDDC Manager", "INITIALIZED"),
    )
    connector.remember_job(TARGET, "bringup-1")
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=task)
        result = await connector.sddc_status(_operator(), TARGET, {"id": "bringup-1"})

    assert result["state"] == "failed"
    assert result["vendor_state"] == "COMPLETED_WITH_FAILURE"
    assert (result["tasks_total"], result["tasks_done"], result["tasks_failed"]) == (3, 1, 1)
    assert result["failed_task"] == "Deploy NSX"
    assert result["error"] == "NSX deploy failed; Check the NSX network"
    assert result["checked_at"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_status_running(connector: CloudBuilderConnector) -> None:
    task = _sddc_task("IN_PROGRESS", _subtask("Deploy vCenter", "IN_PROGRESS"))
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").respond(200, json=task)
        result = await connector.sddc_status(_operator(), TARGET, {"id": "bringup-1"})

    assert (result["state"], result["tasks_running"], result["error"]) == ("running", 1, None)
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_status_hides_a_secret_the_start_sent(
    connector: CloudBuilderConnector,
) -> None:
    """A task error that echoes a spec password is hidden in a later status read."""
    task = _sddc_task(
        "COMPLETED_WITH_FAILURE",
        _subtask(
            "Deploy SDDC Manager",
            "COMPLETED_WITH_FAILURE",
            [{"message": f"root password {VAULT_PASSWORD} was refused"}],
        ),
    )
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").respond(202, json={"id": "bringup-1", "status": "IN_PROGRESS"})
        start = await connector.sddc_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
        assert start["status"] == "accepted"

        mock.get("/v1/sddcs/bringup-1").respond(200, json=task)
        result = await connector.sddc_status(_operator(), TARGET, {"id": "bringup-1"})

    assert VAULT_PASSWORD not in json.dumps(result)
    assert result["state"] == "failed"
    assert result["failed_task"] == "Deploy SDDC Manager"
    assert "MEHO hid this text" in result["error"]
    assert "MEHO hid this text" in result["tasks"][0]["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_status_without_id_lists_flat_rows(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(
            200, json=_page(_sddc_task("IN_PROGRESS", _subtask("A", "IN_PROGRESS")))
        )
        result = await connector.sddc_status(_operator(), TARGET, {})

    assert result["bringups"] == [
        {
            "id": "bringup-1",
            "name": "Bringup task example",
            "state": "running",
            "vendor_state": "IN_PROGRESS",
            "created_at": "2026-10-01T10:00:00Z",
        }
    ]
    await connector.aclose()


@pytest.mark.asyncio
async def test_status_logs_in_again_after_the_login_expired(
    connector: CloudBuilderConnector, loader: _Loader
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").respond(202, json={"id": "bringup-2", "status": "IN_PROGRESS"})
        await connector.sddc_start(_operator(), TARGET, {"spec": _spec()})
        status = mock.get("/v1/sddcs/bringup-2")
        status.side_effect = [
            httpx.Response(401, json={"message": "Unauthorized"}),
            httpx.Response(200, json=_sddc_task("IN_PROGRESS")),
        ]
        result = await connector.sddc_status(_operator(), TARGET, {"id": "bringup-2"})

    assert result["state"] == "running"
    assert loader.calls == 2
    assert status.calls[0].request.headers["authorization"] == _basic("admin-pass-1")
    assert status.calls[1].request.headers["authorization"] == _basic("admin-pass-2")
    await connector.aclose()


@pytest.mark.asyncio
async def test_status_says_not_reachable_with_the_id_and_the_time(
    connector: CloudBuilderConnector,
) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/bringup-1").mock(side_effect=httpx.ConnectError("refused"))
        result = await connector.sddc_status(_operator(), TARGET, {"id": "bringup-1"})

    assert result["state"] == "not_reachable"
    assert result["id"] == "bringup-1"
    assert result["reason"] == "connect_failed"
    assert result["checked_at"]
    assert "failed" not in result["state"]
    await connector.aclose()


# ---------------------------------------------------------------- validation.status


@pytest.mark.asyncio
async def test_validation_status_lists_each_check(connector: CloudBuilderConnector) -> None:
    validation = {
        "id": "val-1",
        "description": "Bring-up spec check",
        "executionStatus": "COMPLETED",
        "resultStatus": "FAILED",
        "validationChecks": [
            {"description": "DNS", "severity": "ERROR", "resultStatus": "SUCCEEDED"},
            {
                "description": "NTP",
                "severity": "ERROR",
                "resultStatus": "FAILED",
                "errorResponse": {"message": "NTP server not reachable"},
            },
        ],
    }
    connector.remember_job(TARGET, "val-1")
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1").respond(200, json=validation)
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})

    assert result["state"] == "failed"
    assert result["checks_failed"] == 1
    assert result["error"] == "NTP: NTP server not reachable"
    assert result["checks"][1] == {
        "description": "NTP",
        "severity": "ERROR",
        "result": "FAILED",
        "error": "NTP server not reachable",
    }
    await connector.aclose()


@pytest.mark.parametrize(
    ("execution_status", "result_status", "state"),
    [
        ("IN_PROGRESS", "UNKNOWN", "running"),
        ("CANCELLATION_IN_PROGRESS", "UNKNOWN", "running"),
        ("CANCELLED", "UNKNOWN", "cancelled"),
        ("COMPLETED", "SUCCEEDED", "succeeded"),
        ("COMPLETED", "FAILED_WITH_WARNINGS", "succeeded"),
        ("COMPLETED", "FAILED", "failed"),
        ("FAILED", "UNKNOWN", "failed"),
    ],
)
def test_validation_state_words(execution_status: str, result_status: str, state: str) -> None:
    assert _jobs.validation_state(execution_status, result_status) == state


@pytest.mark.asyncio
async def test_validation_status_keeps_running_while_a_cancel_is_in_progress(
    connector: CloudBuilderConnector,
) -> None:
    """The caller polls until the cancel is done; the state is not yet cancelled."""
    validation = {
        "id": "val-1",
        "executionStatus": "CANCELLATION_IN_PROGRESS",
        "resultStatus": "UNKNOWN",
        "validationChecks": [],
    }
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1").respond(200, json=validation)
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})
        mock.get("/v1/sddcs/validations").respond(200, json=_page(validation))
        listed = await connector.validation_status(_operator(), TARGET, {})

    assert (result["state"], result["vendor_state"]) == ("running", "CANCELLATION_IN_PROGRESS")
    assert listed["validations"][0]["state"] == "running"
    await connector.aclose()


def _validation_with_error(message: str) -> dict[str, Any]:
    return {
        "id": "val-1",
        "executionStatus": "COMPLETED",
        "resultStatus": "FAILED",
        "validationChecks": [
            {
                "description": "SDDC Manager root password",
                "severity": "ERROR",
                "resultStatus": "FAILED",
                "errorResponse": {"message": message},
            }
        ],
    }


@pytest.mark.asyncio
async def test_validation_status_hides_a_secret_the_start_sent(
    connector: CloudBuilderConnector,
) -> None:
    """A check error that echoes a spec password is hidden in a later status read."""
    echoed = _validation_with_error(f"the password {VAULT_PASSWORD} is too weak")
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page())
        mock.post("/v1/sddcs/validations").respond(
            202, json={"id": "val-1", "executionStatus": "IN_PROGRESS"}
        )
        start = await connector.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
        assert start["status"] == "accepted"

        mock.get("/v1/sddcs/validations/val-1").respond(200, json=echoed)
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})

    assert VAULT_PASSWORD not in json.dumps(result)
    assert result["state"] == "failed"
    assert result["checks"][0]["description"] == "SDDC Manager root password"
    assert "MEHO hid this text" in result["checks"][0]["error"]
    assert "MEHO hid this text" in result["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_status_hides_every_vendor_text_of_a_job_meho_did_not_start(
    connector: CloudBuilderConnector,
) -> None:
    """With no start seen (for example after a restart), no vendor error text is shown."""
    rejected = _validation_with_error(f"licence key {LICENCE_KEY} is not valid for ESXi")
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1").respond(200, json=rejected)
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})

    assert LICENCE_KEY not in json.dumps(result)
    assert result["state"] == "failed"
    assert result["checks"][0]["description"] == "SDDC Manager root password"
    assert result["checks"][0]["error"].startswith("[MEHO hid this text: it may hold a secret")
    assert "did not start this job" in result["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_status_hides_a_licence_key_of_a_job_meho_started(
    connector: CloudBuilderConnector,
) -> None:
    """A licence key is hidden by its shape, also when it was not read from Vault."""
    rejected = _validation_with_error(f"licence key {LICENCE_KEY} is not valid for ESXi")
    connector.remember_job(TARGET, "val-1")
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1").respond(200, json=rejected)
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})

    assert LICENCE_KEY not in json.dumps(result)
    assert result["checks"][0]["error"] == (
        "licence key [MEHO hid a licence key] is not valid for ESXi"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_status_after_a_restart_hides_the_echoed_password(
    loader: _Loader,
) -> None:
    """A new connector (MEHO restarted) has no memory of the start; the text is hidden anyway."""
    echoed = _validation_with_error(f"the password {VAULT_PASSWORD} is too weak")
    before = CloudBuilderConnector(credentials_loader=loader)
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations").respond(200, json=_page())
        mock.post("/v1/sddcs/validations").respond(
            202, json={"id": "val-1", "executionStatus": "IN_PROGRESS"}
        )
        start = await before.validation_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
        assert start["status"] == "accepted"
        await before.aclose()

        after = CloudBuilderConnector(credentials_loader=loader)
        mock.get("/v1/sddcs/validations/val-1").respond(200, json=echoed)
        result = await after.validation_status(_operator(), TARGET, {"id": "val-1"})
        traced = after.trace_response_body(
            httpx.Response(
                200, json=echoed, request=httpx.Request("GET", f"{BASE}/v1/sddcs/validations/val-1")
            )
        )
    await after.aclose()

    assert VAULT_PASSWORD not in json.dumps(result)
    assert VAULT_PASSWORD not in json.dumps(traced)
    assert result["state"] == "failed"
    assert result["checks_failed"] == 1
    assert "did not start this job" in result["checks"][0]["error"]
    assert "did not start this job" in result["error"]
    assert "did not start this job" in traced["validationChecks"][0]["errorResponse"]["message"]


@pytest.mark.asyncio
async def test_sddc_status_after_a_restart_hides_the_echoed_password(loader: _Loader) -> None:
    """A new connector (MEHO restarted) has no memory of the start; the text is hidden anyway."""
    task = _sddc_task(
        "COMPLETED_WITH_FAILURE",
        _subtask(
            "Deploy SDDC Manager",
            "COMPLETED_WITH_FAILURE",
            [
                {
                    "message": f"root password {VAULT_PASSWORD} was refused",
                    "remediationMessage": f"do not use {VAULT_PASSWORD}",
                    "nestedErrors": [{"message": f"got {VAULT_PASSWORD}"}],
                }
            ],
        ),
    )
    before = CloudBuilderConnector(credentials_loader=loader)
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").respond(202, json={"id": "bringup-1", "status": "IN_PROGRESS"})
        start = await before.sddc_start(
            _operator(), TARGET, {"spec": _spec(), "secrets": _secrets()}
        )
        assert start["status"] == "accepted"
        await before.aclose()

        after = CloudBuilderConnector(credentials_loader=loader)
        mock.get("/v1/sddcs/bringup-1").respond(200, json=task)
        result = await after.sddc_status(_operator(), TARGET, {"id": "bringup-1"})
        traced = after.trace_response_body(
            httpx.Response(
                200, json=task, request=httpx.Request("GET", f"{BASE}/v1/sddcs/bringup-1")
            )
        )
    await after.aclose()

    assert VAULT_PASSWORD not in json.dumps(result)
    assert VAULT_PASSWORD not in json.dumps(traced)
    assert result["state"] == "failed"
    assert result["failed_task"] == "Deploy SDDC Manager"
    assert "did not start this job" in result["error"]
    assert "did not start this job" in result["tasks"][0]["error"]
    error = traced["sddcSubTasks"][0]["errors"][0]
    assert "did not start this job" in error["message"]
    assert "did not start this job" in error["remediationMessage"]
    assert "did not start this job" in error["nestedErrors"][0]["message"]


@pytest.mark.asyncio
async def test_status_trace_body_hides_the_secret_the_start_sent(
    connector: CloudBuilderConnector,
) -> None:
    """The flight recorder gets the vendor answer without the echoed password or key."""
    connector.remember_secrets(TARGET, (VAULT_PASSWORD,))
    connector.remember_job(TARGET, "val-1")
    echoed = _validation_with_error(f"{VAULT_PASSWORD} is too weak; key {LICENCE_KEY} is bad")
    request = httpx.Request("GET", f"{BASE}/v1/sddcs/validations/val-1")
    traced = connector.trace_response_body(httpx.Response(200, json=echoed, request=request))

    assert VAULT_PASSWORD not in json.dumps(traced)
    assert LICENCE_KEY not in json.dumps(traced)
    assert traced["validationChecks"][0]["errorResponse"]["message"] == (
        "[MEHO hid this text: it held a secret from Vault]"
    )
    other_host = httpx.Request("GET", "https://cb-b.test.invalid/v1/sddcs/validations/val-1")
    not_remembered = connector.trace_response_body(
        httpx.Response(200, json=echoed, request=other_host)
    )
    assert VAULT_PASSWORD not in json.dumps(not_remembered)
    assert LICENCE_KEY not in json.dumps(not_remembered)
    assert (
        "did not start this job"
        in (not_remembered["validationChecks"][0]["errorResponse"]["message"])
    )
    assert connector.trace_response_body(httpx.Response(200, text="not json")) is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_status_not_reachable(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1").mock(side_effect=httpx.ConnectTimeout("slow"))
        result = await connector.validation_status(_operator(), TARGET, {"id": "val-1"})

    assert (result["state"], result["id"], result["reason"]) == (
        "not_reachable",
        "val-1",
        "timeout",
    )
    await connector.aclose()


# ---------------------------------------------------------------- report


@pytest.mark.parametrize("fmt", ["pdf", "csv"])
@pytest.mark.asyncio
async def test_detail_report_comes_back_as_a_file(
    connector: CloudBuilderConnector, fmt: str
) -> None:
    content = b"%PDF-1.4 example" if fmt == "pdf" else b"task,status\nA,OK\n"
    async with respx.mock(base_url=BASE) as mock:
        route = mock.get("/v1/sddcs/bringup-1/detail-report").respond(200, content=content)
        result = await connector.sddc_report(
            _operator(), TARGET, {"sddc_id": "bringup-1", "format": fmt}
        )

    assert route.calls[0].request.url.params["format"] == fmt
    assert result["report"] == "bringup_detail"
    assert base64.b64decode(result["content"]) == content
    assert result["size_bytes"] == len(content)
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    await connector.aclose()


@pytest.mark.asyncio
async def test_validation_report_comes_back_as_a_file(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/validations/val-1/report").respond(200, content=b"%PDF-1.4 v")
        result = await connector.sddc_report(_operator(), TARGET, {"validation_id": "val-1"})

    assert (result["report"], result["format"]) == ("validation", "pdf")
    await connector.aclose()


def test_report_asks_for_a_stored_document() -> None:
    op = next(op for op in CLOUD_BUILDER_TYPED_OPS if op.op_id == "installer.cb.sddc.report")
    assert op.llm_instructions["result_document"] == {"always": True}


@pytest.mark.asyncio
async def test_report_vendor_error_propagates(connector: CloudBuilderConnector) -> None:
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs/missing/detail-report").respond(404, json={"message": "no"})
        with pytest.raises(httpx.HTTPStatusError):
            await connector.sddc_report(_operator(), TARGET, {"sddc_id": "missing"})
    await connector.aclose()


@pytest.mark.asyncio
async def test_approved_start_runs_through_the_dispatcher(
    registered: CloudBuilderConnector,
) -> None:
    del registered
    async with respx.mock(base_url=BASE) as mock:
        mock.get("/v1/sddcs").respond(200, json=_page())
        mock.post("/v1/sddcs").respond(202, json={"id": "bringup-9", "status": "IN_PROGRESS"})
        result = await _call(
            "installer.cb.sddc.start", {"spec": _spec(), "secrets": _secrets()}, approved=True
        )

    assert result.status == "ok", result
    assert isinstance(result.result, dict)
    assert result.result["status"] == "accepted"
    assert VAULT_PASSWORD not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "op_id",
    ["installer.cb.validation.start", "installer.cb.sddc.start", "installer.cb.sddc.retry"],
)
def test_changes_that_send_a_spec_are_credential_writes(op_id: str) -> None:
    """No flight-recorder body, no broadcast params, an encrypted approval handoff."""
    assert classify_op(op_id) == "credential_write"


@pytest.mark.parametrize(
    "op_id",
    ["installer.cb.validation.start", "installer.cb.sddc.start", "installer.cb.sddc.retry"],
)
@pytest.mark.asyncio
async def test_the_approver_sees_what_changes_and_no_secret(op_id: str) -> None:
    spec = {
        **_spec(),
        "hostSpecs": [{"hostname": "esx-example-1", "credentials": {"username": "root"}}],
        "networkSpecs": [{"networkType": "MANAGEMENT", "vlanId": "10", "subnet": "x/24"}],
    }
    params: dict[str, Any] = {"spec": spec, "secrets": _secrets()}
    if op_id.endswith("retry"):
        params["id"] = "bringup-1"
    ctx = SimpleNamespace(params=params, target=TARGET)
    preview = await _PREVIEW_BUILDERS[op_id](ctx)  # type: ignore[arg-type]

    assert preview is not None
    assert preview["target"] == "cb-a"
    assert preview["spec"]["sddc_id"] == "sddc-example"
    assert preview["spec"]["host_names"] == ["esx-example-1"]
    assert preview["secrets_from_vault"] == [
        {"path": "/sddcManagerSpec/rootUserCredentials/password", "secret_ref": "bringup/sddc-root"}
    ]
    assert VAULT_PASSWORD not in json.dumps(preview)
