# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO refuses an API file whose paths look like Kubernetes paths without the usual start.

The usual start is ``/api/v1`` or ``/apis/<group>/<version>``. The parser
refuses the whole file, so the dry run, the ingest and the start check of
the shipped files refuse it too, and nothing is stored. A file with full
paths, or with the start as a relative server address, loads. At start,
MEHO writes one warning line for each connector with such stored
operations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from importlib import resources
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes.short_path_warning import (
    warn_stored_short_kubernetes_paths,
)
from meho_backplane.connectors.kubernetes.short_paths import (
    full_kubernetes_path,
    refuse_short_kubernetes_paths,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, SpecProvenance
from meho_backplane.operations.ingest import UnsupportedSpecError, build_unsupported_spec_detail
from meho_backplane.operations.ingest.api_schemas import SpecSource
from meho_backplane.operations.ingest.catalog import load_catalog, load_entry_spec
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.openapi import (
    parse_openapi,
    parse_openapi_with_provenance,
)
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _spec(paths: list[str], *, server: str | None = None) -> str:
    doc: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "Made-up API", "version": "1.0.0"},
        "paths": {path: {"get": {"responses": {"200": {"description": "ok"}}}} for path in paths},
    }
    if server is not None:
        doc["servers"] = [{"url": server}]
    return yaml.safe_dump(doc, sort_keys=False)


def _parse(paths: list[str], *, server: str | None = None) -> list[str]:
    protos = parse_openapi("spec:made-up.yaml", content=_spec(paths, server=server))
    return [proto.op_id for proto in protos]


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------

_SHAPE_A = [
    "/namespaces/{namespace}/secrets",
    "/namespaces/{namespace}/secrets/{name}",
    "/watch/namespaces/{namespace}/secrets",
    "/x/namespaces/{namespace}/secrets",
    "/namespaces/{namespace}/pods/{name}/exec",
    "/namespaces/{namespace}/pods/{name}/attach",
    "/namespaces/{namespace}/pods/{name}/portforward",
    "/namespaces/{namespace}/pods/{name}/proxy/{path}",
    "/namespaces/{namespace}/serviceaccounts/{name}/token",
    "/clusters/c-1/namespaces/prod/secrets/db",
    "/namespaces/prod/pods/web-1/exec",
    "/namespaces/{namespace}/pods/{name}/proxy/api/v1/health",
    "/namespaces/{namespace}/pods/{name}/proxy/apis/apps/v1/status",
    "/x/namespaces/{namespace}/pods/{name}/proxy/api/v1/health",
    "/x/namespaces/{namespace}/pods/{name}/proxy/apis/apps/v1/status",
]

_CORE_PLURALS = [
    "pods",
    "services",
    "configmaps",
    "secrets",
    "serviceaccounts",
    "persistentvolumeclaims",
    "endpoints",
    "events",
    "limitranges",
    "resourcequotas",
    "replicationcontrollers",
    "podtemplates",
    "bindings",
]

_SHAPE_B = [
    *(f"/namespaces/{{namespace}}/{plural}" for plural in _CORE_PLURALS),
    "/namespaces/prod/configmaps/app",
    "/v1/namespaces/{namespace}/services/{name}",
    "/apps/v1/namespaces/{namespace}/deployments",
    "/apps/v1/namespaces/prod/deployments/web",
    "/batch/v1/namespaces/{namespace}/jobs/{name}",
    "/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers",
    "/policy/v1/namespaces/{namespace}/poddisruptionbudgets",
    "/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}",
    "/networking.k8s.io/v1beta1/namespaces/{namespace}/ingresses",
    "/v1/apps/v1/namespaces/{namespace}/statefulsets",
]

_NOT_REFUSED = [
    "/api/v1/namespaces/{namespace}/secrets",
    "/apis/apps/v1/namespaces/{namespace}/deployments",
    "/k8s/clusters/{cluster}/api/v1/namespaces/{namespace}/secrets",
    "/nodes/{node}/qemu",
    "/namespaces/{namespace}/workflows/{id}",
    "/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts",
    "/api/namespaces/{namespace}/services/{service}",
    "/cloud/namespaces/{namespace}",
    "/helm.cattle.io/v1/namespaces/{namespace}/helmcharts",
    "/persistentvolumes",
    "/things/{id}",
    "/api/v1",
    "/api/v1/namespaces/{namespace}/pods/{name}/proxy/api/v1/health",
    "/k8s/clusters/{cluster}/api/v1/namespaces/{namespace}/pods/{name}/proxy/apis/a/v1/x",
]


