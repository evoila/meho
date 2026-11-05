# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested licence operations stay off whatever their HTTP method.

A vendor file can read licences with ``POST``: the vSphere API file has
``QueryAssignedLicenses``, ``QueryLicenseUsage`` and ``DecodeLicense``.
MEHO treats them like the ``GET`` licence reads
(``operations/license_reads.py``):

* the rule matches every HTTP method of an ingested op whose path names
  licences, and never a hand-written op;
* a fresh load leaves a ``POST`` licence row off, and ``enable-reads``
  does not turn it on;
* a call and a preview answer ``license_read_off`` before any vendor
  call, when the row is off and when it was turned on by hand.

MEHO also hides a list of licence keys: ``licenseKeys`` and
``licenceKeys`` are secret names, in the default view of every answer,
in vendor error text, in broadcast params and in approval params.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import (
    is_secret_param_name,
    is_secret_param_name_lowercased,
    scrub_broadcast_params,
    scrub_secret_named_values,
)
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.operations.approval_queue import params_with_secret_fields_hidden
from meho_backplane.operations.ingest._internals import ConnectorScope, bulk_enable_read_ops
from meho_backplane.operations.license_reads import is_license_read, is_license_read_op_id
from meho_backplane.redaction.secret_fields import scrub_secret_fields_in_text
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000a561")
_PRODUCT = "licpostfixture"
_VERSION = "1"
_IMPL_ID = "licpostfixture-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

_QUERY_ASSIGNED = "POST:/LicenseAssignmentManager/{moId}/QueryAssignedLicenses"
_QUERY_USAGE = "POST:/LicenseManager/{moId}/QueryLicenseUsage"
_HOSTS = "GET:/v1/hosts"

#: A made-up licence key. It must reach nothing.
_FAKE_KEY = "fake-licence-key-one"
_OTHER_FAKE_KEY = "fake-licence-key-two"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def embedding() -> list[float]:
    return [0.1] * 384


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _make_operator() -> Operator:
    return Operator(
        sub="op-license-post",
        name="Licence Post Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """The target shape the resolver, the dispatcher and the connector read."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = _FakeFingerprint(version=_VERSION)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "licence-target"
        self.host = "licence.example.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None


class _KeyReturningConnector(HttpConnector):
    """Answers every call with the made-up key; counts its calls."""

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1,<2"
    priority = 1

    calls: ClassVar[int] = 0

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        type(self).calls += 1
        return {"returnval": [{"assignedLicense": {"licenseKey": _FAKE_KEY}}]}

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        type(self).calls += 1
        return {"returnval": [{"assignedLicense": {"licenseKey": _FAKE_KEY}}]}

    async def fingerprint(  # type: ignore[override]
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _descriptor(
    *, op_id: str, is_enabled: bool, embedding: list[float] | None = None
) -> EndpointDescriptor:
    method, _, path = op_id.partition(":")
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary=f"{method} {path}",
        description="Ingested licence test op.",
        tags=["licensing"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="caution",
        requires_approval=False,
        is_enabled=is_enabled,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def _seed(session: AsyncSession, embedding: list[float], *, is_enabled: bool) -> None:
    _KeyReturningConnector.calls = 0
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_KeyReturningConnector
    )
    for op_id in (_QUERY_ASSIGNED, _QUERY_USAGE):
        session.add(_descriptor(op_id=op_id, is_enabled=is_enabled, embedding=embedding))
    session.add(_descriptor(op_id=_HOSTS, is_enabled=True, embedding=embedding))
    await session.commit()


async def _audit_text_for(op_id: str) -> str:
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return json.dumps([[row.payload, row.raw_payload] for row in rows], default=str)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    [
        _QUERY_ASSIGNED,
        _QUERY_USAGE,
        "POST:/LicenseManager/{moId}/DecodeLicense",
        "POST:/LicenseManager/{moId}/AddLicense",
        "PUT:/api/v1/licenses/{id}",
        "PATCH:/api/licence",
        "DELETE:/v1/license-keys/{key}",
    ],
)
def test_rule_matches_every_method_of_a_licence_path(op_id: str) -> None:
    assert is_license_read_op_id(op_id) is True


@pytest.mark.parametrize(
    "op_id",
    ["POST:/v1/hosts", "fixture.license.list", "fixture.license.assign"],
)
def test_rule_leaves_other_paths_and_hand_written_ops_alone(op_id: str) -> None:
    assert is_license_read_op_id(op_id) is False


def test_rule_never_matches_a_row_without_a_method() -> None:
    assert is_license_read(None, "/LicenseManager/{moId}/QueryLicenseUsage") is False
    assert is_license_read("post", "/LicenseManager/{moId}/QueryLicenseUsage") is True


# ---------------------------------------------------------------------------
# A fresh load leaves the POST licence rows off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_reads_leaves_post_licence_rows_off(
    session: AsyncSession, embedding: list[float]
) -> None:
    for op_id in (_QUERY_ASSIGNED, _QUERY_USAGE, _HOSTS):
        session.add(_descriptor(op_id=op_id, is_enabled=False, embedding=embedding))
    await session.commit()

    scope = ConnectorScope(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, tenant_id=_TENANT)
    assert await bulk_enable_read_ops(session, scope) == 1
    await session.commit()

    query = select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
    rows = (await session.execute(query)).scalars().all()
    assert {row.op_id: row.is_enabled for row in rows} == {
        _QUERY_ASSIGNED: False,
        _QUERY_USAGE: False,
        _HOSTS: True,
    }


