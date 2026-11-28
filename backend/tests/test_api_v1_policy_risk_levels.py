# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``risk_levels`` part of the policy file through ``/api/v1/policy``.

Each case asks over REST with a real token (``GET /api/v1/policy/export``,
``POST /api/v1/policy/validate`` and ``POST /api/v1/policy/import``) and
then reads the stored rows.

* With ``CUSTOM_POLICY`` on, the export of a tenant writes each risk-level
  edit of that tenant: the rule id, the operation, the level or the
  approval flag, the reason, who set it and when. It holds no install-wide
  edit, no edit of another tenant and no row that sets only a category.
  The install's export holds the install-wide edits, also a platform
  admin's "approval off".
* Export, then import of the same file, is "No change.", also for odd
  reason texts.
* A ``risk_levels`` part that comes in through a merge key (``<<``) is
  refused at the line of the merge key.
* An import whose ``risk_levels`` part is not the stored edits is refused
  with the line named; nothing waits and nothing is stored.
* The export writes nothing.
* Five more round-trip cases: a tenant with edits and no stored policy; an
  approval flag that is not ``true`` or ``false``; a file the import would
  refuse as too large (the export refuses first, with a 409); a mistake in
  the part next to a mistake in another part (both named); a reason
  compared with the stored text exactly (U+0085); the line of the right
  merge key when a file has several.
* With the switch off, the export has no ``risk_levels`` part, and a file
  with the part is refused as before.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    OperationPolicyOverride,
    PolicyRevision,
    TenantPolicy,
)
from meho_backplane.policy.bindings import shipped_binding_documents
from meho_backplane.policy.document import MAX_POLICY_BYTES
from meho_backplane.policy.limits import not_read_mistake
from meho_backplane.policy.risk_levels import RISK_LEVELS_NOT_IN_THIS_VERSION
from meho_backplane.policy.service import POLICY_TOO_LARGE_TO_EXPORT
from meho_backplane.settings import get_settings

from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    TENANT,
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEYPAIR = make_rsa_keypair("kid-risk-levels-file")
_OTHER_TENANT = uuid.UUID("7d1f3a52-0c4e-4b8a-9f61-2a3b4c5d6e7f")
_OP = {"product": "inslim", "version": "1.x", "impl_id": "inslim"}


async def _add(tenant_id: uuid.UUID | None, op_id: str, **values: Any) -> uuid.UUID:
    row = OperationPolicyOverride(
        tenant_id=tenant_id, **_OP, op_id=op_id, set_by_sub="admin-a", **values
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    return row.id


async def _store_tenant_policy() -> None:
    """A tenant policy with the shipped bindings, as after the tenant's first import."""
    document = {"classifications": [], "bindings": shipped_binding_documents(get_settings())}
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=TENANT, document=document, set_by_sub="fixture"))
        await session.commit()


async def _edits() -> dict[str, uuid.UUID]:
    """A tenant's two edits, an install-wide "approval off", another tenant's edit, a category."""
    return {
        "level": await _add(
            TENANT, "inslim.vm.restart", safety_level="dangerous", reason="Restarts drop users."
        ),
        "approval": await _add(TENANT, "inslim.vm.list", requires_approval=True),
        "install": await _add(
            None, "inslim.vm.list", requires_approval=False, reason="Read only, checked."
        ),
        "other": await _add(_OTHER_TENANT, "inslim.vm.restart", safety_level="dangerous"),
        "category": await _add(TENANT, "inslim.vm.stop", category="network"),
    }


async def _stored_rows() -> list[tuple[Any, ...]]:
    """Every row an export or an import could touch, as plain values."""
    async with get_sessionmaker()() as session:
        overrides = (await session.execute(select(OperationPolicyOverride))).scalars().all()
        policies = (await session.execute(select(TenantPolicy))).scalars().all()
        revisions = (await session.execute(select(PolicyRevision))).scalars().all()
        requests = (await session.execute(select(ApprovalRequest))).scalars().all()
    return sorted(
        [
            *(
                (
                    "override",
                    str(o.id),
                    str(o.tenant_id),
                    o.op_id,
                    o.safety_level,
                    o.requires_approval,
                    o.reason,
                    o.category,
                    o.set_by_sub,
                    o.set_at.isoformat(),
                )
                for o in overrides
            ),
            *(("policy", str(p.tenant_id), str(p.document), str(p.set_at)) for p in policies),
            *(("revision", str(r.id)) for r in revisions),
            *(("request", str(r.id), str(r.status)) for r in requests),
        ]
    )


