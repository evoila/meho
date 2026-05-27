# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`scripts.annotate_github_write_ops` (G3.11-T5 / #1225).

Coverage:

* Annotation contract -- 4 ops, the right op_ids (parser shape
  ``f"{method}:{path}"``), nicknames match the issue body.
* End-to-end against a SQLite fixture: 4 pre-seeded
  ``endpoint_descriptor`` rows get ``requires_approval=True`` +
  ``safety_level="dangerous"`` flipped on commit.
* Idempotency: a second run on already-annotated rows reports
  ``status="already-annotated"`` and writes nothing.
* Missing rows: when one or more target rows aren't present (the
  realistic v0.x state, before the parser ref-bucket follow-up
  lands), the script reports ``status="missing"`` for each absent
  op and exits 2.
* Dry-run: reads rows, reports what *would* flip, commits nothing.
* Triple-narrowness: only rows under
  ``(product="gh", version="v3", impl_id="gh-rest")`` are touched
  -- rows under different triples (and tenant-scoped clones of the
  same op_id) are left alone.

The connector-registry / embedding-service stubs aren't needed
here -- the script reads/writes ``endpoint_descriptor`` directly
through the same sessionmaker the production code uses; the
autouse-migrated SQLite engine from ``tests/conftest.py`` is
sufficient.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest

# ``scripts/`` ships as a sibling-of-src package; ``pyproject.toml``'s
# ``[tool.pytest.ini_options] pythonpath = ["."]`` puts ``backend/``
# on ``sys.path`` so this import resolves without per-file bootstrap.
from scripts.annotate_github_write_ops import (
    GH_IMPL_ID,
    GH_PRODUCT,
    GH_VERSION,
    GITHUB_WRITE_OPS,
    TARGET_REQUIRES_APPROVAL,
    TARGET_SAFETY_LEVEL,
    AnnotationReport,
    annotate_github_write_ops,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_seed_row(
    op_id: str,
    *,
    product: str = GH_PRODUCT,
    version: str = GH_VERSION,
    impl_id: str = GH_IMPL_ID,
    tenant_id: uuid.UUID | None = None,
    safety_level: str = "caution",
    requires_approval: bool = False,
    method: str = "POST",
) -> EndpointDescriptor:
    """Build an :class:`EndpointDescriptor` mimicking a freshly-ingested row."""
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=op_id.split(":", 1)[1],
        summary=f"Stub for {op_id}",
        description=f"Stub description for {op_id}",
        tags=["spec:gh/v3"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        safety_level=safety_level,
        requires_approval=requires_approval,
        is_enabled=False,
    )


async def _seed_all_four_ops(session: AsyncSession) -> None:
    """Seed all 4 target rows in their freshly-ingested state."""
    for spec in GITHUB_WRITE_OPS:
        method = spec.op_id.split(":", 1)[0]
        session.add(_make_seed_row(spec.op_id, method=method))
    await session.commit()


# ---------------------------------------------------------------------------
# Static contract: the 4 ops, the right shape
# ---------------------------------------------------------------------------


def test_github_write_ops_lists_exactly_four_ops() -> None:
    """Acceptance: the script targets the 4 ops named in #1225."""
    assert len(GITHUB_WRITE_OPS) == 4


def test_github_write_ops_nicknames_match_issue_body() -> None:
    """Nicknames are the issue-body friendly identifiers (operator log readability)."""
    nicknames = {op.nickname for op in GITHUB_WRITE_OPS}
    assert nicknames == {
        "gh.issue.create",
        "gh.pr.merge",
        "gh.workflow_run.dispatch",
        "gh.release.create",
    }


def test_github_write_ops_op_ids_are_parser_shape() -> None:
    """op_id is the ``METHOD:path`` shape :func:`parse_openapi` emits.

    See ``backend/src/meho_backplane/operations/ingest/openapi.py`` line
    ~428 (``op_id=f"{method}:{path}"``).
    """
    for op in GITHUB_WRITE_OPS:
        method, _, path = op.op_id.partition(":")
        assert method in {"POST", "PUT", "PATCH", "DELETE"}, (
            f"{op.nickname}: write ops should be POST/PUT/PATCH/DELETE, got {method!r}"
        )
        assert path.startswith("/repos/{owner}/{repo}/"), (
            f"{op.nickname}: GitHub repo-scoped write ops should be under "
            f"/repos/{{owner}}/{{repo}}/, got {path!r}"
        )


def test_target_annotation_values_match_schema_vocabulary() -> None:
    """``"dangerous"`` is in the DB CHECK constraint enum; ``"write"`` is not.

    Documents the deviation called out in the module docstring's
    "Schema-vocabulary deviation from the issue body" section.
    """
    assert TARGET_SAFETY_LEVEL == "dangerous"
    assert TARGET_REQUIRES_APPROVAL is True


def test_connector_triple_matches_catalog_entry() -> None:
    """``(gh, v3, gh-rest)`` matches the catalog.yaml gh/v3 entry."""
    assert (GH_PRODUCT, GH_VERSION, GH_IMPL_ID) == ("gh", "v3", "gh-rest")


# ---------------------------------------------------------------------------
# End-to-end annotation against SQLite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotates_all_four_ops_when_rows_exist(session: AsyncSession) -> None:
    """Happy path: 4 pre-seeded rows get flipped to ``requires_approval=True``."""
    await _seed_all_four_ops(session)

    report = await annotate_github_write_ops(get_sessionmaker())

    assert isinstance(report, AnnotationReport)
    assert len(report.annotated) == 4
    assert not report.missing
    assert not report.already_annotated
    assert report.to_exit_code() == 0

    # Re-read every row through a fresh session to assert the commit landed.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        for spec in GITHUB_WRITE_OPS:
            row = (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == spec.op_id,
                        EndpointDescriptor.product == GH_PRODUCT,
                        EndpointDescriptor.version == GH_VERSION,
                        EndpointDescriptor.impl_id == GH_IMPL_ID,
                    )
                )
            ).scalar_one()
            assert row.requires_approval is True, (
                f"{spec.nickname}: requires_approval should be True after annotation"
            )
            assert row.safety_level == "dangerous", (
                f"{spec.nickname}: safety_level should be 'dangerous' after annotation"
            )


