# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The migration runner gives the runtime role its rights.

The running backplane can log in with its own role
(``DATABASE_RUNTIME_ROLE``, :mod:`meho_backplane.db.runtime_role`). After
``alembic upgrade head`` the runner, as the owner of the schema, grants that
role what it needs. These tests pin:

* the role name comes from the environment and is checked; a bad name
  fails the run before any statement runs;
* off PostgreSQL (the SQLite lane) the role is ignored with one line;
* on PostgreSQL (testcontainers, real runner processes), the role can read
  and write the other tables, and on ``audit_log`` it can add and read
  rows and empty ``raw_payload``, but not change or delete a row, truncate
  the table, add a trigger, disable the append-only trigger or drop the
  table; a table the owner creates later gets the rights too; a second run
  changes nothing;
* the runner waits for a role that is created a moment later, and fails
  with ``RuntimeRoleMissingError`` when the role never appears.

The PostgreSQL tests run the migrations as a role that is not a superuser,
with the ``vector`` extension created by the superuser first, like a real
install. They skip where Docker is absent.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from meho_backplane.db.migrate import main
from meho_backplane.db.runtime_role import (
    RUNTIME_ROLE_ENV_VAR,
    runtime_role_from_env,
)
from tests._pgvector_image import pgvector_test_image

_BACKEND_DIR = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# The environment variable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("meho_app", "meho_app"),
        (" meho_app ", "meho_app"),
        ("_x1", "_x1"),
    ],
)
def test_role_env_blank_means_none(raw: str, expected: str | None) -> None:
    assert runtime_role_from_env({RUNTIME_ROLE_ENV_VAR: raw}) == expected


def test_role_env_unset_means_none() -> None:
    assert runtime_role_from_env({}) is None