# ---------------------------------------------------------------------------
# A call and a preview refuse it before any vendor call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("is_enabled", [True, False])
@pytest.mark.parametrize("op_id", [_QUERY_ASSIGNED, _QUERY_USAGE])
async def test_call_of_a_post_licence_read_answers_license_read_off(
    op_id: str, is_enabled: bool, session: AsyncSession, embedding: list[float]
) -> None:
    await _seed(session, embedding, is_enabled=is_enabled)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "license_read_off"
    assert _KeyReturningConnector.calls == 0
    assert _FAKE_KEY not in json.dumps(result.model_dump(mode="json"))
    assert _FAKE_KEY not in await _audit_text_for(op_id)


@pytest.mark.asyncio
async def test_call_with_reveal_secret_is_refused_the_same_way(
    session: AsyncSession, embedding: list[float]
) -> None:
    await _seed(session, embedding, is_enabled=True)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_QUERY_ASSIGNED,
        target=_FakeTarget(),
        params={"reveal_secret": True},
    )

    assert result.extras["error_code"] == "license_read_off"
    assert _KeyReturningConnector.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("is_enabled", [True, False])
async def test_preview_of_a_post_licence_read_answers_license_read_off(
    is_enabled: bool, session: AsyncSession, embedding: list[float]
) -> None:
    await _seed(session, embedding, is_enabled=is_enabled)

    out = await preview_dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_QUERY_ASSIGNED,
        target=_FakeTarget(),
        params={},
    )

    assert out["status"] == "error"
    assert out["extras"]["error_code"] == "license_read_off"
    assert _KeyReturningConnector.calls == 0


# ---------------------------------------------------------------------------
# A list of licence keys is hidden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["licenseKeys", "licenceKeys", "license_keys", "licence-keys", "LicenseKeys", "licenseKey"],
)
def test_licence_key_names_are_secret_names(name: str) -> None:
    assert is_secret_param_name(name) is True
    assert is_secret_param_name_lowercased(name.lower()) is True


def _licence_source() -> dict[str, Any]:
    """The licence source shape of a vSphere licence usage answer."""
    return {
        "source": {
            "_typeName": "LocalLicenseSource",
            "licenseKeys": [_FAKE_KEY, _OTHER_FAKE_KEY],
        },
        "sourceAvailable": True,
        "licenceKeys": [_OTHER_FAKE_KEY],
    }


@pytest.mark.parametrize("op_id", ["fixture.license.usage", _QUERY_USAGE])
def test_default_view_of_an_answer_hides_licence_key_lists(op_id: str) -> None:
    shown = hide_secret_values(op_id, _licence_source())

    text = json.dumps(shown)
    assert _FAKE_KEY not in text
    assert _OTHER_FAKE_KEY not in text
    assert shown["sourceAvailable"] is True
    assert shown["source"]["_typeName"] == "LocalLicenseSource"


def test_default_view_of_a_hand_written_licence_answer_masks_each_key() -> None:
    answer = {"licenses": [{"licenseKey": _FAKE_KEY, "name": "vCenter", "total": 4}]}

    shown = hide_secret_values("fixture.license.list", answer)

    assert _FAKE_KEY not in json.dumps(shown)
    assert shown["licenses"][0]["name"] == "vCenter"


def test_audit_copy_of_an_answer_hides_licence_key_lists() -> None:
    scrubbed, found = scrub_secret_named_values(_licence_source())

    assert found is True
    assert _FAKE_KEY not in json.dumps(scrubbed)
    assert _OTHER_FAKE_KEY not in json.dumps(scrubbed)


def test_broadcast_params_hide_licence_key_lists() -> None:
    scrubbed, found = scrub_broadcast_params({"licenseKeys": [_FAKE_KEY], "host": "h1"})

    assert found is True
    assert _FAKE_KEY not in json.dumps(scrubbed)
    assert scrubbed["host"] == "h1"


def test_approval_params_hide_licence_key_lists() -> None:
    hidden = params_with_secret_fields_hidden({"spec": {"licenceKeys": [_FAKE_KEY]}, "n": 1})

    assert hidden is not None
    assert _FAKE_KEY not in json.dumps(hidden)
    assert hidden["n"] == 1


def test_vendor_error_text_hides_licence_key_lists() -> None:
    text = 'fault: {"licenseKeys": ["' + _FAKE_KEY + '"], "reason": "bad"}'

    scrubbed = scrub_secret_fields_in_text(text)

    assert _FAKE_KEY not in scrubbed
    assert '"reason": "bad"' in scrubbed
