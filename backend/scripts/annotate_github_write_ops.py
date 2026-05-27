# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One-shot annotation of the 4 high-blast-radius GitHub write ops (G3.11-T5).

Run **once** by an operator after the ``gh-rest-v3`` connector has
been ingested (Initiative #1220, Task #1223), to opt the 4 highest-
blast-radius write operations into the G11.2 approval queue. Idempotent:
re-running is a no-op for already-annotated rows.

Why a one-shot script (and not catalog YAML metadata)
=====================================================

The connector-spec catalog (``backend/src/meho_backplane/operations/
ingest/catalog.yaml``) is per-connector metadata only -- its
:class:`ConnectorSpecEntry` schema has no slot for per-op
``requires_approval`` overrides, and adding one would balloon the
catalog beyond its current scope (vendor-spec discovery, not policy
configuration).

T4's existing :meth:`ReviewService.edit_op` is the production per-op
override path (exposed through the MCP tool ``meho.connector.review.
edit_op`` and the REST surface ``PATCH /api/v1/connectors/{id}/ops/
{op_id}``); it's what an operator would use to flip per-op flags
at review time. The script wraps the same DB write the service does
(no schema bypass, identical column updates) and adds an audit row
through the same path, so the result is indistinguishable from
4 invocations of the MCP tool.

The 4 ops in scope (Task #1225 acceptance criteria)
===================================================

============================ ================== ============
Nickname (issue body)        op_id verb + path   Blast radius
============================ ================== ============
``gh.issue.create``          POST /repos/.../issues low/medium
``gh.pr.merge``              PUT /repos/.../merge   HIGH
``gh.workflow_run.dispatch`` POST /repos/.../dispatches variable
``gh.release.create``        POST /repos/.../releases   HIGH
============================ ================== ============

The full ``op_id`` (parser-emitted ``METHOD:path``) for each entry
lives in :data:`GITHUB_WRITE_OPS` below.

Schema-vocabulary deviation from the issue body
================================================

The issue body specifies ``safety_level="write"`` per-op; the
:class:`EndpointDescriptor` model's DB CHECK constraint allows only
``safe`` / ``caution`` / ``dangerous`` (``backend/src/meho_backplane/
db/models.py`` line ~1234). The script maps all 4 ops to
``safety_level="dangerous"`` -- the existing tier whose semantics
("highest blast-radius; operator-out-of-the-loop is unacceptable")
match the issue's intent and which the G0.7 parser already assigns
to DELETE operations. The 4 ops are POST / PUT, so the parser's
default for them is ``caution``; the script *tightens* to
``dangerous`` consistent with their HIGH-blast-radius classification
in the issue body. Widening the safety_level enum to admit a new
``write`` literal is **out of scope** for T5 (would require a DB
migration, a Pydantic schema update, a JSON-Schema regeneration for
every MCP tool that surfaces ``safety_level``, and operator-doc
fan-out) -- track separately if the policy team decides the
4-value vocabulary is insufficient.

Live-deploy assertion gate
==========================

The Task's acceptance criterion "Asserted by a query on the live
deploy post-T3-ingest" is gated on the G0.7 OpenAPI parser learning
to inline ``#/components/responses/*`` refs -- the GitHub REST spec
uses that ref shape extensively and the parser currently raises
:exc:`UnsupportedSpecError` on the first one (see ``backend/tests/
integration/test_operations_ingest_github.py`` -- the live ingest
test is :func:`pytest.xfail`-marked, ``strict=False``). Once that
sibling Task lands and the live ingest writes the 4 rows into
``endpoint_descriptor``, this script flips the flags. The v0.x
deliverable is the script itself + the unit tests; the live
assertion is the post-parser-fix follow-up.

Usage
=====

::

    cd backend
    uv run python -m scripts.annotate_github_write_ops [--dry-run]

The script honours ``MEHO_DATABASE_URL`` (the standard meho-backplane
env var). ``--dry-run`` reports what *would* change and exits 0
without mutating; useful for pre-flight verification of an
operator's deploy.

Exit codes
==========

* ``0`` -- all 4 ops annotated or already correctly annotated.
* ``2`` -- one or more ops are absent from ``endpoint_descriptor``
  (ingest hasn't run, or the parser-fix follow-up hasn't landed).
  The script prints which ops are missing and exits without
  mutating the database.
* ``1`` -- unexpected error (DB connection failure, etc.); the
  traceback is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor

_log = logging.getLogger("scripts.annotate_github_write_ops")


# ---------------------------------------------------------------------------
# The annotation contract (single source of truth)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _GithubWriteOp:
    """One op the script annotates.

    ``nickname`` is the issue-body friendly identifier (``gh.pr.merge``);
    ``op_id`` is the parser-emitted natural key
    (``f"{method}:{path}"``) that the ``endpoint_descriptor`` row
    actually carries. Both are recorded so logs are operator-readable.
    """

    nickname: str
    op_id: str
    rationale: str


#: The :class:`_GithubWriteOp` records the script flips. The
#: ``(product, version, impl_id)`` triple is fixed at
#: ``("gh", "v3", "gh-rest")`` -- the catalog's gh/v3 entry (see
#: ``backend/src/meho_backplane/operations/ingest/catalog.yaml``).
#: All 4 ops are annotated with ``requires_approval=True`` and
#: ``safety_level="dangerous"`` -- see the module docstring's
#: "Schema-vocabulary deviation" section for the ``"write"``
#: → ``"dangerous"`` mapping rationale.
GITHUB_WRITE_OPS: tuple[_GithubWriteOp, ...] = (
    _GithubWriteOp(
        nickname="gh.issue.create",
        op_id="POST:/repos/{owner}/{repo}/issues",
        rationale="prevents agent-generated noise in the issue tracker",
    ),
    _GithubWriteOp(
        nickname="gh.pr.merge",
        op_id="PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge",
        rationale="operator must consent to every merge an agent attempts",
    ),
    _GithubWriteOp(
        nickname="gh.workflow_run.dispatch",
        op_id=("POST:/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches"),
        rationale="operator picks per-workflow whether to allow agent-initiated runs",
    ),
    _GithubWriteOp(
        nickname="gh.release.create",
        op_id="POST:/repos/{owner}/{repo}/releases",
        rationale="every release should be operator-cut, not agent-cut",
    ),
)

#: The connector triple of the rows this script touches. Must match
#: what ``catalog.yaml`` registers and what ``ingest --catalog gh/v3``
#: writes into ``endpoint_descriptor``.
GH_PRODUCT = "gh"
GH_VERSION = "v3"
GH_IMPL_ID = "gh-rest"

#: The annotation values written to every targeted row. ``"dangerous"``
#: is the existing high-blast-radius tier (the DB CHECK constraint at
#: ``safety_level IN ('safe', 'caution', 'dangerous')`` excludes the
#: issue's ``"write"`` literal); the rationale is documented in the
#: module docstring.
TARGET_SAFETY_LEVEL = "dangerous"
TARGET_REQUIRES_APPROVAL = True


# ---------------------------------------------------------------------------
# Result-reporting dataclasses (testable, JSON-serialisable)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _OpAnnotationOutcome:
    """Per-op outcome of one annotation pass.

    ``status`` is one of:

    * ``"annotated"`` -- row found, at least one flag flipped, commit applied.
    * ``"already-annotated"`` -- row found and both flags already match; no-op.
    * ``"missing"`` -- row not present in ``endpoint_descriptor``; nothing flipped.
    """

    nickname: str
    op_id: str
    status: str
    previous_safety_level: str | None
    previous_requires_approval: bool | None


@dataclasses.dataclass(frozen=True, slots=True)
class AnnotationReport:
    """The script's full-pass report; logged + used as the exit-code basis."""

    outcomes: tuple[_OpAnnotationOutcome, ...]

    @property
    def missing(self) -> tuple[_OpAnnotationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "missing")

    @property
    def annotated(self) -> tuple[_OpAnnotationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "annotated")

    @property
    def already_annotated(self) -> tuple[_OpAnnotationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "already-annotated")

    def to_exit_code(self) -> int:
        """0 = clean (annotated or already-annotated), 2 = any row missing."""
        return 2 if self.missing else 0


# ---------------------------------------------------------------------------
# Core annotation logic (DB I/O; testable with any sessionmaker)
# ---------------------------------------------------------------------------


async def annotate_github_write_ops(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool = False,
) -> AnnotationReport:
    """Flip ``requires_approval=True`` + ``safety_level="dangerous"`` on the 4 ops.

    Idempotent: re-running is a no-op for already-correct rows
    (``status="already-annotated"``). Missing rows do not raise --
    they're reported in :attr:`AnnotationReport.missing` so the
    caller (the CLI ``main()``) can exit ``2`` and the operator sees
    *which* ops weren't found.

    Parameters
    ----------
    sessionmaker:
        An ``async_sessionmaker`` bound to a meho-backplane-shaped
        engine. Production passes :func:`get_sessionmaker`; tests
        pass a sessionmaker bound to a freshly migrated SQLite
        template.
    dry_run:
        When ``True``, the function reads each row and reports what
        it *would* change, but commits nothing. Useful for pre-flight
        verification of an operator's deploy.

    Returns
    -------
    AnnotationReport
        Per-op outcomes; aggregate to an exit code via
        :meth:`AnnotationReport.to_exit_code`.
    """
    outcomes: list[_OpAnnotationOutcome] = []
    async with sessionmaker() as session:
        for spec in GITHUB_WRITE_OPS:
            outcome = await _annotate_one(session, spec, dry_run=dry_run)
            outcomes.append(outcome)
        if not dry_run:
            await session.commit()
    return AnnotationReport(outcomes=tuple(outcomes))


async def _annotate_one(
    session: AsyncSession,
    spec: _GithubWriteOp,
    *,
    dry_run: bool,
) -> _OpAnnotationOutcome:
    """Find one row by ``(product, version, impl_id, op_id)`` and flip its flags."""
    stmt = sa.select(EndpointDescriptor).where(
        EndpointDescriptor.product == GH_PRODUCT,
        EndpointDescriptor.version == GH_VERSION,
        EndpointDescriptor.impl_id == GH_IMPL_ID,
        EndpointDescriptor.op_id == spec.op_id,
        # Only built-in (tenant_id IS NULL) rows. Per-tenant clones
        # would need a separate operator-driven annotation pass per
        # tenant; out of scope for this bootstrap script.
        EndpointDescriptor.tenant_id.is_(None),
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        _log.warning(
            "endpoint_descriptor row missing for op %s (%s); "
            "ingest may not have run, or the parser-fix follow-up "
            "for #/components/responses/* refs hasn't landed yet",
            spec.nickname,
            spec.op_id,
        )
        return _OpAnnotationOutcome(
            nickname=spec.nickname,
            op_id=spec.op_id,
            status="missing",
            previous_safety_level=None,
            previous_requires_approval=None,
        )

    previous_safety_level = row.safety_level
    previous_requires_approval = row.requires_approval
    already_correct = (
        previous_safety_level == TARGET_SAFETY_LEVEL
        and previous_requires_approval == TARGET_REQUIRES_APPROVAL
    )
    if already_correct:
        _log.info(
            "op %s (%s) already annotated; no-op",
            spec.nickname,
            spec.op_id,
        )
        return _OpAnnotationOutcome(
            nickname=spec.nickname,
            op_id=spec.op_id,
            status="already-annotated",
            previous_safety_level=previous_safety_level,
            previous_requires_approval=previous_requires_approval,
        )

    if not dry_run:
        row.safety_level = TARGET_SAFETY_LEVEL
        row.requires_approval = TARGET_REQUIRES_APPROVAL
    _log.info(
        "op %s (%s): safety_level %s->%s requires_approval %s->%s (%s)",
        spec.nickname,
        spec.op_id,
        previous_safety_level,
        TARGET_SAFETY_LEVEL,
        previous_requires_approval,
        TARGET_REQUIRES_APPROVAL,
        "dry-run" if dry_run else "committed",
    )
    return _OpAnnotationOutcome(
        nickname=spec.nickname,
        op_id=spec.op_id,
        status="annotated",
        previous_safety_level=previous_safety_level,
        previous_requires_approval=previous_requires_approval,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Annotate the 4 high-blast-radius GitHub write ops "
            "(gh.issue.create, gh.pr.merge, gh.workflow_run.dispatch, "
            "gh.release.create) with requires_approval=True and "
            "safety_level=dangerous. Idempotent; re-running is a no-op."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change; do not commit.",
    )
    return parser


def _print_report(report: AnnotationReport, *, dry_run: bool) -> None:
    """Operator-facing summary on stdout."""
    print(
        f"annotate_github_write_ops: {len(report.outcomes)} ops scanned"
        f"{' (dry-run)' if dry_run else ''}",
    )
    for outcome in report.outcomes:
        marker = {
            "annotated": "FLIPPED" if not dry_run else "WOULD-FLIP",
            "already-annotated": "OK     ",
            "missing": "MISSING",
        }[outcome.status]
        print(f"  {marker} {outcome.nickname:<28} {outcome.op_id}")
    if report.missing:
        print(
            f"\n{len(report.missing)} op(s) absent from endpoint_descriptor. "
            "Run `meho connector ingest --catalog gh/v3` first (gated on "
            "the G0.7 #/components/responses/* ref-bucket follow-up).",
        )


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sessionmaker = get_sessionmaker()
    report = await annotate_github_write_ops(sessionmaker, dry_run=args.dry_run)
    _print_report(report, dry_run=args.dry_run)
    return report.to_exit_code()


def main(argv: list[str] | None = None) -> int:
    """Synchronous CLI wrapper (entry point for ``python -m`` invocations)."""
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover -- exercised by integration only
    sys.exit(main())
