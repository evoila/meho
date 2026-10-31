# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX profile, site pairing and certificate operations, with a respx-mocked Manager.

* ``hcx.profile.list`` is ``safe`` and returns flat rows of both kinds;
* ``hcx.profile.set``, ``hcx.site_pairing.set`` and ``hcx.certificate.trust``
  are typed, ``dangerous`` and wait for a person's approval;
* each change sends the right call; a vendor error reaches the answer;
* the pairing reads its user and password from the Vault path in the
  params, never returns them, and returns an untrusted remote certificate
  instead of trusting it;
* an unpair is refused while a service mesh uses the site.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
    site_pairing_writes,
)
from meho_backplane.connectors.hcx.profile_ops import HCX_PROFILE_OPS
from meho_backplane.connectors.hcx.site_pairing_ops import HCX_SITE_PAIRING_OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_HOST = "hcx-profiles.test.invalid"
_BASE_URL = f"https://{_HOST}"
_TARGET_NAME = "hcx-profiles"
_SESSIONS = "/hybridity/api/sessions"
_PASSWORD = "profiles-login-password-value"
_TOKEN = "profiles-hm-token-1"
_REMOTE_USER = "remote-admin-user"
_REMOTE_PASSWORD = "remote-pairing-password-value"
_REMOTE_URL = "https://remote-hcx.test.invalid"
_SECRET_REF = "hcx/remote-site"
_CP_PATH = "/hybridity/api/interconnect/computeProfiles"
_NP_PATH = "/hybridity/api/networks"
_CLOUD_CONFIGS = "/hybridity/api/cloudConfigs"
_MESHES = "/hybridity/api/interconnect/serviceMesh"
_CERTS = "/hybridity/api/admin/certificates"
#: A made-up key block, built at run time so no key block sits in the file.
_KEY_BLOCK = "-----BEGIN " + "PRIVATE KEY-----\nmade-up\n-----END " + "PRIVATE KEY-----\n"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004549")

_TABLE = {
    "hcx.profile.list": "safe",
    "hcx.profile.set": "dangerous",
    "hcx.site_pairing.set": "dangerous",
    "hcx.certificate.trust": "dangerous",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def vault_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the remote site's secret; record each Vault path read."""
    reads: list[str] = []

    async def _load(target: Any, _operator: Operator, *, param: str, **_kw: Any) -> dict[str, Any]:
        assert param == "remote_secret_ref"
        reads.append(target.secret_ref)
        return {"username": _REMOTE_USER, "password": f"{_REMOTE_PASSWORD}\n"}

    monkeypatch.setattr(site_pairing_writes, "load_param_secret_data", _load)
    return reads


def _operator(sub: str = "hcx-profiles-requester") -> Operator:
    return Operator(
        sub=sub,
        name="HCX Profiles Test Operator",
        email=None,
        raw_jwt="<hcx-profiles-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-profiles-svc", "password": _PASSWORD}


@pytest.fixture
async def hcx() -> AsyncIterator[HcxConnector]:
    set_default_reducer(PassThroughReducer())
    await register_hcx_typed_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=HCX_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="hcx/profiles",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0.0"},
                notes="seeded by test_connectors_hcx_profiles_pairing",
            )
        )
        await session.commit()
    instance = get_or_create_connector_instance(HcxConnector)
    assert isinstance(instance, HcxConnector)
    instance._session_loader = _session_loader
    yield instance


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(op_id: str, params: dict[str, Any]) -> Any:
    """Run the op as an already-approved call (the handler path)."""
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(),
        params=params,
        _approved=True,
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, headers={"x-hm-authorization": _TOKEN})


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _rendered(result: Any) -> str:
    return json.dumps(result.model_dump(mode="json"), default=str)


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


