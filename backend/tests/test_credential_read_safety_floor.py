# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op class floor for credential reads.

An operation that a connector declares as ``credential_read`` (the class
that hides the values of its answer) is at least ``caution`` and waits for a
person's approval, unless its connector marked it in code as one that
returns no secret value. For an ingested operation the floor is part of
``apply_safety_floor``, so it needs no catalog row and no ``safety_overlay``
list. These tests check that:

* a declared credential read comes out ``caution`` with approval, with no
  catalog row for its product;
* a read without the class keeps the parser's result;
* the floor only raises, and an overlay rule can raise it further;
* a credential read counts as governed, so registration stores the raised
  level and a re-ingest never lowers a stricter stored row;
* the floor also holds at every lookup (dispatch, the call contract,
  search, the operation read, the connector review, the console page and
  the preview),
  for a typed and an ingested read stored as ``safe``, also after an
  override that turned approval off;
* a read marked as one that returns no secret value stays ``safe``: the
  five built-in marked reads, and ``vault.kv.read`` only without
  ``reveal_secret``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from meho_backplane import op_classes
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.bind9 import op_classes as _bind9_op_classes  # noqa: F401
from meho_backplane.connectors.kubernetes import op_classes as _k8s_op_classes  # noqa: F401
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, OperationPolicyOverride
from meho_backplane.operations._contract import _find_descriptor
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._search import _FusedEntry, _hydrate_hits
from meho_backplane.operations.credential_read import raise_for_credential_read
from meho_backplane.operations.ingest import (
    ReviewService,
    parse_openapi,
    register_ingested_operations,
)
from meho_backplane.operations.ingest import catalog as catalog_module
from meho_backplane.operations.ingest.catalog import parse_catalog
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.meta_tools import describe_descriptor
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.connectors.detail import _load_ops_matrix

_PRODUCT = "secretfixture"
_VERSION = "1.0"
_IMPL_ID = "secretfixture-rest"

_SPEC = """
openapi: 3.1.0
info: {title: credential read fixture, version: '1.0'}
paths:
  /v1/accounts/{id}/password:
    get:
      operationId: accounts.password
      responses: {'200': {description: ok}}
  /v1/accounts:
    get:
      operationId: accounts.list
      responses: {'200': {description: ok}}
"""

_PASSWORD_READ = "GET:/v1/accounts/{id}/password"
_PLAIN_READ = "GET:/v1/accounts"

_OVERLAY_CATALOG = f"""
entries:
  - product: {_PRODUCT}
    version: "{_VERSION}"
    impl_id: {_IMPL_ID}
    requires_connector_class: SecretFixtureConnector
    safety_overlay:
      - method: GET
        path: /v1/accounts/{{id}}/password
        safety_level: dangerous
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def op_class_state() -> Iterator[None]:
    """Put every op class declaration and mark back after the test."""
    saved = dict(op_classes._DECLARED)
    saved_safe_writes = set(op_classes._SAFE_WRITES)
    saved_root_shell = set(op_classes._ROOT_SHELL)
    saved_per_call = set(op_classes._PER_CALL_APPROVAL)
    saved_no_secret_value = set(op_classes._NO_SECRET_VALUE)
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        op_classes._DECLARED.update(saved)
        op_classes._SAFE_WRITES.update(saved_safe_writes)
        op_classes._ROOT_SHELL.update(saved_root_shell)
        op_classes._PER_CALL_APPROVAL.update(saved_per_call)
        op_classes._NO_SECRET_VALUE.update(saved_no_secret_value)


@pytest.fixture
def declared_password_read(op_class_state: None) -> None:
    """Declare the password read as a credential read for one test."""
    op_classes.declare_op_classes(credential_read=(_PASSWORD_READ,))


@pytest.fixture
def overlay_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog whose only row raises the password read to dangerous."""
    catalog = parse_catalog(_OVERLAY_CATALOG)
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: catalog)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _parsed_ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///credential-read-fixture.yaml", content=_SPEC)
    return {row.op_id: row for row in rows}


