# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Export, then import of the same tenant file through ``/api/v1/policy`` is "No change.".

Each case asks over REST with a real token (``GET /api/v1/policy/export``,
then ``POST /api/v1/policy/import`` with the exported text) and then reads
the stored rows.

* A tenant with no stored bindings: with approver routing on, the export
  writes the shipped bindings only as a comment. The import answers
  "No change.", nothing waits, and the stored row stays as it was. This
  holds with ``CUSTOM_POLICY`` on, with ``POLICY_APPROVER_ROUTING`` on and
  ``CUSTOM_POLICY`` off, and with routing off.
* A tenant with stored bindings: the export writes them as stored, and
  the import answers "No change.".
* The guide's example of personal agent rules (no bindings) exports and
  imports again with "No change.".
* With routing off, the export has no bindings and no comment about them,
  as before.
* A stored row with the old kinds ``SA-Operators`` and ``Approvers`` exports
  them as stored, and the import answers "No change.": they mean
  ``Operators`` and ``Readers``.
* A file near the import's size limit: the comment gets shorter, so the
  export still imports again with "No change.".

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, PolicyRevision, TenantPolicy
from meho_backplane.policy.document import MAX_POLICY_BYTES, document_of, parse_policy_file
from meho_backplane.policy.service import NO_CHANGE, export_policy
from meho_backplane.settings import get_settings

from . import _install_limits_helpers as h
from ._agent_rules_helpers import set_limit
from ._install_limits_helpers import TENANT, client_fixture  # noqa: F401
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEYPAIR = make_rsa_keypair("kid-export-bindings")

_NOTE = "# No bindings are stored for this tenant, so these shipped bindings are in force.\n"

#: The example of the guide's part "Rules for personal agents", as it stands there.
_GUIDE_AGENTS_FILE = """\
policy: v1
agents:
  - name: admins-assistant
    caller: personal_agent
    roles: [Admin]                  # Reader, Operator, Admin
    up_to: dangerous                # safe, caution or dangerous
    connectors: [vmware-rest-9.0]   # optional
    rights_changes: allow           # optional, only with connectors
"""

_NO_BINDINGS: dict[str, Any] = {
    "description": "Network team",
    "classifications": [{"match": {"connector": "inslim-1.x"}, "category": "network"}],
}

_BOUND: dict[str, Any] = {
    **_NO_BINDINGS,
    "bindings": [
        {"match": {"group": "/MEHO-Operators/Network"}, "kind": "Operators", "categories": ["*"]}
    ],
}

#: The switch sets: CUSTOM_POLICY on; routing on and CUSTOM_POLICY off; routing off.
#: The import needs POLICY_CLASSIFICATION when CUSTOM_POLICY is off.
_CUSTOM = {"CUSTOM_POLICY": "true"}
_ROUTING = {"POLICY_APPROVER_ROUTING": "true", "POLICY_CLASSIFICATION": "true"}
_NO_ROUTING = {"POLICY_CLASSIFICATION": "true"}
_SWITCH_NAMES = ("CUSTOM_POLICY", "POLICY_APPROVER_ROUTING", "POLICY_CLASSIFICATION")


