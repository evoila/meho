# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0132_hide_secret_fields_of_decided_approval_params``.

The migration rewrites ``approval_request.params`` of every row that is no
longer ``pending``: each secret-named value (``rootPassword``,
``licenseKey``, ``credentials``, ...) becomes the marker, the other fields
keep their value. A pending row, a row without such a field and a row
without params are left as they are.

Every step targets this migration's own revision (``0132``) and its
``down_revision`` (``0131``), never ``head``, so a later migration cannot
break these tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0132"
_DOWN_REVISION = "0131"
_TENANT = uuid4().hex
_MARKER = "[REDACTED:param_name]"
_PW_SENTINEL = "made-up-pw-value"
_LICENCE = "LIC-SENTINEL-0132"
_SPEC_PARAMS: dict[str, Any] = {
    "spec": {
        "sddcId": "lab-1",
        "vcenterSpec": {"rootPassword": _PW_SENTINEL, "licenseKey": _LICENCE},
        "hostSpecs": [{"hostname": "esx-1", "credentials": {"password": _PW_SENTINEL}}],
    }
}
_PLAIN_PARAMS: dict[str, Any] = {"vm": "vm-42", "state": "off"}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0132.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _insert(conn: sa.Connection, *, status: str, params: dict[str, Any] | None) -> str:
    row_id = uuid4().hex
    conn.execute(
        sa.text(
            "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, "
            "connector_id, params_hash, proposed_effect, status, created_at, params) "
            "VALUES (:id, :tenant, 'agent:requester', 'installer.sddc.bringup.start', "
            "'vcf-installer-9.0', 'h', '{}', :status, '2026-10-16 00:00:00', :params)"
        ),
        {
            "id": row_id,
            "tenant": _TENANT,
            "status": status,
            "params": None if params is None else json.dumps(params),
        },
    )
    return row_id


def _params_by_id(sync_url: str) -> dict[str, dict[str, Any] | None]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(sa.text("SELECT id, params FROM approval_request")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]): None if row[1] is None else json.loads(row[1]) for row in rows}


def _hidden_spec() -> dict[str, Any]:
    return {
        "spec": {
            "sddcId": "lab-1",
            "vcenterSpec": {"rootPassword": _MARKER, "licenseKey": _MARKER},
            "hostSpecs": [{"hostname": "esx-1", "credentials": _MARKER}],
        }
    }


def test_decided_rows_keep_no_secret_values_and_the_rest_stay(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            approved = _insert(conn, status="approved", params=_SPEC_PARAMS)
            rejected = _insert(conn, status="rejected", params=_SPEC_PARAMS)
            expired = _insert(conn, status="expired", params=_SPEC_PARAMS)
            pending = _insert(conn, status="pending", params=_SPEC_PARAMS)
            plain = _insert(conn, status="approved", params=_PLAIN_PARAMS)
            no_params = _insert(conn, status="approved", params=None)
    finally:
        sync_eng.dispose()

    command.upgrade(cfg, _REVISION)

    params = _params_by_id(sync_url)
    for row_id in (approved, rejected, expired):
        assert params[row_id] == _hidden_spec()
        assert _PW_SENTINEL not in json.dumps(params[row_id])
    assert params[pending] == _SPEC_PARAMS, "a waiting request still needs its values"
    assert params[plain] == _PLAIN_PARAMS
    assert params[no_params] is None


def test_second_run_changes_nothing(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            row_id = _insert(conn, status="rejected", params=_SPEC_PARAMS)
    finally:
        sync_eng.dispose()

    command.upgrade(cfg, _REVISION)
    once = _params_by_id(sync_url)[row_id]
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert once == _hidden_spec()
    assert _params_by_id(sync_url)[row_id] == once