def _apply(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    return apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)


def _governed(proto: EndpointDescriptorProto) -> bool:
    return has_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)


# -- applying the floor -------------------------------------------------------


def test_product_has_no_catalog_row() -> None:
    """The fixture product is in no catalog, so no overlay list can play a part."""
    assert catalog_module.load_catalog().get(_PRODUCT, _VERSION) is None


@pytest.mark.usefixtures("declared_password_read")
def test_declared_credential_read_is_caution_with_approval() -> None:
    parsed = _parsed_ops()[_PASSWORD_READ]
    assert (parsed.safety_level, parsed.requires_approval) == ("safe", False)

    result = _apply(parsed)

    assert (result.safety_level, result.requires_approval) == ("caution", True)


@pytest.mark.usefixtures("declared_password_read")
def test_declared_credential_read_is_governed() -> None:
    assert _governed(_parsed_ops()[_PASSWORD_READ])


@pytest.mark.usefixtures("declared_password_read")
def test_read_without_the_class_keeps_the_parser_result() -> None:
    plain = _parsed_ops()[_PLAIN_READ]

    assert _apply(plain) is plain
    assert not _governed(plain)


def test_undeclared_password_read_keeps_the_parser_result() -> None:
    """Without the declaration the floor does not guess from the path."""
    parsed = _parsed_ops()[_PASSWORD_READ]

    assert _apply(parsed) is parsed


@pytest.mark.usefixtures("declared_password_read")
@pytest.mark.parametrize(
    ("level", "approval"),
    [("dangerous", False), ("dangerous", True), ("destructive", True)],
)
def test_floor_keeps_a_stricter_level_and_turns_approval_on(level: str, approval: bool) -> None:
    proto = EndpointDescriptorProto(
        op_id=_PASSWORD_READ,
        method="GET",
        path="/v1/accounts/{id}/password",
        safety_level=level,  # type: ignore[arg-type]
        requires_approval=approval,
    )

    result = _apply(proto)

    assert (result.safety_level, result.requires_approval) == (level, True)


@pytest.mark.usefixtures("declared_password_read", "overlay_catalog")
def test_overlay_raises_a_credential_read_further() -> None:
    result = _apply(_parsed_ops()[_PASSWORD_READ])

    assert (result.safety_level, result.requires_approval) == ("dangerous", True)


# -- registration and re-ingest ----------------------------------------------


async def _stored(op_id: str) -> tuple[str, bool]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT,
                    EndpointDescriptor.op_id == op_id,
                )
            )
        ).scalar_one()
        return row.safety_level, row.requires_approval


async def _register(service: AsyncMock) -> None:
    await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="credential-read-fixture.yaml",
        operations=list(_parsed_ops().values()),
        embedding_service=service,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("declared_password_read")
async def test_register_stores_caution_with_approval(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)

    assert await _stored(_PASSWORD_READ) == ("caution", True)
    assert await _stored(_PLAIN_READ) == ("safe", False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("declared_password_read")
async def test_reingest_keeps_a_stricter_stored_row(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.product == _PRODUCT,
                EndpointDescriptor.op_id == _PASSWORD_READ,
            )
            .values(safety_level="dangerous")
        )
        await session.commit()

    await _register(stub_embedding_service)

    assert await _stored(_PASSWORD_READ) == ("dangerous", True)


@pytest.mark.asyncio
async def test_reingest_raises_a_row_stored_before_the_declaration(
    stub_embedding_service: AsyncMock,
) -> None:
    """A row stored while the read had no class is raised by the next ingest."""
    await _register(stub_embedding_service)
    assert await _stored(_PASSWORD_READ) == ("safe", False)

    saved = dict(op_classes._DECLARED)
    op_classes.declare_op_classes(credential_read=(_PASSWORD_READ,))
    try:
        await _register(stub_embedding_service)
    finally:
        op_classes._DECLARED.clear()
        op_classes._DECLARED.update(saved)

    assert await _stored(_PASSWORD_READ) == ("caution", True)


