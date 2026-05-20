# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for ``extra="forbid"`` on every public v1 request schema.

G0.9-T2 (#729) of Initiative #737. Surfaced by the 2026-05-20 RDC
in-lab dogfood as "Signal #2": a v0.2.1 client posting the
pre-rename ``q`` / ``top_k`` keys to ``/api/v1/retrieve`` saw the
fields silently dropped (Pydantic v2 default ``extra="ignore"``) and
got either a confusing required-field error or a request that ran
with the defaults. The fix: every public v1 **request** schema sets
``model_config = ConfigDict(extra="forbid", ...)`` so unknown keys
surface as 422 ``extra_forbidden`` at the framework boundary.

Two complementary layers of coverage:

1. **Schema-level tests** — pure Pydantic ``model_validate`` calls
   against every touched request model, asserting that an extra
   field raises a :class:`ValidationError` with a
   ``type == "extra_forbidden"`` detail. These are fast and don't
   depend on the FastAPI test harness or the JWT mock; they are the
   regression line that catches a future drop of ``extra="forbid"``
   from any one schema.
2. **Integration test against ``/api/v1/retrieve``** — exercises the
   exact signal #2 payload (``{"q": "vault", "top_k": 3}``) through
   the route and asserts a 422 with ``extra_forbidden`` in the
   detail, not a 400 or a silent 200. This is the contract the
   consumer dogfood report named — the schema-level test alone
   would not catch a route-layer middleware mis-mapping the error.

Inventory covered (every request-body ``BaseModel`` backing a public
v1 POST / PATCH / PUT, plus :class:`CallOperationBody` outside
``api/v1/`` per the issue scope):

* :class:`meho_backplane.api.v1.retrieve.RetrieveRequest`
* :class:`meho_backplane.api.v1.audit_models.AuditQueryRequest`
* :class:`meho_backplane.api.v1.retrieve_eval.EvalRequest`
* :class:`meho_backplane.api.v1.retrieve_retire.RetireChecklistRequest`
* :class:`meho_backplane.api.v1.kb.KbEntryCreate`
* :class:`meho_backplane.api.v1.kb.IngestKbRequest`
* :class:`meho_backplane.targets.schemas.TargetCreate`
* :class:`meho_backplane.targets.schemas.TargetUpdate`
* :class:`meho_backplane.operations.ingest.api_schemas.IngestRequest`
* :class:`meho_backplane.operations.ingest.api_schemas.SpecSource`
  (nested in ``IngestRequest.specs``)
* :class:`meho_backplane.operations.ingest.api_schemas.EditGroupBody`
* :class:`meho_backplane.operations.ingest.api_schemas.EditOpBody`
* :class:`meho_backplane.operations.meta_tools.CallOperationBody`
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from meho_backplane.api.v1.audit_models import AuditQueryRequest
from meho_backplane.api.v1.kb import IngestKbRequest, KbEntryCreate
from meho_backplane.api.v1.retrieve import RetrieveRequest
from meho_backplane.api.v1.retrieve import router as retrieve_router
from meho_backplane.api.v1.retrieve_eval import EvalRequest
from meho_backplane.api.v1.retrieve_retire import RetireChecklistRequest
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest.api_schemas import (
    EditGroupBody,
    EditOpBody,
    IngestRequest,
    SpecSource,
)
from meho_backplane.operations.meta_tools import CallOperationBody
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Schema-level coverage — one parametrised test per request model
# ---------------------------------------------------------------------------

#: (model, minimal-valid-payload) for every public v1 request schema.
#:
#: The minimal-valid-payload satisfies each model's required fields so
#: the only thing distinguishing the test from a happy construction is
#: the addition of one unknown key. Future schemas should append here
#: rather than copy-paste a new parametrised case.
_REQUEST_MODELS: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (RetrieveRequest, {"query": "vault"}),
    (AuditQueryRequest, {}),
    (EvalRequest, {}),
    (RetireChecklistRequest, {}),
    (KbEntryCreate, {"slug": "k8s-ingress", "body": "Ingress runbook"}),
    (IngestKbRequest, {"directory": "/tmp/kb"}),
    (
        TargetCreate,
        {"name": "rdc-vcenter", "product": "vsphere", "host": "vc.example.lab"},
    ),
    (TargetUpdate, {"host": "vc-2.example.lab"}),
    (
        IngestRequest,
        {
            "product": "vsphere",
            "version": "8.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "/abs/spec.yaml"}],
        },
    ),
    (SpecSource, {"uri": "/abs/spec.yaml"}),
    (EditGroupBody, {"when_to_use": "for vCenter VM ops"}),
    (EditOpBody, {"safety_level": "safe"}),
    (
        CallOperationBody,
        {"connector_id": "vmware-rest-8.0", "op_id": "vm.list"},
    ),
]


