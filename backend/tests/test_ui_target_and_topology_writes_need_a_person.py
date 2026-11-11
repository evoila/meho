# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console target writes and topology graph writes are for people.

A console session whose access token is a machine login (a service or a
runner) with the ``tenant_admin`` role gets 403 ``human_principal_required``
on every target write page and every topology graph write page, and
nothing is written. These pages call the REST handlers as plain
functions, so the console gate runs the person check itself.

A person with ``tenant_admin`` still opens every gated page here. The
full write paths of a person are covered by
``test_create_submit_persists_target_and_redirects_to_list``,
``test_delete_submit_soft_deletes_and_redirects_to_list``
(``test_ui_connectors_forms.py``),
``test_topology_ui_annotate_delete_curated_edge_succeeds``
(``test_ui_topology_annotate.py``) and
``test_topology_ui_bulk_apply_creates_all_rows``
(``test_ui_topology_bulk_refresh.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, Target
from tests._oidc_jwt_helpers import mint_token
from tests.test_ui_agent_grants import (
    _TENANT_A,
    _admin_token,
    _authenticated_client,
    _bff_env,  # noqa: F401  (autouse fixture)
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
    _seed_tenant,
)

_TARGET = "kept-target"
_EDGE_ID = uuid.UUID("00000000-0000-0000-0000-00000000e001")

#: Each write page: (method, path, form data).
_WRITES: dict[str, tuple[str, str, dict[str, str] | None]] = {
    "target-create-modal": ("GET", "/ui/connectors/create", None),
    "target-create": (
        "POST",
        "/ui/connectors/create",
        {"name": "new-target", "product": "vmware", "host": "new.example.test"},
    ),
    "target-edit-modal": ("GET", f"/ui/connectors/{_TARGET}/edit", None),
    "target-edit": ("PATCH", f"/ui/connectors/{_TARGET}", {"host": "moved.example.test"}),
    "target-delete-modal": ("GET", f"/ui/connectors/{_TARGET}/delete", None),
    "target-delete": ("POST", f"/ui/connectors/{_TARGET}/delete", {}),
    "target-import-page": ("GET", "/ui/connectors/import", None),
    "target-import-preview": ("POST", "/ui/connectors/import", {"pasted": "targets: []"}),
    "target-import-confirm": (
        "POST",
        "/ui/connectors/import/confirm",
        {"pasted": "targets:\n  - name: imported\n    product: vmware\n    host: i.example.test\n"},
    ),
    "edge-annotate-modal": ("GET", "/ui/topology/edges/annotate", None),
    "edge-annotate": (
        "POST",
        "/ui/topology/edges",
        {"from_name": "vm-a", "kind": "depends-on", "to_name": "vm-b"},
    ),
    "edge-unannotate": ("DELETE", f"/ui/topology/edges/{_EDGE_ID}", None),
    "edge-bulk-panel": ("GET", "/ui/topology/edges/bulk", None),
    "edge-bulk-import": (
        "POST",
        "/ui/topology/edges/bulk",
        {"rows_text": "vm-a depends-on vm-b"},
    ),
}

#: The gated pages a person opens without writing anything.
_PERSON_PAGES = (
    "target-create-modal",
    "target-edit-modal",
    "target-delete-modal",
    "target-import-page",
    "edge-annotate-modal",
    "edge-bulk-panel",
)


def _seed_target() -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                Target(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_A,
                    name=_TARGET,
                    aliases=[],
                    product="vmware",
                    host="kept.example.test",
                    created_at=datetime.now(UTC),
                )
            )

    asyncio.run(_do())


async def _state() -> tuple[list[tuple[str, str, bool]], int]:
    """The targets (name, host, deleted) and the number of graph edges."""
    async with get_sessionmaker()() as session:
        targets = (await session.execute(select(Target))).scalars().all()
        edges = (await session.execute(select(func.count()).select_from(GraphEdge))).scalar_one()
    rows = sorted((t.name, t.host, t.deleted_at is not None) for t in targets)
    return rows, int(edges)


def _machine_client(kind: str) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    sub = f"{kind}:deploy-bot"
    extra: dict[str, Any] = {"runner_id": str(uuid.uuid4())} if kind == "runner" else {}
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind=kind,
        **extra,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=sub)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)


def _send(client: Any, csrf: str, name: str) -> Any:
    method, path, data = _WRITES[name]
    headers = _csrf_headers(csrf)
    if method == "GET":
        return client.get(path, headers=headers)
    return client.request(method, path, data=data, headers=headers)


@pytest.mark.parametrize("name", sorted(_WRITES))
@pytest.mark.parametrize("kind", ["service", "runner"])
def test_a_machine_console_session_is_refused(kind: str, name: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target()
    before = asyncio.run(_state())
    client, mock, csrf = _machine_client(kind)
    try:
        response = _send(client, csrf, name)
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "human_principal_required"}
    assert asyncio.run(_state()) == before


@pytest.mark.parametrize("name", _PERSON_PAGES)
def test_a_person_with_tenant_admin_opens_the_page(name: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target()
    keypair, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=_admin_token(keypair), operator_sub="op-alice"
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = _send(client, csrf, name)
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