# -- every lookup -------------------------------------------------------------

_TYPED_KEY = ("credtyped", "1.x", "credtyped", "credtyped.account.password")
_INGESTED_KEY = ("credspec", "1.x", "credspec", "GET:/v1/accounts/{id}/password")


def _admin(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub=f"sub-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _seed_safe_row(key: tuple[str, str, str, str], *, source_kind: str) -> uuid.UUID:
    """Store an enabled built-in row as ``safe`` with no approval, in its own group.

    The way a typed connector whose code says ``safe`` stores it, and the
    way an ingested row stored before the floor existed looks.
    """
    product, version, impl_id, op_id = key
    group_id, descriptor_id = uuid.uuid4(), uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key="secrets",
                name="Secrets",
                when_to_use="when you need a password",
                review_status="enabled",
            )
        )
        session.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                source_kind=source_kind,
                method="GET",
                path="/v1/accounts/{id}/password",
                summary="Read a password.",
                description="Read a password.",
                group_id=group_id,
                parameter_schema={"type": "object", "properties": {}},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
            )
        )
    return descriptor_id


async def _every_surface(
    key: tuple[str, str, str, str], descriptor_id: uuid.UUID, operator: Operator
) -> dict[str, tuple[str, bool]]:
    """Read the risk level and approval of one operation on every surface."""
    product, version, impl_id, op_id = key
    tenant_id = operator.tenant_id
    seen: dict[str, tuple[str, bool]] = {}

    looked_up = await lookup_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=op_id
    )
    assert looked_up is not None
    seen["dispatch"] = (looked_up.safety_level, looked_up.requires_approval)

    found = await _find_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=op_id
    )
    assert found is not None
    seen["call contract"] = (found[0].safety_level, found[0].requires_approval)

    async with get_sessionmaker()() as session:
        (hit,) = await _hydrate_hits(session, [_FusedEntry(descriptor_id)], tenant_id=tenant_id)
        seen["search"] = (hit.safety_level, hit.requires_approval)
        (group,) = await _load_ops_matrix(
            session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
        )
        (row,) = group.operations
        seen["console page"] = (row.safety_level, row.requires_approval)

    described = await describe_descriptor(operator, descriptor_id)
    assert described is not None
    seen["operation read"] = (described.safety_level, described.requires_approval)

    payload = await ReviewService(operator).get_review_payload(f"{impl_id}-{version}", tenant_id)
    ops = [*payload.ungrouped_ops, *(op for group in payload.groups for op in group.ops)]
    (reviewed,) = [op for op in ops if op.op_id == op_id]
    seen["connector review"] = (reviewed.safety_level, reviewed.requires_approval)

    previewed = await preview_dispatch(
        operator=operator,
        connector_id=f"{impl_id}-{version}",
        op_id=op_id,
        target=SimpleNamespace(
            id=uuid.uuid4(),
            name=f"{product}-target",
            product=product,
            fingerprint=SimpleNamespace(version="1.0"),
            preferred_impl_id=impl_id,
        ),
        params={"id": "1"},
    )
    assert "safety_level" in previewed, (previewed.get("error"), previewed.get("extras"))
    seen["preview"] = (previewed["safety_level"], previewed["requires_approval"])
    return seen


@pytest.mark.asyncio
@pytest.mark.usefixtures("op_class_state")
@pytest.mark.parametrize(
    ("key", "source_kind"), [(_TYPED_KEY, "typed"), (_INGESTED_KEY, "ingested")]
)
async def test_unmarked_credential_read_is_caution_with_approval_on_every_surface(
    key: tuple[str, str, str, str], source_kind: str
) -> None:
    op_classes.declare_op_classes(credential_read=(key[3],))
    descriptor_id = await _seed_safe_row(key, source_kind=source_kind)

    seen = await _every_surface(key, descriptor_id, _admin(uuid.uuid4()))

    assert seen == dict.fromkeys(seen, ("caution", True))
    assert len(seen) == 7