@pytest.mark.parametrize(
    "raw", ["Meho_App", "meho app", "meho;drop", "1app", "meho-app", "a" * 64, 'x"y']
)
def test_a_bad_role_name_fails_the_run(
    raw: str, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(RUNTIME_ROLE_ENV_VAR, raw)
    assert main() == 1
    err = capfd.readouterr().err
    assert err.startswith(
        "migration_failed: ValueError: DATABASE_RUNTIME_ROLE must be a lower-case "
        "PostgreSQL role name"
    )
    # The name is refused before the runner opens a connection.
    assert "migration_db_waiting" not in err


def test_runner_ignores_the_role_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv(RUNTIME_ROLE_ENV_VAR, "meho_app")
    assert main() == 0
    err = capfd.readouterr().err
    assert "migration_runtime_role_skipped: sqlite has no roles; 'meho_app' ignored" in err
    assert "migration_failed" not in err


# ---------------------------------------------------------------------------
# PostgreSQL (real runner processes)
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


needs_docker = pytest.mark.skipif(
    not _docker_socket_present(), reason="Docker is not available here"
)


@pytest.fixture(scope="module")
def pg_server() -> Iterator[dict[str, Any]]:
    """One PostgreSQL server for the module; each test makes its own database."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pgvector_test_image()) as pg:
        yield {
            "host": pg.get_container_host_ip(),
            "port": int(pg.get_exposed_port(5432)),
            "user": pg.username,
            "password": pg.password,
        }


def _auth(server: dict[str, Any], user: str, password: str) -> str:
    return f"{user}:{password}@{server['host']}:{server['port']}"


@pytest.fixture
async def owner_db(pg_server: dict[str, Any]) -> dict[str, Any]:
    """A new database owned by a role that is not a superuser, with ``vector`` present.

    Returns the owner's runner URL, the superuser DSN of the database, and
    a function that makes a new login role on the server.
    """
    tag = uuid.uuid4().hex[:10]
    owner, db_name, password = f"owner_{tag}", f"db_{tag}", f"pw_{tag}"
    admin_auth = _auth(pg_server, pg_server["user"], pg_server["password"])
    admin = await asyncpg.connect(f"postgresql://{admin_auth}/postgres")
    try:
        await admin.execute(f"CREATE ROLE {owner} LOGIN NOSUPERUSER PASSWORD '{password}'")
        await admin.execute(f'CREATE DATABASE "{db_name}" OWNER {owner}')
    finally:
        await admin.close()
    # The documented prerequisite: a superuser creates the extension once.
    admin_db = await asyncpg.connect(f"postgresql://{admin_auth}/{db_name}")
    try:
        await admin_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await admin_db.close()
    return {
        "name": db_name,
        "owner_url": f"postgresql+asyncpg://{_auth(pg_server, owner, password)}/{db_name}",
        "admin_dsn": f"postgresql://{admin_auth}/{db_name}",
        "server": pg_server,
    }


async def _make_login(db: dict[str, Any], role: str) -> str:
    """Create *role* with CONNECT on the database, as the built-in SQL file does; return its DSN."""
    password = f"pw_{role}"
    admin = await asyncpg.connect(db["admin_dsn"])
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN NOSUPERUSER PASSWORD '{password}'")
        await admin.execute(f'GRANT CONNECT ON DATABASE "{db["name"]}" TO {role}')
    finally:
        await admin.close()
    return f"postgresql://{_auth(db['server'], role, password)}/{db['name']}"


async def _run_runner(url: str, role: str, *, wait_seconds: str = "60") -> tuple[int, str]:
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "MIGRATION_DB_WAIT_SECONDS": wait_seconds,
        RUNTIME_ROLE_ENV_VAR: role,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meho_backplane.db.migrate",
        cwd=_BACKEND_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=240)
    assert proc.returncode is not None
    return proc.returncode, err.decode()


#: One complete audit row; made-up values only.
def _audit_row() -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "occurred_at": datetime.now(UTC),
        "operator_sub": "operator-runtime-role",
        "method": "DISPATCH",
        "path": "test.op.read",
        "status_code": 200,
        "request_id": uuid.uuid4(),
        "duration_ms": Decimal("1.5"),
        "payload": '{"op_id": "test.op.read"}',
        "tenant_id": uuid.uuid4(),
        "raw_payload": '{"body": "raw-body-marker"}',
    }


async def _insert_audit_row(conn: asyncpg.Connection) -> uuid.UUID:
    row = _audit_row()
    columns = ", ".join(row)
    placeholders = ", ".join(
        f"${i}::jsonb" if name in ("payload", "raw_payload") else f"${i}"
        for i, name in enumerate(row, start=1)
    )
    await conn.execute(f"INSERT INTO audit_log ({columns}) VALUES ({placeholders})", *row.values())
    return row["id"]


async def _refused(conn: asyncpg.Connection, sql: str, *args: Any) -> str:
    """Run *sql* in its own transaction; return the permission error's text."""
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
    except asyncpg.InsufficientPrivilegeError as exc:
        return str(exc)
    pytest.fail(f"not refused: {sql}")


@needs_docker
async def test_runner_grants_the_runtime_role_its_rights(owner_db: dict[str, Any]) -> None:
    role = f"app_{uuid.uuid4().hex[:8]}"
    app_dsn = await _make_login(owner_db, role)

    code, err = await _run_runner(owner_db["owner_url"], role)
    assert code == 0, err
    assert f"migration_runtime_role_granted: {role}" in err
    assert "migration_runtime_role_waiting" not in err

    app = await asyncpg.connect(app_dsn)
    try:
        # What the running backplane does: read the schema version (the
        # readiness probe), add audit rows, read them, and empty raw_payload
        # (the weekly age-off).
        assert await app.fetchval("SELECT count(*) FROM alembic_version") == 1
        row_id = await _insert_audit_row(app)
        assert await app.fetchval("SELECT count(*) FROM audit_log WHERE id = $1", row_id) == 1
        emptied = await app.execute(
            "UPDATE audit_log SET raw_payload = NULL WHERE id = $1 AND raw_payload IS NOT NULL",
            row_id,
        )
        assert emptied == "UPDATE 1"
        # A plain table: full rights (the catalog says so for every table
        # but audit_log).
        assert await app.fetchval(
            "SELECT bool_and(has_table_privilege($1, c.oid, 'SELECT, INSERT, UPDATE, DELETE')) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'audit_log'",
            role,
        )

        # What it must not do to the audit log.
        refused = [
            await _refused(app, "UPDATE audit_log SET status_code = 500 WHERE id = $1", row_id),
            await _refused(app, "DELETE FROM audit_log WHERE id = $1", row_id),
            await _refused(app, "TRUNCATE audit_log"),
            await _refused(app, "ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"),
            await _refused(
                app,
                "CREATE TRIGGER t BEFORE INSERT ON audit_log FOR EACH ROW "
                "EXECUTE FUNCTION audit_log_reject_mutation()",
            ),
            await _refused(app, "DROP TABLE audit_log"),
        ]
        assert all("audit_log" in text for text in refused), refused
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "TRIGGER"):
            assert not await app.fetchval(
                "SELECT has_table_privilege($1, 'audit_log', $2)", role, privilege
            ), privilege
        assert await app.fetchval(
            "SELECT has_column_privilege($1, 'audit_log', 'raw_payload', 'UPDATE')", role
        )
        assert not await app.fetchval(
            "SELECT has_column_privilege($1, 'audit_log', 'status_code', 'UPDATE')", role
        )
        assert await app.fetchval("SELECT count(*) FROM audit_log WHERE id = $1", row_id) == 1
    finally:
        await app.close()

    # A table a later migration creates gets the rights without a new grant.
    admin = await asyncpg.connect(owner_db["admin_dsn"])
    try:
        owner = await admin.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1", owner_db["name"]
        )
        await admin.execute(f"SET ROLE {owner}")
        await admin.execute("CREATE TABLE later_table (id int)")
        assert await admin.fetchval(
            "SELECT has_table_privilege($1, 'later_table', 'SELECT, INSERT, UPDATE, DELETE')", role
        )
    finally:
        await admin.close()

    # A second run (the next pod start) applies the same rights again.
    code, err = await _run_runner(owner_db["owner_url"], role)
    assert code == 0, err
    assert f"migration_runtime_role_granted: {role}" in err
    assert "Running upgrade" not in err