def _tenant_admin() -> str:
    return h.token(_KEYPAIR, sub="tenant-admin-a", platform_admin=False)


def _platform_admin() -> str:
    return h.token(_KEYPAIR, sub="platform-admin-a", platform_admin=True)


def _export(client: TestClient, bearer: str, scope: str = "tenant") -> Any:
    return client.get(
        "/api/v1/policy/export",
        params={"scope": scope},
        headers={"Authorization": f"Bearer {bearer}"},
    )


def _ask_all(client: TestClient, bearer: str, content: str, scope: str) -> list[Any]:
    """Validate, preview and import *content*."""
    return [
        h.post(client, "validate", bearer, content, scope=scope),
        h.post(client, "import", bearer, content, scope=scope, preview="true"),
        h.post(client, "import", bearer, content, scope=scope),
    ]


def _line_of(text: str, needle: str) -> int:
    return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_export_writes_only_the_tenants_own_edits(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    ids = await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        resp = _export(client, _tenant_admin())
    assert resp.status_code == 200, resp.text
    part = yaml.safe_load(resp.text)["risk_levels"]
    async with get_sessionmaker()() as session:
        level_row = await session.get(OperationPolicyOverride, ids["level"])
        approval_row = await session.get(OperationPolicyOverride, ids["approval"])
    assert level_row is not None and approval_row is not None
    assert part == [
        {
            "rule": f"override:{ids['approval']}",
            "operation": {**_OP, "op_id": "inslim.vm.list"},
            "scope": "tenant",
            "requires_approval": True,
            "set_by": "admin-a",
            "set_at": approval_row.set_at.isoformat(),
        },
        {
            "rule": f"override:{ids['level']}",
            "operation": {**_OP, "op_id": "inslim.vm.restart"},
            "scope": "tenant",
            "level": "dangerous",
            "reason": "Restarts drop users.",
            "set_by": "admin-a",
            "set_at": level_row.set_at.isoformat(),
        },
    ]
    for other in ("install", "other", "category"):
        assert str(ids[other]) not in resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_install_export_holds_the_install_wide_approval_off(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    ids = await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        resp = _export(client, _platform_admin(), scope="install")
    assert resp.status_code == 200, resp.text
    part = yaml.safe_load(resp.text)["risk_levels"]
    assert [item["rule"] for item in part] == [f"override:{ids['install']}"]
    assert part[0]["scope"] == "install"
    assert part[0]["requires_approval"] is False
    assert part[0]["reason"] == "Read only, checked."


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_export_changes_no_row(client: TestClient, embedding: AsyncMock) -> None:
    await h.setup_ops(embedding)
    await _edits()
    before = await _stored_rows()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        assert _export(client, _tenant_admin()).status_code == 200
        assert _export(client, _platform_admin(), scope="install").status_code == 200
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_reader_still_may_not_export(client: TestClient, embedding: AsyncMock) -> None:
    await h.setup_ops(embedding)
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        for role in (TenantRole.READ_ONLY, TenantRole.OPERATOR):
            bearer = h.token(_KEYPAIR, platform_admin=False, tenant_role=role.value)
            assert _export(client, bearer).status_code == 403


# ---------------------------------------------------------------------------
# Export, then import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("scope", ["tenant", "install"])
async def test_export_then_import_of_the_same_file_is_no_change(
    client: TestClient, embedding: AsyncMock, scope: str
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    bearer = _tenant_admin() if scope == "tenant" else _platform_admin()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        text = _export(client, bearer, scope=scope).text
        assert "risk_levels:" in text
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, text, scope)
    assert checked.json()["mistakes"] == [], checked.text
    assert preview.json()["no_change"] is True, preview.text
    assert preview.json()["message"] == "No change."
    assert asked.status_code == 200, asked.text
    assert asked.json()["message"] == "No change."
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_file_without_the_part_leaves_the_edits_as_they_are(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        document = yaml.safe_load(text)
        del document["risk_levels"]
        before = await _stored_rows()
        preview = h.post(client, "import", bearer, yaml.safe_dump(document), preview="true")
    assert preview.json()["mistakes"] == [], preview.text
    assert preview.json()["no_change"] is True
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    "reason",
    [
        "Stops\u0085the host.",
        "Two\nlines.",
        "Line\u2028separator.",
        "Say \"no\" and it's 'done'.",
        "A long reason " * 60,
    ],
    ids=["next-line", "newline", "line-separator", "quotes", "long-line"],
)
async def test_an_export_imports_again_for_odd_reasons(
    client: TestClient, embedding: AsyncMock, reason: str
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _add(TENANT, "inslim.vm.restart", safety_level="dangerous", reason=reason)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert "risk_levels:" in text
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, text, "tenant")
    assert checked.json()["mistakes"] == [], checked.text
    assert preview.json()["message"] == "No change.", preview.text
    assert asked.status_code == 200, asked.text
    assert asked.json()["message"] == "No change."
    assert await _stored_rows() == before


# ---------------------------------------------------------------------------
# A changed part is refused
# ---------------------------------------------------------------------------


def _changed_level(text: str) -> tuple[str, str]:
    """*text* with the risk level of the second edit lowered, and the expected sentence."""
    changed = text.replace("level: dangerous", "level: caution")
    starts = [n for n, row in enumerate(changed.splitlines(), 1) if row.startswith("- rule: ")]
    line = starts[1]
    return changed, f"Line {line}: `risk_levels[1]` is not a stored risk-level edit."


def _new_item(text: str) -> tuple[str, str]:
    extra = (
        "- rule: override:00000000-0000-0000-0000-000000000001\n"
        "  operation: {product: inslim, version: 1.x, impl_id: inslim, op_id: inslim.vm.list}\n"
        "  scope: tenant\n"
        "  level: dangerous\n"
    )
    changed = text.replace("risk_levels:\n", "risk_levels:\n" + extra)
    line = _line_of(changed, "risk_levels:") + 1
    return changed, f"Line {line}: `risk_levels[0]` is not a stored risk-level edit."


def _not_a_list(text: str) -> tuple[str, str]:
    head = text.split("risk_levels:\n")[0]
    changed = head + "risk_levels: example-value-7\n"
    line = _line_of(changed, "risk_levels:")
    return changed, f"Line {line}: `risk_levels` is not the list of risk-level edits."


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("change", [_changed_level, _new_item, _not_a_list])
async def test_a_changed_part_is_refused_with_its_line_and_nothing_is_stored(
    client: TestClient, embedding: AsyncMock, change: Any
) -> None:
    await h.setup_ops(embedding)
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert text.splitlines()[-1].startswith("  set_at:"), "risk_levels is the last part"
        changed, start = change(text)
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, changed, "tenant")
    sentence = f"{start} {RISK_LEVELS_NOT_IN_THIS_VERSION}"
    assert checked.json()["mistakes"] == [sentence], checked.text
    assert preview.json()["mistakes"] == [sentence], preview.text
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == [sentence]
    assert "not in this version" in sentence
    assert "example-value-7" not in asked.text
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_part_that_misses_an_edit_names_it_and_nothing_is_stored(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    ids = await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        document = yaml.safe_load(_export(client, bearer).text)
        document["risk_levels"] = document["risk_levels"][:1]
        content = yaml.safe_dump(document, sort_keys=False)
        before = await _stored_rows()
        asked = h.post(client, "import", bearer, content)
    line = _line_of(content, "risk_levels:")
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == [
        f"Line {line}: `risk_levels` misses the stored edits `override:{ids['level']}`. "
        f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
    ]
    assert await _stored_rows() == before


def _merge_key_part(text: str, *, same: bool) -> str:
    """*text* with its ``risk_levels`` part given through a merge key (``<<``)."""
    head, part = text.split("risk_levels:\n")
    if not same:
        return head + "<<: {risk_levels: []}\n"
    indented = "".join(f"  {row}\n" for row in part.splitlines())
    return head + "<<:\n  risk_levels:\n" + indented


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("same", [False, True], ids=["changed", "unchanged"])
async def test_a_part_through_a_merge_key_is_refused_with_its_line(
    client: TestClient, embedding: AsyncMock, same: bool
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert text.splitlines()[-1].startswith("  set_at:"), "risk_levels is the last part"
        changed = _merge_key_part(text, same=same)
        assert yaml.safe_load(changed)["risk_levels"] == (
            yaml.safe_load(text)["risk_levels"] if same else []
        )
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, changed, "tenant")
    sentence = (
        f"Line {_line_of(changed, '<<:')}: `risk_levels` comes in through a merge key (`<<`). "
        f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
    )
    assert checked.json()["mistakes"] == [sentence], checked.text
    assert preview.json()["mistakes"] == [sentence], preview.text
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == [sentence]
    assert await _stored_rows() == before


# ---------------------------------------------------------------------------
# The switch off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_switch_off_the_export_has_no_part_and_the_key_is_refused(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    ids = await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert "risk_levels" not in text
        assert str(ids["level"]) not in text
        checked = h.post(client, "validate", bearer, text + "risk_levels: []\n")
    assert checked.json()["mistakes"] == [not_read_mistake("risk_levels")]


@pytest.mark.asyncio
async def test_with_the_switch_off_a_part_through_a_merge_key_is_refused(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        content = text + "<<: {risk_levels: []}\n"
        checked = h.post(client, "validate", bearer, content)
        preview = h.post(client, "import", bearer, content, preview="true")
    for answer in (checked, preview):
        assert answer.json()["mistakes"] == [not_read_mistake("risk_levels")], answer.text


# ---------------------------------------------------------------------------
# Five more cases of export, then import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_with_edits_and_no_stored_policy_imports_its_export_as_no_change(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert "risk_levels:" in text
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, text, "tenant")
    assert checked.json()["mistakes"] == [], checked.text
    assert preview.json()["message"] == "No change.", preview.text
    assert asked.status_code == 200, asked.text
    assert asked.json()["message"] == "No change."
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("value", ["1", "0", "'true'", "yes-please"])
async def test_an_approval_flag_that_is_not_true_or_false_is_refused_with_its_line(
    client: TestClient, embedding: AsyncMock, value: str
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert text.count("requires_approval: true") == 1
        changed = text.replace("requires_approval: true", f"requires_approval: {value}")
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, changed, "tenant")
    line = _line_of(changed, "- rule: ")
    sentence = (
        f"Line {line}: `risk_levels[0]` is not a stored risk-level edit. "
        f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
    )
    assert checked.json()["mistakes"] == [sentence], checked.text
    assert preview.json()["mistakes"] == [sentence], preview.text
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == [sentence]
    assert await _stored_rows() == before


async def _drop_from(start: int) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(OperationPolicyOverride).where(
                OperationPolicyOverride.op_id >= f"inslim.vm.op{start:05d}"
            )
        )
        await session.commit()


async def _add_many(start: int, count: int, reason: str) -> None:
    async with get_sessionmaker()() as session:
        session.add_all(
            OperationPolicyOverride(
                tenant_id=TENANT,
                **_OP,
                op_id=f"inslim.vm.op{n:05d}",
                set_by_sub="admin-a",
                safety_level="dangerous",
                reason=reason,
            )
            for n in range(start, start + count)
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_export_never_writes_a_file_the_import_refuses_as_too_large(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edits with long reasons are added 100 at a time until the export refuses.

    Then the last 100 go again: the export writes the file it wrote before,
    and that file imports as "No change.".
    """
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    reason = "Checked by the team and written down. " * 26
    last_file = ""
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        for start in range(0, 2000, 100):
            await _add_many(start, 100, reason)
            resp = _export(client, bearer)
            if resp.status_code != 200:
                break
            last_file = resp.text
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == POLICY_TOO_LARGE_TO_EXPORT
        await _drop_from(start)
        assert _export(client, bearer).text == last_file
        assert len(last_file.encode()) <= MAX_POLICY_BYTES
        assert last_file.count("- rule: ") >= 600
        checked, preview, asked = _ask_all(client, bearer, last_file, "tenant")
        monkeypatch.setenv("CUSTOM_POLICY", "false")
        get_settings.cache_clear()
        switched_off = _export(client, bearer)
    assert checked.json()["mistakes"] == [], checked.text
    assert preview.json()["message"] == "No change.", preview.text
    assert asked.status_code == 200, asked.text
    assert asked.json()["message"] == "No change."
    assert switched_off.status_code == 200, switched_off.text
    assert "risk_levels" not in switched_off.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_mistake_in_the_part_and_in_another_part_are_both_named(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        changed, start = _changed_level(_export(client, bearer).text)
        content = "baseline: example-value-7\n" + changed
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, content, "tenant")
    line = int(start.split(":")[0].removeprefix("Line ")) + 1
    expected = [
        not_read_mistake("baseline"),
        f"Line {line}: `risk_levels[1]` is not a stored risk-level edit. "
        f"{RISK_LEVELS_NOT_IN_THIS_VERSION}",
    ]
    assert checked.json()["mistakes"] == expected, checked.text
    assert preview.json()["mistakes"] == expected, preview.text
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == expected
    assert "example-value-7" not in asked.text
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    ("form", "passes"),
    [
        (r"\N", True),
        (r"\u0085", True),
        (r"\x85", True),
        (" ", False),
        (r"\n", False),
        ("", False),
    ],
    ids=["as-exported", "unicode-escape", "hex-escape", "space", "newline", "dropped"],
)
async def test_the_reason_is_compared_with_the_stored_text_exactly(
    client: TestClient, embedding: AsyncMock, form: str, passes: bool
) -> None:
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _add(TENANT, "inslim.vm.restart", safety_level="dangerous", reason="Stops\u0085the host.")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        text = _export(client, bearer).text
        assert 'reason: "Stops\\Nthe host."' in text
        content = text.replace("\\N", form)
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, content, "tenant")
    if passes:
        assert checked.json()["mistakes"] == [], checked.text
        assert preview.json()["message"] == "No change.", preview.text
        assert asked.json()["message"] == "No change.", asked.text
    else:
        sentence = (
            f"Line {_line_of(content, '- rule: ')}: `risk_levels[0]` is not a stored "
            f"risk-level edit. {RISK_LEVELS_NOT_IN_THIS_VERSION}"
        )
        assert checked.json()["mistakes"] == [sentence], checked.text
        assert preview.json()["mistakes"] == [sentence], preview.text
        assert asked.status_code == 422, asked.text
    assert await _stored_rows() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    ("merges", "named", "twice"),
    [
        (["<<: {description: sample}", "<<: {risk_levels: []}"], 2, None),
        (["<<: {risk_levels: []}", "<<: {description: sample}"], 1, None),
        (
            ["<<: {risk_levels: []}", "<<: {description: sample}", "<<: {risk_levels: []}"],
            3,
            "risk_levels",
        ),
        (["<<: {description: sample}", "<<: [{routing: []}, {risk_levels: []}]"], 2, None),
        (["<<: {description: sample}", "<<: {<<: {risk_levels: []}}"], 2, None),
    ],
    ids=["second", "first", "last-of-two", "in-a-list", "nested"],
)
async def test_the_refusal_names_the_merge_key_that_brings_in_the_part(
    client: TestClient, embedding: AsyncMock, merges: list[str], named: int, twice: str | None
) -> None:
    """*twice* is a key two merge keys bring in: the parser names it too."""
    await h.setup_ops(embedding)
    await _store_tenant_policy()
    await _edits()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        bearer = _tenant_admin()
        head = _export(client, bearer).text.split("risk_levels:\n")[0]
        content = head + "".join(f"{merge}\n" for merge in merges)
        before = await _stored_rows()
        checked, preview, asked = _ask_all(client, bearer, content, "tenant")
    line = len(head.splitlines()) + named
    assert content.splitlines()[line - 1] == merges[named - 1]
    expected = [
        *([f"Line {line}: the key `{twice}` appears twice in the same block."] if twice else []),
        f"Line {line}: `risk_levels` comes in through a merge key (`<<`). "
        f"{RISK_LEVELS_NOT_IN_THIS_VERSION}",
    ]
    assert checked.json()["mistakes"] == expected, checked.text
    assert preview.json()["mistakes"] == expected, preview.text
    assert asked.status_code == 422, asked.text
    assert asked.json()["mistakes"] == expected
    assert await _stored_rows() == before