@pytest.mark.asyncio
@pytest.mark.usefixtures("op_class_state")
@pytest.mark.parametrize(
    ("key", "source_kind"), [(_TYPED_KEY, "typed"), (_INGESTED_KEY, "ingested")]
)
async def test_marked_credential_read_stays_safe_on_every_surface(
    key: tuple[str, str, str, str], source_kind: str
) -> None:
    op_classes.declare_op_classes(credential_read=(key[3],))
    op_classes.declare_no_secret_value((key[3],))
    descriptor_id = await _seed_safe_row(key, source_kind=source_kind)

    seen = await _every_surface(key, descriptor_id, _admin(uuid.uuid4()))

    assert seen == dict.fromkeys(seen, ("safe", False))


@pytest.mark.asyncio
@pytest.mark.usefixtures("op_class_state")
async def test_override_that_turns_approval_off_does_not_lower_a_credential_read() -> None:
    """An install-wide override with approval off and ``safe`` changes nothing."""
    product, version, impl_id, op_id = _TYPED_KEY
    op_classes.declare_op_classes(credential_read=(op_id,))
    descriptor_id = await _seed_safe_row(_TYPED_KEY, source_kind="typed")
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                safety_level="safe",
                requires_approval=False,
                reason="fixture: approval turned off",
                set_by_sub="fixture-admin",
            )
        )

    seen = await _every_surface(_TYPED_KEY, descriptor_id, _admin(uuid.uuid4()))

    assert seen == dict.fromkeys(seen, ("caution", True))


@pytest.mark.asyncio
@pytest.mark.usefixtures("op_class_state")
async def test_floor_keeps_a_stricter_stored_level_at_lookup() -> None:
    product, version, impl_id, op_id = _TYPED_KEY
    op_classes.declare_op_classes(credential_read=(op_id,))
    await _seed_safe_row(_TYPED_KEY, source_kind="typed")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == op_id)
            .values(safety_level="destructive")
        )

    looked_up = await lookup_descriptor(
        tenant_id=uuid.uuid4(), product=product, version=version, impl_id=impl_id, op_id=op_id
    )

    assert looked_up is not None
    assert (looked_up.safety_level, looked_up.requires_approval) == ("destructive", True)


# -- the built-in marked reads ------------------------------------------------

_MARKED_BUILT_IN_READS = (
    "vault.kv.list",
    "vault.kv.compare",
    "vault.kv.read",
    "k8s.secret.compare_to_ref",
    "bind9.config.show",
)


@pytest.mark.parametrize("op_id", _MARKED_BUILT_IN_READS)
def test_built_in_marked_read_stays_safe(op_id: str) -> None:
    assert classify_op(op_id) == "credential_read"

    assert raise_for_credential_read(op_id, safety_level="safe", requires_approval=False) == (
        "safe",
        False,
    )


@pytest.mark.parametrize("op_id", [op for op in _MARKED_BUILT_IN_READS if op != "vault.kv.read"])
def test_reveal_secret_does_not_raise_a_read_that_never_returns_a_value(op_id: str) -> None:
    assert raise_for_credential_read(
        op_id, safety_level="safe", requires_approval=False, reveal_secret=True
    ) == ("safe", False)


def test_kv_read_with_reveal_secret_is_caution_with_approval() -> None:
    assert raise_for_credential_read(
        "vault.kv.read", safety_level="safe", requires_approval=False, reveal_secret=True
    ) == ("caution", True)


def test_read_to_ref_is_not_marked() -> None:
    """The only other built-in credential read keeps its floor."""
    assert classify_op("k8s.secret.read_to_ref") == "credential_read"

    assert raise_for_credential_read(
        "k8s.secret.read_to_ref", safety_level="safe", requires_approval=False
    ) == ("caution", True)


def test_a_plain_read_with_reveal_secret_is_not_raised() -> None:
    assert raise_for_credential_read(
        "vault.kv.versions", safety_level="safe", requires_approval=False, reveal_secret=True
    ) == ("safe", False)