def _pem(common_name: str = "remote-hcx.test.invalid") -> tuple[str, str]:
    """A made-up self-signed certificate and its SHA-256 fingerprint."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    fingerprint = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    return cert.public_bytes(Encoding.PEM).decode("ascii"), fingerprint


# ---------------------------------------------------------------------------
# Registration and approval
# ---------------------------------------------------------------------------


async def test_each_operation_has_the_table_risk_and_approval(hcx: HcxConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == HCX_IMPL_ID)
                )
            )
            .scalars()
            .all()
        }
    ops = {op.op_id: op for op in (*HCX_PROFILE_OPS, *HCX_SITE_PAIRING_OPS)}
    assert set(ops) == set(_TABLE)
    for op_id, safety in _TABLE.items():
        row = rows[op_id]
        assert row.source_kind == "typed"
        assert row.safety_level == safety, op_id
        assert row.requires_approval is (safety != "safe"), op_id
        if safety != "safe":
            assert "preconditions" in row.llm_instructions
    assert ops["hcx.profile.list"].group_key == "hcx-profiles"
    assert ops["hcx.profile.set"].group_key == "hcx-profiles"
    assert ops["hcx.site_pairing.set"].group_key == "hcx-manager"
    assert ops["hcx.certificate.trust"].group_key == "hcx-manager"


def test_the_pairing_is_pinned_as_a_credential_write() -> None:
    assert classify_op("hcx.site_pairing.set") == "credential_write"


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("hcx.profile.set", {"profile_id": "cp-1", "compute_profile": {"name": "<name>"}}),
        (
            "hcx.site_pairing.set",
            {"action": "pair", "url": _REMOTE_URL, "remote_secret_ref": _SECRET_REF},
        ),
        ("hcx.site_pairing.set", {"action": "unpair", "endpoint_id": "site-remote"}),
        ("hcx.certificate.trust", {"certificate": _pem()[0]}),
    ],
)
async def test_a_change_waits_for_approval_and_sends_nothing(
    hcx: HcxConnector, vault_reads: list[str], op_id: str, params: dict[str, Any]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        vendor = mock.route(path__startswith="/hybridity/api").respond(200, json={})
        call = await call_operation(
            _operator(),
            {
                "connector_id": HCX_CONNECTOR_ID,
                "op_id": op_id,
                "target": _TARGET_NAME,
                "params": params,
            },
        )
    assert call["status"] == "awaiting_approval", call
    assert not [c for c in vendor.calls if c.request.url.path != _SESSIONS]
    assert vault_reads == []
    assert await _approval_rows() == 1


# ---------------------------------------------------------------------------
# hcx.profile.list
# ---------------------------------------------------------------------------


async def test_profile_list_returns_flat_rows_of_both_kinds(hcx: HcxConnector) -> None:
    compute = {
        "items": [
            {
                "computeProfileId": "cp-1",
                "name": "<compute-profile>",
                "state": "VALID",
                "services": [{"name": "INTERCONNECT"}],
                "lastTaskDetails": {"interconnectTaskId": "task-1", "status": "SUCCESS"},
            }
        ]
    }
    networks = [
        {
            "objectId": "np-1",
            "name": "<network-profile>",
            "state": "REALIZED",
            "mtu": 1500,
            "jobDetails": {"lastJobId": "job-1"},
            "vcPassword": "made-up-profile-secret",
        }
    ]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CP_PATH).respond(200, json=compute)
        mock.get(_NP_PATH).respond(200, json=networks)
        result = await _run("hcx.profile.list", {})

    assert result.status == "ok", result.error
    items = result.result["items"]
    assert [(row["profile_type"], row["profile_id"]) for row in items] == [
        ("compute", "cp-1"),
        ("network", "np-1"),
    ]
    assert items[0]["last_task_id"] == "task-1"
    assert items[0]["last_task_status"] == "SUCCESS"
    assert items[1]["last_job_id"] == "job-1"
    assert "made-up-profile-secret" not in _rendered(result)


async def test_profile_list_of_one_kind_reads_only_that_kind(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        compute = mock.get(_CP_PATH).respond(200, json={"items": []})
        mock.get(_NP_PATH).respond(200, json=[{"objectId": "np-1", "name": "<np>"}])
        result = await _run("hcx.profile.list", {"kind": "network"})

    assert result.status == "ok", result.error
    assert not compute.called
    assert [row["profile_id"] for row in result.result["items"]] == ["np-1"]


# ---------------------------------------------------------------------------
# hcx.profile.set -- compute profiles
# ---------------------------------------------------------------------------

_NEW_COMPUTE = {
    "name": "<compute-profile>",
    "services": ["INTERCONNECT", "VMOTION"],
    "clusters": [
        {"id": "cluster-1", "name": "<cluster>", "type": "ClusterComputeResource", "cmp_id": "vc-1"}
    ],
    "deployment": {
        "clusters": [{"id": "cluster-1", "cmp_id": "vc-1"}],
        "datastores": [{"id": "ds-1", "name": "<datastore>"}],
        "cpu_reservation": 0,
    },
    "networks": [{"id": "np-1", "tags": ["management", "uplink"]}],
    "switches": [{"id": "dvs-1", "cmp_id": "vc-1", "max_mtu": 9000}],
}


def _task_answer(profile_id: str = "cp-new") -> dict[str, Any]:
    return {
        "data": {"interconnectTaskId": "task-9", "computeProfileId": profile_id},
        "warnings": [],
        "errors": [],
    }


async def test_compute_profile_create_posts_vendor_fields(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        create = mock.post(_CP_PATH).respond(202, json=_task_answer())
        result = await _run("hcx.profile.set", {"compute_profile": _NEW_COMPUTE})

    assert result.status == "ok", result.error
    assert _body(create) == {
        "name": "<compute-profile>",
        "services": [{"name": "INTERCONNECT"}, {"name": "VMOTION"}],
        "compute": [
            {
                "id": "cluster-1",
                "name": "<cluster>",
                "type": "ClusterComputeResource",
                "cmpId": "vc-1",
            }
        ],
        "networks": [{"id": "np-1", "tags": ["management", "uplink"]}],
        "switches": [{"id": "dvs-1", "cmpId": "vc-1", "maxMtu": 9000}],
        "deploymentContainer": {
            "compute": [{"id": "cluster-1", "cmpId": "vc-1"}],
            "storage": [{"id": "ds-1", "name": "<datastore>"}],
            "cpuReservation": 0,
        },
    }
    assert result.result == {
        "status": "accepted",
        "kind": "compute",
        "action": "create",
        "profile_id": "cp-new",
        "interconnect_task_id": "task-9",
        "job_id": None,
        "status_read": "hcx.task.get",
        "warnings": [],
        "errors": [],
    }


async def test_compute_profile_create_needs_every_create_field(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        create = mock.post(_CP_PATH).respond(202, json=_task_answer())
        result = await _run("hcx.profile.set", {"compute_profile": {"name": "<name>"}})
    assert result.status == "error"
    assert not create.called
    assert "services, clusters, deployment, networks" in _rendered(result)


async def test_compute_profile_change_reads_merges_and_puts_back(hcx: HcxConnector) -> None:
    current = {
        "computeProfileId": "cp-1",
        "name": "<compute-profile>",
        "state": "VALID",
        "compute": [{"id": "cluster-1"}],
        "services": [{"name": "INTERCONNECT"}],
        "deploymentContainer": {"compute": [{"id": "cluster-1"}], "storage": [{"id": "ds-1"}]},
        "networks": [{"id": "np-1", "tags": ["management"]}],
        "lastTaskDetails": {"interconnectTaskId": "task-old"},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(f"{_CP_PATH}/cp-1").respond(200, json=current)
        put = mock.put(f"{_CP_PATH}/cp-1").respond(202, json=_task_answer("cp-1"))
        result = await _run(
            "hcx.profile.set",
            {
                "profile_id": "cp-1",
                "compute_profile": {
                    "services": ["INTERCONNECT", "BULK_MIGRATION"],
                    "deployment": {"memory_reservation": 100},
                },
            },
        )

    assert result.status == "ok", result.error
    sent = _body(put)
    assert "state" not in sent and "lastTaskDetails" not in sent
    assert sent["computeProfileId"] == "cp-1"
    assert sent["name"] == "<compute-profile>"
    assert sent["services"] == [{"name": "INTERCONNECT"}, {"name": "BULK_MIGRATION"}]
    assert sent["deploymentContainer"] == {
        "compute": [{"id": "cluster-1"}],
        "storage": [{"id": "ds-1"}],
        "memoryReservation": 100,
    }
    assert sent["networks"] == current["networks"]
    assert result.result["action"] == "change"
    assert result.result["interconnect_task_id"] == "task-9"


async def test_compute_profile_vendor_error_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_CP_PATH).respond(
            422, json={"errors": [{"code": "<code>", "message": "Datastore not found"}]}
        )
        result = await _run("hcx.profile.set", {"compute_profile": _NEW_COMPUTE})
    assert result.status == "error"
    assert "Datastore not found" in result.extras["upstream_message"]


async def test_profile_set_takes_one_kind_only(hcx: HcxConnector) -> None:
    result = await _run(
        "hcx.profile.set",
        {"compute_profile": {"name": "<a>"}, "network_profile": {"name": "<b>"}},
    )
    assert result.status == "error"


# ---------------------------------------------------------------------------
# hcx.profile.set -- network profiles
# ---------------------------------------------------------------------------

_NEW_NETWORK = {
    "name": "<network-profile>",
    "backing": {
        "id": "dvportgroup-1",
        "name": "<port-group>",
        "type": "DistributedVirtualPortgroup",
        "vcenter_instance_uuid": "vc-1",
    },
    "mtu": 1500,
    "prefix_length": 24,
    "gateway": "<gateway>",
    "primary_dns": "<dns>",
    "ip_ranges": [{"start_address": "<first>", "end_address": "<last>"}],
}


async def test_network_profile_create_posts_vendor_fields(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        create = mock.post(_NP_PATH).respond(
            200, json={"error": False, "data": {"jobId": "job-7", "objectId": "np-new"}}
        )
        result = await _run("hcx.profile.set", {"network_profile": _NEW_NETWORK})

    assert result.status == "ok", result.error
    assert _body(create) == {
        "organization": "DEFAULT",
        "l3TenantManaged": False,
        "ownedBySystem": True,
        "name": "<network-profile>",
        "mtu": 1500,
        "backings": [
            {
                "backingId": "dvportgroup-1",
                "backingName": "<port-group>",
                "type": "DistributedVirtualPortgroup",
                "vCenterInstanceUuid": "vc-1",
            }
        ],
        "ipScopes": [
            {
                "prefixLength": 24,
                "gateway": "<gateway>",
                "primaryDns": "<dns>",
                "networkIpRanges": [{"startAddress": "<first>", "endAddress": "<last>"}],
            }
        ],
    }
    assert result.result["status"] == "accepted"
    assert result.result["kind"] == "network"
    assert result.result["profile_id"] == "np-new"
    assert result.result["job_id"] == "job-7"
    assert result.result["status_read"] == "hcx.job.get"


async def test_network_profile_change_keeps_the_pool_and_other_fields(hcx: HcxConnector) -> None:
    current = {
        "objectId": "np-1",
        "name": "<network-profile>",
        "mtu": 1500,
        "backings": [{"backingId": "dvportgroup-1"}],
        "ipScopes": [{"poolId": "pool-1", "prefixLength": 24, "gateway": "<old-gateway>"}],
        "state": "REALIZED",
        "jobDetails": {"lastJobId": "job-old"},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(f"{_NP_PATH}/np-1").respond(200, json=current)
        put = mock.put(f"{_NP_PATH}/np-1").respond(
            200, json={"error": False, "data": {"jobId": "job-8", "objectId": "np-1"}}
        )
        result = await _run(
            "hcx.profile.set",
            {"profile_id": "np-1", "network_profile": {"gateway": "<new-gateway>", "mtu": 9000}},
        )

    assert result.status == "ok", result.error
    sent = _body(put)
    assert "state" not in sent and "jobDetails" not in sent
    assert sent["objectId"] == "np-1"
    assert sent["mtu"] == 9000
    assert sent["backings"] == current["backings"]
    assert sent["ipScopes"] == [
        {"poolId": "pool-1", "prefixLength": 24, "gateway": "<new-gateway>"}
    ]
    assert result.result["action"] == "change"
    assert result.result["job_id"] == "job-8"


async def test_network_profile_error_answer_reaches_the_answer(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_NP_PATH).respond(
            200, json={"error": True, "errorMessage": "IP range overlaps an existing profile"}
        )
        result = await _run("hcx.profile.set", {"network_profile": _NEW_NETWORK})
    assert result.status == "error"
    assert "IP range overlaps an existing profile" in _rendered(result)


# ---------------------------------------------------------------------------
# hcx.site_pairing.set
# ---------------------------------------------------------------------------

_PAIR = {"action": "pair", "url": _REMOTE_URL, "remote_secret_ref": _SECRET_REF}


async def test_pair_reads_the_secret_and_starts_the_pairing_job(
    hcx: HcxConnector, vault_reads: list[str]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CLOUD_CONFIGS).respond(200, json={"data": {"items": []}})
        pair = mock.post(_CLOUD_CONFIGS).respond(
            200, json={"success": True, "data": {"jobId": "job-pair"}, "errors": None}
        )
        result = await _run("hcx.site_pairing.set", _PAIR)

    assert result.status == "ok", result.error
    assert vault_reads == [_SECRET_REF]
    assert _body(pair) == {
        "remote": {"username": _REMOTE_USER, "password": _REMOTE_PASSWORD, "url": _REMOTE_URL}
    }
    assert result.result["status"] == "accepted"
    assert result.result["job_id"] == "job-pair"
    assert result.result["status_read"] == "hcx.job.get"
    assert _REMOTE_PASSWORD not in _rendered(result)


async def test_pair_with_an_existing_pairing_sends_nothing(
    hcx: HcxConnector, vault_reads: list[str]
) -> None:
    existing = {"items": [{"endpointId": "site-remote", "url": f"{_REMOTE_URL}/"}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CLOUD_CONFIGS).respond(200, json=existing)
        pair = mock.post(_CLOUD_CONFIGS).respond(200, json={})
        result = await _run("hcx.site_pairing.set", _PAIR)

    assert result.status == "ok", result.error
    assert not pair.called
    assert vault_reads == []
    assert result.result["status"] == "already_paired"
    assert result.result["endpoint_id"] == "site-remote"


@pytest.mark.parametrize("status", [200, 400])
async def test_pair_returns_an_untrusted_certificate_and_trusts_nothing(
    hcx: HcxConnector, vault_reads: list[str], status: int
) -> None:
    pem, fingerprint = _pem()
    answer = {
        "success": False,
        "errors": [{"error": "<error>", "text": "<text>", "data": [{"certificate": pem}]}],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CLOUD_CONFIGS).respond(200, json={"items": []})
        mock.post(_CLOUD_CONFIGS).respond(status, json=answer)
        trust = mock.post(_CERTS).respond(200, json={"success": True})
        result = await _run("hcx.site_pairing.set", _PAIR)

    assert result.status == "ok", result.error
    assert not trust.called
    assert result.result["status"] == "certificate_not_trusted"
    cert = result.result["remote_certificate"]
    assert cert["sha256"] == fingerprint
    assert cert["subject"] == "CN=remote-hcx.test.invalid"
    assert "BEGIN CERTIFICATE" in cert["certificate"]


async def test_pair_login_failure_is_an_error_without_the_vendor_text(
    hcx: HcxConnector, vault_reads: list[str]
) -> None:
    answer = {"success": False, "errors": [{"error": "Login failure", "text": "Bad user name"}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CLOUD_CONFIGS).respond(200, json={"items": []})
        mock.post(_CLOUD_CONFIGS).respond(200, json=answer)
        result = await _run("hcx.site_pairing.set", _PAIR)

    assert result.status == "error"
    rendered = _rendered(result)
    assert result.extras["exception_class"] == "ValueError"
    assert "vendor text withheld" in rendered
    assert _REMOTE_PASSWORD not in rendered


async def test_pair_http_error_never_carries_the_password(
    hcx: HcxConnector, vault_reads: list[str]
) -> None:
    echo = {"errors": [{"message": f"bad input {_REMOTE_PASSWORD}"}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_CLOUD_CONFIGS).respond(200, json={"items": []})
        mock.post(_CLOUD_CONFIGS).respond(400, json=echo)
        result = await _run("hcx.site_pairing.set", _PAIR)

    assert result.status == "error"
    assert result.extras["http_status"] == 400
    rendered = _rendered(result) + json.dumps(result.extras, default=str)
    assert _REMOTE_PASSWORD not in rendered
    assert _REMOTE_USER not in rendered


async def test_pair_needs_a_url_and_a_secret_ref(hcx: HcxConnector) -> None:
    result = await _run("hcx.site_pairing.set", {"action": "pair", "url": _REMOTE_URL})
    assert result.status == "error"
    result = await _run("hcx.site_pairing.set", {**_PAIR, "url": "http://remote-hcx.test.invalid"})
    assert result.status == "error"


async def test_unpair_removes_nothing_while_a_service_mesh_uses_the_site(hcx: HcxConnector) -> None:
    meshes = {
        "items": [
            {
                "serviceMeshId": "mesh-1",
                "computeProfiles": [{"endpointId": "site-local"}, {"endpointId": "site-remote"}],
            }
        ]
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_MESHES).respond(200, json=meshes)
        delete = mock.delete("/hybridity/api/endpointPairing/site-remote").respond(200, json={})
        result = await _run(
            "hcx.site_pairing.set", {"action": "unpair", "endpoint_id": "site-remote"}
        )

    assert result.status == "ok", result.error
    assert not delete.called
    assert result.result["status"] == "in_use"
    assert result.result["service_mesh_ids"] == ["mesh-1"]


async def test_unpair_removes_the_pairing(hcx: HcxConnector) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_MESHES).respond(200, json={"items": []})
        delete = mock.delete("/hybridity/api/endpointPairing/site-remote").respond(
            200, json={"success": True, "completed": True}
        )
        result = await _run(
            "hcx.site_pairing.set", {"action": "unpair", "endpoint_id": "site-remote"}
        )

    assert result.status == "ok", result.error
    assert delete.call_count == 1
    assert result.result["status"] == "removed"
    assert result.result["endpoint_id"] == "site-remote"


async def test_unpair_vendor_error_keeps_its_status(hcx: HcxConnector) -> None:
    """The op sends a password, so the dispatcher withholds the vendor text of its errors."""
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(_MESHES).respond(200, json={"items": []})
        mock.delete("/hybridity/api/endpointPairing/site-remote").respond(
            500, json={"errors": [{"message": "Endpoint is in use"}]}
        )
        result = await _run(
            "hcx.site_pairing.set", {"action": "unpair", "endpoint_id": "site-remote"}
        )
    assert result.status == "error"
    assert result.extras["http_status"] == 500
    assert "Endpoint is in use" not in _rendered(result)


# ---------------------------------------------------------------------------
# hcx.certificate.trust
# ---------------------------------------------------------------------------


async def test_trust_imports_the_certificate(hcx: HcxConnector) -> None:
    pem, fingerprint = _pem()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        trust = mock.post(_CERTS).respond(200, json={"success": True, "completed": True})
        result = await _run(
            "hcx.certificate.trust",
            {"certificate": pem, "expected_sha256": fingerprint.replace(":", "").lower()},
        )

    assert result.status == "ok", result.error
    assert _body(trust) == {"certificate": pem}
    assert result.result["status"] == "trusted"
    assert result.result["sha256"] == fingerprint
    assert result.result["subject"] == "CN=remote-hcx.test.invalid"


async def test_trust_refuses_another_fingerprint(hcx: HcxConnector) -> None:
    pem, _fingerprint = _pem()
    _other_pem, other = _pem("other.test.invalid")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        trust = mock.post(_CERTS).respond(200, json={"success": True})
        result = await _run("hcx.certificate.trust", {"certificate": pem, "expected_sha256": other})
    assert result.status == "error"
    assert not trust.called


@pytest.mark.parametrize(
    "text",
    [
        "not a certificate",
        _KEY_BLOCK,
    ],
    ids=["not-pem", "private-key"],
)
async def test_trust_refuses_text_that_is_not_one_certificate(hcx: HcxConnector, text: str) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        trust = mock.post(_CERTS).respond(200, json={"success": True})
        result = await _run("hcx.certificate.trust", {"certificate": text})
    assert result.status == "error"
    assert not trust.called


async def test_trust_vendor_refusal_reaches_the_answer(hcx: HcxConnector) -> None:
    pem, _fingerprint = _pem()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_CERTS).respond(
            200, json={"success": False, "errors": [{"message": "Certificate already exists"}]}
        )
        result = await _run("hcx.certificate.trust", {"certificate": pem})
    assert result.status == "error"
    assert "Certificate already exists" in _rendered(result)
