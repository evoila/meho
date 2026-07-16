# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0054_backfill_empty_runbook_step_bodies``.

Initiative #2286 (G0.30 v0.20.0 closed-loop dogfood hardening), Task
#2239. The migration is the data half of PR #2122's forward-only
``StringConstraints(strip_whitespace=True, min_length=1)`` tightening on
the runbook step ``body`` field: it rewrites every stored step body that
strips to empty (the exact condition the constraint rejects) to a
non-empty placeholder, so the read-side re-validation
(:func:`~meho_backplane.runbooks.service._steps_from_storage`) stops
raising :class:`pydantic.ValidationError` on legacy rows.

Test matrix
-----------

* **Empty body -> backfilled.** A ``manual`` step whose ``body`` is ``""``
  carries the placeholder after ``upgrade 0054``.
* **Whitespace-only body -> backfilled.** A ``body`` of ``"   \\t\\n"`` --
  the constraint strips-then-length-checks, so whitespace-only is just as
  invalid -- is backfilled too.
* **operation_call step -> backfilled.** Both step variants carry the
  tightened ``body``; the rewrite keys on the value, not ``type``.
* **Non-empty body -> preserved.** A real body is left byte-identical.
* **Idempotency.** A stamp-back replay pinned to ``0054`` (NOT ``head`` --
  the house footgun, recurred 0049/#2015 and 0050/#2021) is a no-op.
* **Hydration probe (AC #2).** A seeded legacy empty-body template + a run
  pinned to it both hydrate cleanly through ``show_template`` AND
  ``list_runs`` after the migration -- the tenant-wide ``list_runs`` break
  is repaired.

Sync-test constraint: ``alembic.command.upgrade`` drives env.py's async
cookbook through ``asyncio.run``, so the migration tests stay sync; the
hydration-probe test wraps its async service calls in ``asyncio.run``
with an engine-cache reset on each side (same shape as
:mod:`tests.migrations.test_migration_0038_backfill_product_splits`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import dispose_engine, reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

#: The placeholder the migration writes over an empty body. Kept in sync
#: with ``_PLACEHOLDER_BODY`` in the migration by asserting the exact text
#: -- a drift in either surfaces here.
_PLACEHOLDER: Final[str] = (
    "(no instructions recorded — authored before the v0.20.0 non-empty-body requirement)"
)

_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as
    :mod:`tests.migrations.test_migration_0038_backfill_product_splits`.
    """
    db_path = tmp_path / "migration_0054.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _manual_step(step_id: str, body: str) -> dict[str, Any]:
    """A minimal stored ``manual`` step dict with the given ``body``."""
    return {
        "id": step_id,
        "title": "Do the thing",
        "body": body,
        "type": "manual",
        "verify": {"type": "confirm", "prompt": "Done?"},
    }


def _operation_call_step(step_id: str, body: str) -> dict[str, Any]:
    """A minimal stored ``operation_call`` step dict with the given ``body``."""
    return {
        "id": step_id,
        "title": "Dispatch the op",
        "body": body,
        "type": "operation_call",
        "op_id": "vmware.vm.list",
        "params": {},
        "verify": {"type": "confirm", "prompt": "OK?"},
    }


def _insert_template(
    sync_url: str,
    *,
    tenant_id: UUID,
    slug: str,
    version: int,
    steps: list[dict[str, Any]],
    status: str = "published",
) -> UUID:
    """Insert one ``runbook_templates`` row at revision 0053 via raw SQL.

    Raw SQL (not the ORM) pins the seed to the schema the migration runs
    against. ``steps`` is serialised to a JSON string (the ``sa.JSON()``
    column stores TEXT on SQLite); UUID binds use ``.hex`` per
    ``docs/codebase/migrations.md``.
    """
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO runbook_templates (
                        id, tenant_id, slug, version, title, description,
                        steps, target_kind, status, created_by, created_at,
                        edited_by, edited_at
                    ) VALUES (
                        :id, :tenant_id, :slug, :version, :title, :description,
                        :steps, :target_kind, :status, :created_by, :ts,
                        :edited_by, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": tenant_id.hex,
                    "slug": slug,
                    "version": version,
                    "title": "Rotate cert",
                    "description": "Rotate the expiring TLS cert.",
                    "steps": json.dumps(steps),
                    "target_kind": "host",
                    "status": status,
                    "created_by": "op-admin",
                    "edited_by": "op-admin",
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _insert_run(
    sync_url: str,
    *,
    tenant_id: UUID,
    template_slug: str,
    template_version: int,
    assigned_to: str,
) -> UUID:
    """Insert one ``completed`` ``runbook_runs`` row pinned to a template."""
    run_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO runbook_runs (
                        run_id, tenant_id, template_slug, template_version,
                        assigned_to, target, params, state, started_by,
                        started_at, completed_at
                    ) VALUES (
                        :run_id, :tenant_id, :template_slug, :template_version,
                        :assigned_to, :target, :params, 'completed', :started_by,
                        :ts, :ts
                    )
                    """,
                ),
                {
                    "run_id": run_id.hex,
                    "tenant_id": tenant_id.hex,
                    "template_slug": template_slug,
                    "template_version": template_version,
                    "assigned_to": assigned_to,
                    "target": "host:edge-01",
                    "params": json.dumps({}),
                    "started_by": assigned_to,
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return run_id


def _read_steps(sync_url: str, row_id: UUID) -> list[dict[str, Any]]:
    """Return the stored ``steps`` list for one template row."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            raw = conn.execute(
                text("SELECT steps FROM runbook_templates WHERE id = :id"),
                {"id": row_id.hex},
            ).scalar_one()
            return list(json.loads(raw))
    finally:
        sync_eng.dispose()


def test_empty_body_is_backfilled(alembic_cfg: tuple[Config, str]) -> None:
    """A ``manual`` step with an empty ``body`` carries the placeholder after upgrade."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="cert-rotate",
        version=1,
        steps=[_manual_step("revoke", "")],
    )

    command.upgrade(cfg, "0054")

    steps = _read_steps(sync_url, row_id)
    assert steps[0]["body"] == _PLACEHOLDER
    # Untouched fields survive the rewrite verbatim.
    assert steps[0]["id"] == "revoke"
    assert steps[0]["type"] == "manual"


def test_whitespace_only_body_is_backfilled(alembic_cfg: tuple[Config, str]) -> None:
    """A whitespace-only ``body`` (strips to empty) is backfilled too (AC #1)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="cert-rotate",
        version=1,
        steps=[_manual_step("revoke", "  \t\n ")],
    )

    command.upgrade(cfg, "0054")

    assert _read_steps(sync_url, row_id)[0]["body"] == _PLACEHOLDER


def test_operation_call_step_body_is_backfilled(alembic_cfg: tuple[Config, str]) -> None:
    """The rewrite is variant-agnostic: an ``operation_call`` empty body is fixed."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="vm-audit",
        version=1,
        steps=[_operation_call_step("list-vms", "")],
    )

    command.upgrade(cfg, "0054")

    steps = _read_steps(sync_url, row_id)
    assert steps[0]["body"] == _PLACEHOLDER
    # The op_id and other operation_call fields are untouched.
    assert steps[0]["op_id"] == "vmware.vm.list"
    assert steps[0]["type"] == "operation_call"


def test_non_empty_body_is_preserved(alembic_cfg: tuple[Config, str]) -> None:
    """A step with real content is left byte-identical."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    real_body = "SSH to ${run.target} and revoke the cert."
    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="cert-rotate",
        version=1,
        steps=[_manual_step("revoke", real_body)],
    )

    command.upgrade(cfg, "0054")

    assert _read_steps(sync_url, row_id)[0]["body"] == real_body


def test_mixed_steps_only_empty_bodies_touched(alembic_cfg: tuple[Config, str]) -> None:
    """Within one template, only the empty-body steps move."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="multi",
        version=1,
        steps=[
            _manual_step("keep", "Real instructions here."),
            _manual_step("fix", ""),
        ],
    )

    command.upgrade(cfg, "0054")

    steps = _read_steps(sync_url, row_id)
    assert steps[0]["body"] == "Real instructions here."
    assert steps[1]["body"] == _PLACEHOLDER