@pytest.mark.parametrize(
    ("short", "full"),
    [
        ("/x/namespaces/{n}/secrets", "/api/v1/namespaces/{n}/secrets"),
        ("/v1/namespaces/{n}/pods", "/api/v1/namespaces/{n}/pods"),
        ("/apps/v1/namespaces/{n}/deployments", "/apis/apps/v1/namespaces/{n}/deployments"),
        ("/v1/apps/v1/namespaces/{n}/deployments", "/apis/apps/v1/namespaces/{n}/deployments"),
        ("/watch/namespaces/{n}/secrets", "/api/v1/watch/namespaces/{n}/secrets"),
        ("/namespaces/{n}/secrets/", "/api/v1/namespaces/{n}/secrets"),
        (
            "/x/namespaces/{n}/pods/{p}/exec/more",
            "/api/v1/namespaces/{n}/pods/{p}/exec/more",
        ),
        (
            "/namespaces/{n}/pods/{p}/proxy/api/v1/health",
            "/api/v1/namespaces/{n}/pods/{p}/proxy/api/v1/health",
        ),
        (
            "/x/namespaces/{n}/pods/{p}/proxy/apis/apps/v1/status",
            "/api/v1/namespaces/{n}/pods/{p}/proxy/apis/apps/v1/status",
        ),
    ],
)
def test_full_kubernetes_path_gives_the_full_path(short: str, full: str) -> None:
    assert full_kubernetes_path(short) == full


@pytest.mark.parametrize("path", [*_NOT_REFUSED, "", "namespaces/{n}/secrets", "/"])
def test_full_kubernetes_path_gives_none_for_other_paths(path: str) -> None:
    assert full_kubernetes_path(path) is None


@pytest.mark.parametrize("path", [*_SHAPE_A, *_SHAPE_B])
def test_a_file_with_a_short_kubernetes_path_is_refused(path: str) -> None:
    with pytest.raises(UnsupportedSpecError) as refused:
        _parse(["/things", path])
    message = str(refused.value)
    assert message.startswith("MEHO does not load this API file.")
    assert f"for example: {path}." in message
    assert "/things" not in message
    assert build_unsupported_spec_detail(refused.value)["detail"] == "unsupported_spec"


@pytest.mark.parametrize("path", [_SHAPE_A[0], _SHAPE_B[-1]])
def test_the_provenance_parser_refuses_the_same_file(path: str) -> None:
    with pytest.raises(UnsupportedSpecError):
        parse_openapi_with_provenance("spec:made-up.yaml", content=_spec([path]))


def test_the_refusal_names_at_most_three_paths() -> None:
    paths = [f"/namespaces/{{namespace}}/{plural}" for plural in _CORE_PLURALS[:5]]
    with pytest.raises(UnsupportedSpecError) as refused:
        _parse(paths)
    message = str(refused.value)
    assert f"for example: {', '.join(paths[:3])}." in message
    assert paths[3] not in message
    assert paths[4] not in message


def test_a_path_with_no_operation_is_not_read() -> None:
    doc = yaml.safe_load(_spec(["/things"]))
    doc["paths"]["/namespaces/{namespace}/secrets"] = {"summary": "no operation here"}
    protos = parse_openapi("spec:made-up.yaml", content=yaml.safe_dump(doc, sort_keys=False))
    assert [proto.op_id for proto in protos] == ["GET:/things"]
    assert refuse_short_kubernetes_paths([]) is None


@pytest.mark.parametrize("path", _NOT_REFUSED)
def test_a_file_with_other_paths_loads(path: str) -> None:
    assert _parse([path]) == [f"GET:{path}"]


def test_a_file_with_no_path_under_namespaces_loads() -> None:
    assert _parse(["/things", "/things/{id}"]) == ["GET:/things", "GET:/things/{id}"]


# ---------------------------------------------------------------------------
# The server address, and the false refusals with the way past each
# ---------------------------------------------------------------------------


def test_an_absolute_server_address_with_the_start_is_refused() -> None:
    with pytest.raises(UnsupportedSpecError):
        _parse(["/namespaces/{namespace}/secrets"], server="https://cluster.example.test/api/v1")


def test_the_start_as_a_relative_server_address_loads_with_full_paths() -> None:
    assert _parse(["/namespaces/{namespace}/secrets"], server="/api/v1") == [
        "GET:/api/v1/namespaces/{namespace}/secrets"
    ]


def test_a_cluster_behind_a_management_product_is_refused_and_the_relative_start_loads() -> None:
    with pytest.raises(UnsupportedSpecError):
        _parse(
            ["/namespaces/{namespace}/pods"],
            server="https://manager.example.test/k8s/clusters/c-1/api/v1",
        )
    assert _parse(["/namespaces/{namespace}/pods"], server="/k8s/clusters/c-1/api/v1") == [
        "GET:/k8s/clusters/c-1/api/v1/namespaces/{namespace}/pods"
    ]


def test_a_tool_with_kubernetes_names_is_refused_and_its_own_start_loads() -> None:
    paths = ["/namespaces/{namespace}/services/{service}"]
    with pytest.raises(UnsupportedSpecError):
        _parse(paths, server="https://console.example.test/api")
    assert _parse(paths, server="/api") == ["GET:/api/namespaces/{namespace}/services/{service}"]