@pytest.fixture(name="switches")
def switches_fixture(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[bool]:
    """Set the switch set of the test's parameter; yields whether routing is on."""
    values: dict[str, str] = request.param
    for name in _SWITCH_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    yield "POLICY_CLASSIFICATION" not in values or "POLICY_APPROVER_ROUTING" in values
    get_settings.cache_clear()


_ALL_SETS = pytest.mark.parametrize(
    "switches",
    [_CUSTOM, _ROUTING, _NO_ROUTING],
    ids=["custom", "routing", "no-routing"],
    indirect=True,
)


async def _store(document: dict[str, Any] | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            TenantPolicy.__table__.delete().where(TenantPolicy.tenant_id == TENANT)
        )
        if document is not None:
            session.add(TenantPolicy(tenant_id=TENANT, document=document, set_by_sub="fixture"))


async def _rows() -> list[tuple[Any, ...]]:
    """Every row an import could write, as plain values."""
    async with get_sessionmaker()() as session:
        policies = (await session.execute(select(TenantPolicy))).scalars().all()
        revisions = (await session.execute(select(PolicyRevision))).scalars().all()
        requests = (await session.execute(select(ApprovalRequest))).scalars().all()
    return sorted(
        [
            *(
                ("policy", str(p.tenant_id), repr(p.document), p.set_by_sub, str(p.set_at))
                for p in policies
            ),
            *(("revision", str(r.id)) for r in revisions),
            *(("request", str(r.id)) for r in requests),
        ]
    )


def _round_trip(client: TestClient) -> tuple[str, Any]:
    """Export the tenant's file over REST and import it again; the text and the answer."""
    bearer = h.token(_KEYPAIR, sub="tenant-admin-a", platform_admin=False)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        exported = client.get(
            "/api/v1/policy/export", headers={"Authorization": f"Bearer {bearer}"}
        )
        assert exported.status_code == 200, exported.text
        answer = h.post(client, "import", bearer, exported.text)
    return exported.text, answer


def _assert_no_change(answer: Any) -> None:
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["message"] == NO_CHANGE
    assert body["no_change"] is True


@pytest.mark.asyncio
@_ALL_SETS
@pytest.mark.parametrize("stored", [None, _NO_BINDINGS], ids=["no-row", "row-without-bindings"])
async def test_a_tenant_without_stored_bindings_imports_its_export_with_no_change(
    client: TestClient, switches: bool, stored: dict[str, Any] | None
) -> None:
    await _store(stored)
    before = await _rows()

    text, answer = _round_trip(client)

    _assert_no_change(answer)
    assert await _rows() == before
    parsed = parse_policy_file(text.encode(), custom_policy=True)
    assert parsed.mistakes == [] and parsed.model is not None
    assert "bindings" not in document_of(parsed.model)
    if switches:
        assert _NOTE in text
        assert "# - match:\n#     group: /MEHO-SA-Operators\n" in text
    else:
        assert "bindings" not in text


@pytest.mark.asyncio
@_ALL_SETS
async def test_a_tenant_with_stored_bindings_imports_its_export_with_no_change(
    client: TestClient, switches: bool
) -> None:
    await _store(_BOUND)
    before = await _rows()

    text, answer = _round_trip(client)

    _assert_no_change(answer)
    assert await _rows() == before
    assert "\nbindings:\n- match:\n    group: /MEHO-Operators/Network\n" in text
    assert _NOTE not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("switches", [_CUSTOM], ids=["custom"], indirect=True)
async def test_the_guide_example_of_agent_rules_imports_its_export_with_no_change(
    client: TestClient, switches: bool
) -> None:
    # The example's rule allows `dangerous`: the install's outer limit must allow it too.
    await set_limit("dangerous")
    parsed = parse_policy_file(_GUIDE_AGENTS_FILE.encode(), custom_policy=True)
    assert parsed.mistakes == [] and parsed.model is not None
    await _store(document_of(parsed.model))
    before = await _rows()

    text, answer = _round_trip(client)

    _assert_no_change(answer)
    assert await _rows() == before
    assert "name: admins-assistant" in text
    assert _NOTE in text


@pytest.mark.asyncio
async def test_with_every_switch_off_the_export_is_as_before(
    _settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _SWITCH_NAMES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    await _store(None)
    async with get_sessionmaker()() as session:
        empty = await export_policy(session, scope="tenant", tenant_id=TENANT)
    await _store(_NO_BINDINGS)
    async with get_sessionmaker()() as session:
        stored = await export_policy(session, scope="tenant", tenant_id=TENANT)
    assert empty == "policy: v1\nclassifications: []\n"
    assert stored == (
        "policy: v1\ndescription: Network team\nclassifications:\n"
        "- match:\n    connector: inslim-1.x\n  category: network\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("switches", [_CUSTOM, _ROUTING], ids=["custom", "routing"], indirect=True)
async def test_a_stored_row_with_the_old_kinds_imports_its_export_with_no_change(
    client: TestClient, switches: bool
) -> None:
    # A row stored before the old kinds were read as the new ones keeps them as written.
    old_kinds = {
        **_NO_BINDINGS,
        "bindings": [
            {"match": {"group": "/MEHO-SA-Ops/Net"}, "kind": "SA-Operators", "categories": ["*"]},
            {"match": {"group": "/MEHO-Approvers/Net"}, "kind": "Approvers", "categories": ["*"]},
        ],
    }
    await _store(old_kinds)
    before = await _rows()

    text, answer = _round_trip(client)

    _assert_no_change(answer)
    assert await _rows() == before
    assert "  kind: SA-Operators\n" in text
    assert "  kind: Approvers\n" in text


def _big_document(count: int, description: str = "x") -> dict[str, Any]:
    """A tenant document with *count* classification entries of the same long size."""
    return {
        "description": description,
        "classifications": [
            {
                "match": {
                    "connector": f"{index:04d}-" + "c" * 190,
                    "group": "g" * 200,
                    "op": "o" * 200,
                },
                "category": "network",
            }
            for index in range(count)
        ],
    }


async def _export_size(document: dict[str, Any]) -> int:
    """The bytes of the tenant's export with routing off: the file without any note."""
    await _store(document)
    async with get_sessionmaker()() as session:
        return len((await export_policy(session, scope="tenant", tenant_id=TENANT)).encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("switches", [_CUSTOM], ids=["custom"], indirect=True)
@pytest.mark.parametrize(
    ("room", "short"), [(400, True), (20, False)], ids=["short-note", "no-room-for-a-note"]
)
async def test_a_file_near_the_size_limit_imports_its_export_with_no_change(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    switches: bool,
    room: int,
    short: bool,
) -> None:
    # Size the file with routing off, so the export holds no note.
    monkeypatch.delenv("CUSTOM_POLICY")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    one = await _export_size(_big_document(1))
    per_entry = await _export_size(_big_document(2)) - one
    count = 1 + (MAX_POLICY_BYTES - room - 1000 - one) // per_entry
    description = "x" * (1 + MAX_POLICY_BYTES - room - await _export_size(_big_document(count)))
    assert await _export_size(_big_document(count, description)) == MAX_POLICY_BYTES - room
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings.cache_clear()
    before = await _rows()

    text, answer = _round_trip(client)

    _assert_no_change(answer)
    assert await _rows() == before
    assert len(text.encode()) <= MAX_POLICY_BYTES
    assert _NOTE not in text
    assert ("Shipped bindings in force: /MEHO-Readers (Readers)" in text) is short