def test_re_running_migration_is_idempotent(alembic_cfg: tuple[Config, str]) -> None:
    """Replaying ``upgrade()`` on a repaired DB changes nothing.

    Both passes pin the target to ``0054`` (NOT ``head``) so a future
    non-idempotent schema migration cannot leak into the stamp-back replay
    -- the house footgun that recurred on 0049/#2015 and 0050/#2021.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    row_id = _insert_template(
        sync_url,
        tenant_id=uuid4(),
        slug="cert-rotate",
        version=1,
        steps=[_manual_step("revoke", "")],
    )

    command.upgrade(cfg, "0054")
    first = _read_steps(sync_url, row_id)
    assert first[0]["body"] == _PLACEHOLDER  # sanity: first pass landed

    command.stamp(cfg, "0053")
    command.upgrade(cfg, "0054")

    second = _read_steps(sync_url, row_id)
    assert second == first, "a replay must not re-touch an already-repaired row"


def test_seeded_legacy_row_hydrates_through_show_and_list_runs(
    alembic_cfg: tuple[Config, str],
) -> None:
    """AC #2: a legacy empty-body row hydrates through ``show_template`` AND ``list_runs``.

    Seed a poisoned template + a run pinned to it at 0053, upgrade to head,
    then drive the async service surfaces: both must succeed (no
    ``ValidationError``) and surface the placeholder body -- the tenant-wide
    ``list_runs`` break is repaired.
    """
    from meho_backplane.runbooks.run_service import RunbookRunService
    from meho_backplane.runbooks.runs_schemas import ListRunsFilter
    from meho_backplane.runbooks.service import RunbookTemplateService

    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0053")

    tenant_id = uuid4()
    operator_sub = "op-1"
    _insert_template(
        sync_url,
        tenant_id=tenant_id,
        slug="cert-rotate",
        version=1,
        steps=[_manual_step("revoke", "")],
    )
    _insert_run(
        sync_url,
        tenant_id=tenant_id,
        template_slug="cert-rotate",
        template_version=1,
        assigned_to=operator_sub,
    )

    # Upgrade to head so the schema matches the ORM the service reads through.
    command.upgrade(cfg, "head")

    async def _probe() -> tuple[str, int]:
        reset_engine_for_testing()
        try:
            template = await RunbookTemplateService().show_template(tenant_id, "cert-rotate")
            runs = await RunbookRunService().list_runs(
                tenant_id,
                operator_sub,
                caller_is_admin=True,
                filter_=ListRunsFilter(),
            )
            return template.steps[0].body, len(runs)
        finally:
            await dispose_engine()

    body, run_count = asyncio.run(_probe())
    assert body == _PLACEHOLDER, "show_template must hydrate the repaired body"
    assert run_count == 1, "list_runs must hydrate the run's pinned template cleanly"