def test_a_tool_that_shows_secrets_is_refused_with_any_server_address() -> None:
    paths = ["/clusters/{cluster}/namespaces/{namespace}/secrets"]
    for server in (None, "/api", "https://console.example.test/api"):
        with pytest.raises(UnsupportedSpecError):
            _parse(paths, server=server)


def test_another_product_with_a_core_plural_is_refused_and_its_own_start_loads() -> None:
    paths = ["/namespaces/{namespace}/events"]
    with pytest.raises(UnsupportedSpecError):
        _parse(paths, server="https://other.example.test/api/v2")
    assert _parse(paths, server="/api/v2") == ["GET:/api/v2/namespaces/{namespace}/events"]


# ---------------------------------------------------------------------------
# Every API file this repo ships loads
# ---------------------------------------------------------------------------


def _shipped_spec_files() -> list[str]:
    folder = resources.files("meho_backplane.operations.ingest.specs")
    return [
        entry.read_text(encoding="utf-8")
        for entry in folder.iterdir()
        if entry.name.endswith((".yaml", ".yml", ".json"))
    ]


def test_no_api_file_this_repo_ships_is_refused() -> None:
    contents = _shipped_spec_files()
    assert contents
    contents.extend(
        load_entry_spec(entry) for entry in load_catalog().entries if entry.spec_resource
    )
    for content in contents:
        assert parse_openapi("spec:shipped.yaml", content=content)


# ---------------------------------------------------------------------------
# The dry run and the ingest store nothing
# ---------------------------------------------------------------------------

_PRODUCT = "shortpaths"
_SHORT_SPEC = _spec(["/things", "/namespaces/{namespace}/secrets"])


def _service(monkeypatch: pytest.MonkeyPatch) -> IngestionPipelineService:
    from unittest.mock import AsyncMock

    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.25] * 384
    embedding.encode.return_value = [[0.25] * 384]
    embedding.dimension = 384
    operator = Operator(
        sub=f"op-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = IngestionPipelineService(
        operator, sessionmaker=get_sessionmaker(), embedding_service=embedding
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=f"{_PRODUCT}-rest-1.0",
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=0,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)
    return service


async def _stored_count(model: Any) -> int:
    async with get_sessionmaker()() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.product == _PRODUCT)
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "ingest"])
async def test_the_dry_run_and_the_ingest_refuse_the_file_and_store_nothing(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    with pytest.raises(UnsupportedSpecError) as refused:
        await _service(monkeypatch).ingest(
            product=_PRODUCT,
            version="1.0",
            impl_id=f"{_PRODUCT}-rest",
            specs=[SpecSource(uri="fixture://short/spec.yaml", content=_SHORT_SPEC)],
            dry_run=dry_run,
        )
    assert "/namespaces/{namespace}/secrets" in str(refused.value)
    assert await _stored_count(EndpointDescriptor) == 0
    assert await _stored_count(SpecProvenance) == 0


# ---------------------------------------------------------------------------
# The start warning
# ---------------------------------------------------------------------------


def _row(impl_id: str, op_id: str, *, tenant_id: uuid.UUID | None = None) -> EndpointDescriptor:
    return EndpointDescriptor(
        product=_PRODUCT,
        version="1.0",
        impl_id=impl_id,
        op_id=op_id,
        method=op_id.split(":", 1)[0],
        path=op_id.split(":", 1)[1],
        source_kind="ingested",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
        tenant_id=tenant_id,
    )


@pytest.mark.asyncio
async def test_the_start_warning_names_each_connector_with_short_paths() -> None:
    rows = [
        _row("short-a", "GET:/namespaces/{namespace}/secrets"),
        _row("short-a", "POST:/namespaces/{namespace}/configmaps"),
        _row("short-a", "GET:/things"),
        _row("full-b", "GET:/api/v1/namespaces/{namespace}/secrets"),
    ]
    async with get_sessionmaker()() as session, session.begin():
        session.add_all(rows)

    with capture_logs() as logs:
        await warn_stored_short_kubernetes_paths()

    lines = [line for line in logs if line["event"] == "short_kubernetes_paths_stored"]
    mine = [line for line in lines if line["connector_id"] in {"short-a-1.0", "full-b-1.0"}]
    assert len(mine) == 1
    assert mine[0]["connector_id"] == "short-a-1.0"
    assert mine[0]["count"] == 2
    assert mine[0]["log_level"] == "warning"
    async with get_sessionmaker()() as session:
        stored = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
    assert {(row.op_id, row.requires_approval) for row in stored} == {
        (row.op_id, False) for row in rows
    }


@pytest.mark.asyncio
async def test_a_failed_read_at_start_is_a_warning_and_the_start_goes_on() -> None:
    def _broken() -> Any:
        raise RuntimeError("database is down")

    with capture_logs() as logs:
        written = await warn_stored_short_kubernetes_paths(_broken)  # type: ignore[arg-type]

    assert written == 0
    assert [line["event"] for line in logs] == ["short_kubernetes_paths_not_checked"]
