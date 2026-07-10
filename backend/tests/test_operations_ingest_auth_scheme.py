# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-selectable auth scheme on non-catalog ingest (#2289).

Initiative #2271 / Goal #221. An operator ingesting an arbitrary spec may
*select* a named auth scheme from the closed catalog (plus, optionally, the
secret-field NAMES it reads). When they do, the register phase synthesises a
minimal :class:`ExecutionProfile` and stamps a dispatchable
:class:`ProfiledRestConnector` under the triple — staged behind the #1971
review gate — instead of the non-dispatchable bare
:class:`GenericRestConnector` shim. When they don't, the historical bare-shim
behaviour is byte-identical.

The tests pin the task's acceptance criteria:

* **Dispatchability contrast** — ingest *with* ``auth_scheme`` yields a
  profiled connector whose ``auth_headers`` mint a Bearer token against a
  mocked upstream login (dispatchable); the same ingest *without* the flag
  yields the non-dispatchable bare shim.
* **Closed-set rejection** — an unknown / reserved scheme is rejected at the
  API boundary (the request schema) with a closed-set error naming the
  allowed members; reserved typed-only shapes are not selectable.
* **Secrets by reference** — the request carries secret-field *names* only;
  no credential-value field exists on the schema.
* **Review gate (#1971)** — the stamped connector's ops stay
  ``is_enabled=False`` / ``review_status='staged'``; stamping never
  auto-enables dispatch.

The DB-backed tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`; each clears the
process-global v2 registry around itself.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import shim_kind
from meho_backplane.connectors.profile import NAMED_AUTH_SCHEMES, AuthSchemeName
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest.api_schemas import IngestRequest, SpecSource
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    resolve_authoring_kind,
)
from meho_backplane.operations.ingest.ingest_profile import (
    DEFAULT_SECRET_FIELDS,
    DEFAULT_STATIC_HEADER_VALUE_KIND,
    build_ingest_execution_profile,
)
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

_PRODUCT = "acme"
_VERSION = "1.2"
_IMPL_ID = "acme-rest"
_CONNECTOR_ID = "acme-rest-1.2"

_SPEC_YAML = """
openapi: "3.1.0"
info:
  title: Acme fixture
  version: "1.2.0"
paths:
  /things:
    get:
      operationId: listThings
      summary: List things
      responses:
        "200":
          description: A list of things.
"""


# ---------------------------------------------------------------------------
# Unit: profile synthesis + secret-field defaults
# ---------------------------------------------------------------------------


def test_default_secret_fields_cover_named_schemes() -> None:
    """Every named scheme in the closed catalog has a reviewed secret-field default."""
    assert set(DEFAULT_SECRET_FIELDS) == NAMED_AUTH_SCHEMES


@pytest.mark.parametrize("scheme", sorted(NAMED_AUTH_SCHEMES))
def test_build_profile_is_valid_for_every_named_scheme(scheme: str) -> None:
    """A minimal profile synthesises + validates for every selectable scheme."""
    profile = build_ingest_execution_profile(
        product=_PRODUCT,
        version=_VERSION,
        auth_scheme=scheme,  # type: ignore[arg-type]
    )
    assert profile.product == _PRODUCT
    assert profile.version == _VERSION
    assert profile.auth.scheme == scheme
    assert profile.auth.secret_fields == DEFAULT_SECRET_FIELDS[scheme]
    # value_kind is bound to static_header only (AuthSpec enforces it).
    if scheme == "static_header":
        assert profile.auth.value_kind == DEFAULT_STATIC_HEADER_VALUE_KIND
    else:
        assert profile.auth.value_kind is None


def test_build_profile_honours_secret_fields_override() -> None:
    """An explicit secret-field name list overrides the per-scheme default."""
    profile = build_ingest_execution_profile(
        product=_PRODUCT,
        version=_VERSION,
        auth_scheme="static_header",
        secret_fields=("api_key",),
    )
    assert profile.auth.secret_fields == ("api_key",)


def test_build_profile_rejects_empty_secret_fields() -> None:
    """An empty override is a malformed selection (AuthSpec fails closed)."""
    with pytest.raises(ValidationError):
        build_ingest_execution_profile(
            product=_PRODUCT,
            version=_VERSION,
            auth_scheme="basic",
            secret_fields=(),
        )


# ---------------------------------------------------------------------------
# Unit: API-boundary schema contract
# ---------------------------------------------------------------------------


def _base_request_kwargs() -> dict[str, Any]:
    return {
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL_ID,
        "specs": [SpecSource(uri="https://acme.test/spec.yaml")],
    }


def test_unknown_or_reserved_scheme_rejected_naming_allowed_members() -> None:
    """A reserved typed-only scheme is rejected at the boundary, naming the closed set."""
    with pytest.raises(ValidationError) as exc:
        IngestRequest(**_base_request_kwargs(), auth_scheme="github_app_jwt")
    message = str(exc.value)
    # The closed-set error names the allowed members (pydantic Literal error).
    assert "session_login_token" in message
    assert "github_app_jwt" in message


def test_auth_secret_fields_requires_a_scheme() -> None:
    """Naming secret fields without selecting a scheme is a 422."""
    with pytest.raises(ValidationError) as exc:
        IngestRequest(**_base_request_kwargs(), auth_secret_fields=["username"])
    assert "requires 'auth_scheme'" in str(exc.value)


def test_auth_scheme_mutually_exclusive_with_catalog_entry() -> None:
    """auth_scheme is the non-catalog on-ramp; a catalog row binds its own profile."""
    with pytest.raises(ValidationError) as exc:
        IngestRequest(catalog_entry="acme/1.2", auth_scheme="basic")
    assert "catalog_entry_conflict" in str(exc.value)


def test_no_credential_value_field_on_the_schema() -> None:
    """The request carries field NAMES only — never a credential-value field."""
    fields = set(IngestRequest.model_fields)
    # The only auth fields are the scheme selector and the secret-field NAMES.
    assert "auth_scheme" in fields
    assert "auth_secret_fields" in fields
    # No field that would carry credential values.
    for banned in ("password", "secret", "secrets", "credentials", "token", "api_key"):
        assert banned not in fields


# ---------------------------------------------------------------------------
# DB-backed: ingest register phase stamps a dispatchable profiled connector
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars engine / Operator construction depend on transitively."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None = 443
    secret_ref: str | None = "p/secret"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _stub_embedding() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _run_ingest(
    *,
    auth_scheme: AuthSchemeName | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the full pipeline for the acme fixture, stubbing only grouping.

    Grouping needs an LLM; the register + stamp phases (the #2289 surface) do
    not, so the grouping phase is stubbed to an empty result while the real
    register phase — including the profile stamp — runs against sqlite.
    """
    service = IngestionPipelineService(
        _operator(),
        sessionmaker=get_sessionmaker(),
        embedding_service=_stub_embedding(),
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=_CONNECTOR_ID,
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=1,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)

    await service.ingest(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        specs=[SpecSource(uri="fixture://acme/spec.yaml", content=_SPEC_YAML)],
        tenant_id=None,
        dry_run=False,
        auth_scheme=auth_scheme,
    )


async def _descriptor_states() -> dict[str, bool]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
        return {r.op_id: r.is_enabled for r in rows}


@pytest.mark.asyncio
async def test_ingest_with_auth_scheme_stamps_dispatchable_profiled_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest with --auth-scheme yields a dispatchable, review-gated profiled connector."""
    await _run_ingest(auth_scheme="session_login_token", monkeypatch=monkeypatch)

    # The triple resolves to a ProfiledRestConnector carrying the synthesised
    # profile — the dispatchable ("profiled") tier, not the bare shim.
    registered = all_connectors_v2()[(_PRODUCT, _VERSION, _IMPL_ID)]
    assert issubclass(registered, ProfiledRestConnector)
    assert shim_kind(registered) == "profiled"
    assert registered.profile is not None
    assert registered.profile.auth.scheme == "session_login_token"
    assert registered.profile.auth.secret_fields == ("username", "password")

    # #1971: stamping did NOT clear the review gate — the op stays staged.
    states = await _descriptor_states()
    assert states, "expected the ingested op to be persisted"
    assert all(enabled is False for enabled in states.values())
    # The read-surface projection reports it as dispatchable-but-unreviewed
    # (not yet dispatchable until an operator enables an op).
    assert resolve_authoring_kind(
        product=_PRODUCT, version=_VERSION, enabled_operation_count=0
    ) == ("profiled-but-unreviewed", False)

    # Dispatch proof: the stamped connector authenticates against a mocked
    # upstream login (SDDC-shape token mint -> Bearer). Instantiate the
    # registered class with a stub credentials loader (no Vault) — the profile
    # rides as a class attribute.
    async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
        return {"username": "svc", "password": "pw"}

    connector = registered(credentials_loader=_loader)
    target = _StubTarget(name="sddc", host="sddc.invalid")
    async with respx.mock(base_url="https://sddc.invalid") as mock:
        route = mock.post("/v1/tokens").respond(200, json={"accessToken": "acc-xyz"})
        headers = await connector.auth_headers(target, operator=_operator())
    assert headers == {"Authorization": "Bearer acc-xyz"}
    # Only field names ride the login body — the values come from the loader.
    assert json.loads(route.calls[0].request.read().decode()) == {
        "username": "svc",
        "password": "pw",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_ingest_without_auth_scheme_registers_non_dispatchable_bare_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ingest without the flag yields today's non-dispatchable bare shim."""
    await _run_ingest(auth_scheme=None, monkeypatch=monkeypatch)

    registered = all_connectors_v2()[(_PRODUCT, _VERSION, _IMPL_ID)]
    assert issubclass(registered, GenericRestConnector)
    assert shim_kind(registered) == "bare"
    assert resolve_authoring_kind(
        product=_PRODUCT, version=_VERSION, enabled_operation_count=0
    ) == ("ingested-shim", False)