@needs_docker
async def test_runner_waits_for_a_role_created_a_moment_later(owner_db: dict[str, Any]) -> None:
    role = f"late_{uuid.uuid4().hex[:8]}"

    async def _create_after_a_while() -> None:
        await asyncio.sleep(4)
        await _make_login(owner_db, role)

    (code, err), _ = await asyncio.gather(
        _run_runner(owner_db["owner_url"], role), _create_after_a_while()
    )
    assert code == 0, err
    assert f"migration_runtime_role_waiting: no role '{role}' yet; waited 0 s of 60 s" in err
    assert f"migration_runtime_role_granted: {role}" in err


@needs_docker
async def test_runner_fails_when_the_role_never_appears(owner_db: dict[str, Any]) -> None:
    role = f"never_{uuid.uuid4().hex[:8]}"

    code, err = await _run_runner(owner_db["owner_url"], role, wait_seconds="2")

    assert code == 1
    assert err.rstrip().splitlines()[-1] == (
        f"migration_failed: RuntimeRoleMissingError: the database has no role '{role}' "
        "after 2 s (DATABASE_RUNTIME_ROLE); create the role, or clear the variable"
    )
    # The schema upgrade itself went through; only the grant step failed.
    admin = await asyncpg.connect(owner_db["admin_dsn"])
    try:
        assert await admin.fetchval("SELECT count(*) FROM alembic_version") == 1
    finally:
        await admin.close()