@pytest.mark.asyncio
async def test_idempotent_second_run_is_a_noop(session: AsyncSession) -> None:
    """Re-running on already-annotated rows reports ``already-annotated`` for each."""
    await _seed_all_four_ops(session)
    # First run flips the flags.
    first = await annotate_github_write_ops(get_sessionmaker())
    assert len(first.annotated) == 4
    # Second run should see every row already annotated.
    second = await annotate_github_write_ops(get_sessionmaker())
    assert len(second.already_annotated) == 4
    assert not second.annotated
    assert not second.missing
    assert second.to_exit_code() == 0


@pytest.mark.asyncio
async def test_missing_rows_reported_and_exit_code_two() -> None:
    """No rows seeded → all 4 reported as missing; exit code is 2."""
    # No seeding -- the realistic v0.x state before ingest has run.
    report = await annotate_github_write_ops(get_sessionmaker())
    assert len(report.missing) == 4
    assert not report.annotated
    assert not report.already_annotated
    assert report.to_exit_code() == 2
    # Missing-row nicknames cover the full set.
    missing_nicknames = {o.nickname for o in report.missing}
    assert missing_nicknames == {
        "gh.issue.create",
        "gh.pr.merge",
        "gh.workflow_run.dispatch",
        "gh.release.create",
    }


@pytest.mark.asyncio
async def test_partial_seed_only_annotates_present_rows(session: AsyncSession) -> None:
    """Two ops present, two absent → 2 annotated, 2 missing, exit code 2."""
    # Seed only the two HIGH-blast-radius ops.
    pr_merge = next(o for o in GITHUB_WRITE_OPS if o.nickname == "gh.pr.merge")
    release = next(o for o in GITHUB_WRITE_OPS if o.nickname == "gh.release.create")
    session.add(_make_seed_row(pr_merge.op_id, method="PUT"))
    session.add(_make_seed_row(release.op_id, method="POST"))
    await session.commit()

    report = await annotate_github_write_ops(get_sessionmaker())

    assert len(report.annotated) == 2
    assert len(report.missing) == 2
    assert report.to_exit_code() == 2
    annotated_nicknames = {o.nickname for o in report.annotated}
    assert annotated_nicknames == {"gh.pr.merge", "gh.release.create"}


@pytest.mark.asyncio
async def test_dry_run_reads_but_commits_nothing(session: AsyncSession) -> None:
    """``dry_run=True`` reports what *would* flip but persists nothing."""
    await _seed_all_four_ops(session)

    report = await annotate_github_write_ops(get_sessionmaker(), dry_run=True)
    assert len(report.annotated) == 4
    assert report.to_exit_code() == 0

    # No commit happened -- rows still carry their seed flags.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        for spec in GITHUB_WRITE_OPS:
            row = (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == spec.op_id,
                        EndpointDescriptor.product == GH_PRODUCT,
                    )
                )
            ).scalar_one()
            assert row.requires_approval is False, (
                f"{spec.nickname}: dry-run must not commit requires_approval flip"
            )
            assert row.safety_level == "caution", (
                f"{spec.nickname}: dry-run must not commit safety_level flip"
            )


@pytest.mark.asyncio
async def test_does_not_touch_rows_under_other_connector_triples(
    session: AsyncSession,
) -> None:
    """A row with the same ``op_id`` but a different triple is left alone."""
    pr_merge = next(o for o in GITHUB_WRITE_OPS if o.nickname == "gh.pr.merge")
    # Same op_id, but registered under a different connector triple
    # (e.g. an enterprise GitHub fork using a different impl_id).
    other = _make_seed_row(
        pr_merge.op_id,
        product="gh-ent",
        version="v3",
        impl_id="gh-ent-rest",
        method="PUT",
    )
    session.add(other)
    # Don't seed the gh/v3/gh-rest row at all -- only the foreign one.
    await session.commit()

    report = await annotate_github_write_ops(get_sessionmaker())

    # Every gh/v3/gh-rest row is reported missing; the foreign-triple
    # row is untouched.
    assert len(report.missing) == 4

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        foreign = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "gh-ent",
                )
            )
        ).scalar_one()
        # Untouched: still in the seed state.
        assert foreign.requires_approval is False
        assert foreign.safety_level == "caution"


@pytest.mark.asyncio
async def test_does_not_touch_tenant_scoped_rows(session: AsyncSession) -> None:
    """Per-tenant clones (``tenant_id IS NOT NULL``) are out of scope."""
    pr_merge = next(o for o in GITHUB_WRITE_OPS if o.nickname == "gh.pr.merge")
    tenant_id = uuid.uuid4()
    # A tenant-scoped clone of gh.pr.merge -- e.g. an operator added a
    # custom override for one tenant.
    tenanted = _make_seed_row(pr_merge.op_id, tenant_id=tenant_id, method="PUT")
    session.add(tenanted)
    await session.commit()

    report = await annotate_github_write_ops(get_sessionmaker())

    # The built-in (tenant_id NULL) row is missing.
    assert any(o.nickname == "gh.pr.merge" and o.status == "missing" for o in report.outcomes)
    # The tenant-scoped row is left untouched.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        clone = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id == tenant_id,
                )
            )
        ).scalar_one()
        assert clone.requires_approval is False
        assert clone.safety_level == "caution"