@pytest.mark.parametrize(
    ("model", "payload"),
    _REQUEST_MODELS,
    ids=[m.__name__ for m, _ in _REQUEST_MODELS],
)
def test_request_schema_rejects_unknown_field_with_extra_forbidden(
    model: type[BaseModel],
    payload: dict[str, Any],
) -> None:
    """Every public v1 request schema rejects an unknown field at 422.

    The chosen unknown key (``__unexpected__``) is deliberately not a
    legitimate field on any of the touched models — a future rename
    that happens to land on this name would surface as a test failure,
    which is the desired signal.

    Asserting on ``type == "extra_forbidden"`` (rather than just
    "any ValidationError") pins the **shape** of the failure, not
    just its existence. A schema accidentally dropping
    ``extra="forbid"`` and gaining a different constraint that
    happens to fail on the unknown key would still pass a bare
    ``pytest.raises(ValidationError)``; the explicit type check
    catches that drift.
    """
    valid = model.model_validate(payload)
    assert valid is not None

    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({**payload, "__unexpected__": "value"})

    errors = exc_info.value.errors()
    extra_forbidden = [e for e in errors if e["type"] == "extra_forbidden"]
    assert extra_forbidden, (
        f"{model.__name__} did not raise extra_forbidden on unknown field; errors={errors!r}"
    )
    assert extra_forbidden[0]["loc"] == ("__unexpected__",)


def test_ingest_request_nested_spec_source_also_forbids_unknown_fields() -> None:
    """``IngestRequest.specs[*]`` is a :class:`SpecSource` — nested forbid.

    The schema-level test above proves :class:`SpecSource` in
    isolation; this case proves the constraint cascades when the
    nested model is constructed via the parent's validator. A future
    refactor that re-shapes ``specs`` (e.g. inlines the field shape
    back into :class:`IngestRequest`) must not regress the nested
    strictness — the dogfood signal hit nested keys too.
    """
    with pytest.raises(ValidationError) as exc_info:
        IngestRequest.model_validate(
            {
                "product": "vsphere",
                "version": "8.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "/abs/spec.yaml", "__unexpected__": "x"}],
            },
        )
    errors = exc_info.value.errors()
    extra_forbidden = [e for e in errors if e["type"] == "extra_forbidden"]
    assert extra_forbidden
    assert extra_forbidden[0]["loc"] == ("specs", 0, "__unexpected__")


# ---------------------------------------------------------------------------
# Integration test — exercises the signal-#2 payload through the route
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test.

    Mirrors :mod:`tests.test_api_v1_retrieve` so the integration case
    boots the same way the existing route tests do.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def _log_buffer() -> Iterator[io.StringIO]:
    """Redirect structlog into an in-memory buffer for the integration test.

    Without this fixture the AuditMiddleware logs would land on stdout
    and pollute the pytest output. Same shape as
    :func:`tests.test_api_v1_retrieve.log_buffer`.
    """
    buf = io.StringIO()
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    yield buf
    structlog.reset_defaults()


@pytest.fixture
def _retrieve_client(_log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a FastAPI app with the retrieve route mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(retrieve_router)
    yield TestClient(app)


def test_retrieve_route_rejects_signal_2_payload_with_422_extra_forbidden(
    _retrieve_client: TestClient,
) -> None:
    """``POST /api/v1/retrieve`` rejects the exact dogfood Signal #2 payload.

    The 2026-05-20 RDC report's reproducer was a v0.2.1 client still
    sending ``q`` (the pre-rename field) and ``top_k`` (renamed to
    ``limit``) — that payload used to be silently dropped, producing
    a 422 missing-``query`` error and an apparent "request worked
    with weird defaults" experience. The fix: 422 with one
    ``extra_forbidden`` error per unknown field, surfacing the
    actual problem to the caller.

    Patches :func:`retrieve` so the test never touches the substrate
    even though it should never get past validation either — the
    patch is defence-in-depth in case the route's order of checks
    drifts (the goal is failing validation before retrieval is
    attempted).
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-extras", tenant_role=TenantRole.OPERATOR.value)
    fake_retrieve = AsyncMock(return_value=[])

    with (
        respx.mock as mock_router,
        patch("meho_backplane.api.v1.retrieve.retrieve", new=fake_retrieve),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = _retrieve_client.post(
            "/api/v1/retrieve",
            json={"q": "vault", "top_k": 3},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    extra_forbidden = [d for d in detail if d.get("type") == "extra_forbidden"]
    # Both unknown fields should be flagged so the caller sees both at once
    # rather than playing whack-a-mole through repeated re-submissions.
    flagged = {tuple(d["loc"]) for d in extra_forbidden}
    assert ("body", "q") in flagged
    assert ("body", "top_k") in flagged

    # The retrieval substrate must not be invoked when validation fails
    # — the framework rejects the body before the handler body runs.
    fake_retrieve.assert_not_awaited()
