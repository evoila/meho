# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G4.3-T6 retire-checklist surface.

Coverage matrix (issue #445 acceptance criteria):

* **Per-criterion verdict math** — every band (green / yellow / red)
  for each of the five criteria, asserted against synthetic inputs.
* **Surface verdict composition** — all-green → READY TO RETIRE,
  any-yellow-no-red → REVIEW MANUALLY, any-red → NOT YET.
* **Service-level orchestration** (``compute_retire_checklist``) —
  reads first-use + operator weeks from audit_log, folds in a stubbed
  eval result, returns the right per-surface verdicts.
* **HTTP route RBAC** — ``read_only`` → 403, ``operator`` +
  ``tenant_filter`` → 403, ``tenant_admin`` + ``tenant_filter`` → 200,
  unknown surface → 422.
* **Audit + broadcast contract** — the audit_log row's payload pins
  ``op_id="meho.retrieval.retire_checklist"`` +
  ``op_class="audit_query"`` + ``surfaces`` + ``tenant_scope`` +
  ``row_count = len(surfaces)``.
* **Integration** — seed eval-run + audit_log rows + blocker counts,
  hit the route, assert each verdict combo (READY / REVIEW / NOT YET).

The service-level tests seed audit_log directly via the shared
:class:`AsyncSession` (matching ``test_retrieval_usage.py``'s
``_seed_audit_row`` shape) and patch ``eval_all`` to avoid spinning
up fastembed + a real retriever. The route tests use the same
TestClient + production middleware stack the chassis
``test_api_v1_retrieve`` pattern established.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.retrieve_retire import router as retire_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.retrieval.eval import EvalResult, SurfaceResult
from meho_backplane.retrieval.retire import (
    EVAL_PRECISION_GREEN,
    MIN_DAYS_SINCE_FIRST_USE,
    MIN_OPERATOR_STREAK_WEEKS,
    BaselineMetricsOverride,
    CriterionResult,
    RetireChecklistReport,
    SurfaceChecklist,
    _band_to_surface_verdict,
    _evaluate_daily_use_duration,
    _evaluate_eval_precision,
    _evaluate_meho_vs_baseline,
    _evaluate_open_blockers,
    _evaluate_operator_breadth,
    _longest_consecutive_streak,
    _worst_band,
    compute_retire_checklist,
)
from meho_backplane.retrieval.usage import MCP_TOOL_PATH_PREFIX
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_retrieval_usage.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test."""
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
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
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


def _build_app_with_retire_route() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(retire_router)
    return app


@pytest.fixture
def retire_client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    yield TestClient(_build_app_with_retire_route())


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


_FIXED_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000b0b0")
_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


async def _seed_audit_row(
    *,
    operator_sub: str,
    tenant_id: UUID | None,
    path: str,
    occurred_at: datetime,
    status_code: int = 200,
) -> uuid.UUID:
    """Insert one ``audit_log`` row directly and return its UUID."""
    row_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=row_id,
                occurred_at=occurred_at,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                method="MCP",
                path=path,
                status_code=status_code,
                request_id=None,
                duration_ms=Decimal("1.00"),
                payload={},
            ),
        )
        await session.commit()
    return row_id


async def _read_audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


def _stub_eval_result(
    *,
    kb_precision: float = 0.85,
    kb_query_count: int = 10,
    kb_baseline: tuple[float, float, float] | None = (0.70, 0.50, 0.85),
    memory_query_count: int = 0,
    operations_query_count: int = 0,
) -> EvalResult:
    """Build a deterministic :class:`EvalResult` for retire-checklist tests."""
    kb_surface = SurfaceResult(
        surface="kb",
        query_count=kb_query_count,
        precision_at_5=kb_precision,
        mrr=0.75,
        coverage=0.95,
        verdict="green" if kb_precision >= EVAL_PRECISION_GREEN else "yellow",
        baseline_kind="grep" if kb_baseline is not None else None,
        baseline_precision_at_5=kb_baseline[0] if kb_baseline else None,
        baseline_mrr=kb_baseline[1] if kb_baseline else None,
        baseline_coverage=kb_baseline[2] if kb_baseline else None,
    )
    memory_surface = SurfaceResult(
        surface="memory",
        query_count=memory_query_count,
        precision_at_5=0.0,
        mrr=0.0,
        coverage=0.0,
        verdict="green",
    )
    operations_surface = SurfaceResult(
        surface="operations",
        query_count=operations_query_count,
        precision_at_5=0.0,
        mrr=0.0,
        coverage=0.0,
        verdict="green",
    )
    return EvalResult(
        ran_at=_NOW,
        surfaces=[kb_surface, memory_surface, operations_surface],
        overall_verdict="green",
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_worst_band_returns_red_when_any_red() -> None:
    assert _worst_band(["green", "yellow", "red"]) == "red"


def test_worst_band_returns_yellow_when_only_green_and_yellow() -> None:
    assert _worst_band(["green", "yellow", "green"]) == "yellow"


def test_worst_band_returns_green_for_all_green() -> None:
    assert _worst_band(["green", "green", "green"]) == "green"


def test_worst_band_returns_green_for_empty() -> None:
    assert _worst_band([]) == "green"


def test_band_to_surface_verdict_mapping() -> None:
    assert _band_to_surface_verdict("green") == "READY TO RETIRE"
    assert _band_to_surface_verdict("yellow") == "REVIEW MANUALLY"
    assert _band_to_surface_verdict("red") == "NOT YET"


# ---------------------------------------------------------------------------
# Streak math (criterion 2)
# ---------------------------------------------------------------------------


def test_longest_streak_empty() -> None:
    assert _longest_consecutive_streak([]) == 0


def test_longest_streak_one_week() -> None:
    assert _longest_consecutive_streak([(2026, 18)]) == 1


def test_longest_streak_four_consecutive() -> None:
    """Four ISO weeks in a row → streak == 4."""
    assert (
        _longest_consecutive_streak(
            [(2026, 15), (2026, 16), (2026, 17), (2026, 18)],
        )
        == 4
    )


def test_longest_streak_with_gap_resets() -> None:
    """A gap breaks the streak; the longest run still wins."""
    assert (
        _longest_consecutive_streak(
            [(2026, 15), (2026, 16), (2026, 18), (2026, 19), (2026, 20)],
        )
        == 3
    )


def test_longest_streak_across_iso_year_boundary() -> None:
    """ISO year 2026 W53 → 2027 W01 is a consecutive pair (calendar-correct)."""
    # 2026 has 53 ISO weeks (leap-style); 2026-W53 Monday is 2026-12-28
    # and the next ISO week is 2027-W01 (Monday 2027-01-04).
    weeks = [
        (2026, 51),
        (2026, 52),
        (2026, 53),
        (2027, 1),
    ]
    assert _longest_consecutive_streak(weeks) == 4


# ---------------------------------------------------------------------------
# Criterion 1 — daily-use duration
# ---------------------------------------------------------------------------


def test_criterion1_no_usage_red() -> None:
    result = _evaluate_daily_use_duration(first_use=None, now=_NOW)
    assert result.verdict == "red"
    assert result.observed_value == "no usage in window"


def test_criterion1_above_threshold_green() -> None:
    first = _NOW - timedelta(days=MIN_DAYS_SINCE_FIRST_USE + 5)
    result = _evaluate_daily_use_duration(first_use=first, now=_NOW)
    assert result.verdict == "green"
    assert "days since first use" in result.observed_value


def test_criterion1_yellow_band() -> None:
    """``[21, 30) days`` → yellow."""
    first = _NOW - timedelta(days=25)
    result = _evaluate_daily_use_duration(first_use=first, now=_NOW)
    assert result.verdict == "yellow"


def test_criterion1_red_band() -> None:
    """Below the yellow floor → red."""
    first = _NOW - timedelta(days=10)
    result = _evaluate_daily_use_duration(first_use=first, now=_NOW)
    assert result.verdict == "red"


# ---------------------------------------------------------------------------
# Criterion 2 — operator breadth
# ---------------------------------------------------------------------------


def _streak_weeks(start: tuple[int, int], length: int) -> set[tuple[int, int]]:
    """Helper: build a set of *length* consecutive ISO weeks from *start*."""
    weeks: set[tuple[int, int]] = set()
    year, week = start
    monday = datetime.fromisocalendar(year, week, 1)
    for offset in range(length):
        iso = (monday + timedelta(days=7 * offset)).isocalendar()
        weeks.add((iso.year, iso.week))
    return weeks


def test_criterion2_three_qualified_operators_green() -> None:
    """Three operators with 4-week streaks → green."""
    weeks = {
        "op-A": _streak_weeks((2026, 15), MIN_OPERATOR_STREAK_WEEKS),
        "op-B": _streak_weeks((2026, 15), MIN_OPERATOR_STREAK_WEEKS),
        "op-C": _streak_weeks((2026, 15), MIN_OPERATOR_STREAK_WEEKS),
    }
    result = _evaluate_operator_breadth(operator_weeks=weeks)
    assert result.verdict == "green"
    assert "3 qualified" in result.observed_value


def test_criterion2_two_qualified_operators_yellow() -> None:
    """Two operators with 4-week streaks → yellow."""
    weeks = {
        "op-A": _streak_weeks((2026, 15), MIN_OPERATOR_STREAK_WEEKS),
        "op-B": _streak_weeks((2026, 15), MIN_OPERATOR_STREAK_WEEKS),
        "op-noise": _streak_weeks((2026, 15), 2),  # below threshold
    }
    result = _evaluate_operator_breadth(operator_weeks=weeks)
    assert result.verdict == "yellow"


def test_criterion2_short_streaks_red() -> None:
    """Three operators but each only 3 weeks → no one qualifies → red."""
    weeks = {
        "op-A": _streak_weeks((2026, 15), 3),
        "op-B": _streak_weeks((2026, 15), 3),
        "op-C": _streak_weeks((2026, 15), 3),
    }
    result = _evaluate_operator_breadth(operator_weeks=weeks)
    assert result.verdict == "red"


def test_criterion2_empty_red() -> None:
    """No operators → red (zero qualified)."""
    result = _evaluate_operator_breadth(operator_weeks={})
    assert result.verdict == "red"


# ---------------------------------------------------------------------------
# Criterion 3 — eval precision@5
# ---------------------------------------------------------------------------


def test_criterion3_above_threshold_green() -> None:
    result = _evaluate_eval_precision(precision_at_5=0.85, query_count=10)
    assert result.verdict == "green"


def test_criterion3_yellow_band() -> None:
    result = _evaluate_eval_precision(precision_at_5=0.65, query_count=10)
    assert result.verdict == "yellow"


def test_criterion3_red_band() -> None:
    result = _evaluate_eval_precision(precision_at_5=0.30, query_count=10)
    assert result.verdict == "red"


def test_criterion3_no_corpus_red() -> None:
    result = _evaluate_eval_precision(precision_at_5=None, query_count=0)
    assert result.verdict == "red"
    assert "no corpus shipped" in result.observed_value


def test_criterion3_zero_query_count_red() -> None:
    """A surface with zero queries can't satisfy the criterion regardless of precision."""
    result = _evaluate_eval_precision(precision_at_5=0.90, query_count=0)
    assert result.verdict == "red"


# ---------------------------------------------------------------------------
# Criterion 4 — MEHO vs baseline
# ---------------------------------------------------------------------------


def test_criterion4_no_baseline_yellow() -> None:
    """Baseline didn't run → yellow + explanatory note."""
    result = _evaluate_meho_vs_baseline(
        baseline_kind=None,
        meho_metrics=(0.9, 0.8, 0.9),
        baseline_metrics=None,
    )
    assert result.verdict == "yellow"
    assert "baseline" in (result.notes or "")


def test_criterion4_meho_above_baseline_green() -> None:
    result = _evaluate_meho_vs_baseline(
        baseline_kind="grep",
        meho_metrics=(0.9, 0.8, 0.95),
        baseline_metrics=(0.7, 0.5, 0.85),
    )
    assert result.verdict == "green"


def test_criterion4_meho_below_baseline_on_one_metric_red() -> None:
    """Any per-metric loss → red (matches eval runner's contract)."""
    result = _evaluate_meho_vs_baseline(
        baseline_kind="grep",
        meho_metrics=(0.9, 0.4, 0.95),  # mrr is below baseline
        baseline_metrics=(0.7, 0.5, 0.85),
    )
    assert result.verdict == "red"
    assert "mrr" in result.observed_value


def test_criterion4_meho_equal_baseline_green() -> None:
    """Equality (within the 1e-9 epsilon) → green."""
    result = _evaluate_meho_vs_baseline(
        baseline_kind="grep",
        meho_metrics=(0.7, 0.5, 0.85),
        baseline_metrics=(0.7, 0.5, 0.85),
    )
    assert result.verdict == "green"


# ---------------------------------------------------------------------------
# Criterion 5 — open blockers
# ---------------------------------------------------------------------------


def test_criterion5_zero_blockers_green() -> None:
    result = _evaluate_open_blockers(blocker_count=0)
    assert result.verdict == "green"


def test_criterion5_one_blocker_red() -> None:
    result = _evaluate_open_blockers(blocker_count=1)
    assert result.verdict == "red"


def test_criterion5_none_yellow() -> None:
    """Unknown count → yellow ('review manually')."""
    result = _evaluate_open_blockers(blocker_count=None)
    assert result.verdict == "yellow"
    assert result.notes is not None


# ---------------------------------------------------------------------------
# Service-level orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_retire_empty_audit_log_returns_not_yet() -> None:
    """No audit-log rows → criteria 1 + 2 red → surface verdict NOT YET."""
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0},
                now=_NOW,
            )
    assert report.overall_verdict == "NOT YET"
    assert report.surfaces[0].verdict == "NOT YET"
    daily_use = next(c for c in report.surfaces[0].criteria if c.name == "daily_use_duration")
    assert daily_use.verdict == "red"


@pytest.mark.asyncio
async def test_compute_retire_ready_to_retire_happy_path() -> None:
    """Seed criteria 1+2 green via audit_log, supply green eval + 0 blockers."""
    # Seed 3 operators, each with ≥4 weeks of search activity at >= 30d old.
    base = _NOW - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_FIXED_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0},
                now=_NOW,
            )
    assert report.overall_verdict == "READY TO RETIRE"
    surface = report.surfaces[0]
    assert surface.verdict == "READY TO RETIRE"
    for criterion in surface.criteria:
        assert criterion.verdict == "green", (
            f"criterion {criterion.name} is {criterion.verdict}: {criterion.observed_value}"
        )


@pytest.mark.asyncio
async def test_compute_retire_review_manually_when_blocker_count_unknown() -> None:
    """Every other criterion green + blocker_count=None → REVIEW MANUALLY."""
    base = _NOW - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_FIXED_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts=None,  # CLI didn't run the gh lookup
                now=_NOW,
            )
    assert report.overall_verdict == "REVIEW MANUALLY"
    blockers = next(c for c in report.surfaces[0].criteria if c.name == "open_blockers")
    assert blockers.verdict == "yellow"


@pytest.mark.asyncio
async def test_compute_retire_red_when_any_blocker_open() -> None:
    """An open blocker → red criterion 5 → NOT YET."""
    base = _NOW - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_FIXED_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 2},
                now=_NOW,
            )
    assert report.overall_verdict == "NOT YET"


@pytest.mark.asyncio
async def test_compute_retire_per_surface_results() -> None:
    """``surfaces=all`` returns one entry per surface in canonical order."""
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb", "memory", "operations"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0, "memory": 0, "operations": 0},
                now=_NOW,
            )
    assert [s.surface for s in report.surfaces] == ["kb", "memory", "operations"]
    # Each surface has exactly 5 criteria, deterministic order.
    for surface in report.surfaces:
        assert len(surface.criteria) == 5
        names = [c.name for c in surface.criteria]
        assert names == [
            "daily_use_duration",
            "operator_breadth",
            "eval_precision",
            "meho_vs_baseline",
            "open_blockers",
        ]


@pytest.mark.asyncio
async def test_compute_retire_narrows_eval_to_requested_surfaces() -> None:
    """Single-surface request must not run eval over every supported surface.

    Locks the optimisation that ``eval_all`` receives only the
    ``surfaces`` the caller requested — kb-only requests skip the
    eval cost on memory + operations.
    """
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0},
                now=_NOW,
            )
    fake_eval.assert_awaited_once()
    assert fake_eval.await_args.kwargs["surfaces"] == ["kb"]


@pytest.mark.asyncio
async def test_compute_retire_baseline_override_promotes_criterion4_to_green() -> None:
    """Caller-supplied baseline triple turns criterion 4 green via override.

    Without the override, criterion 4 stays yellow for v0.2 production
    callers because the eval runner is invoked without a baseline corpus
    root. With the override, the service uses the supplied
    (precision, mrr, coverage) triple as the baseline numbers for the
    MEHO-vs-baseline check.
    """
    base = _NOW - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_FIXED_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    # Stub eval runner returns kb with MEHO numbers + NO baseline (the
    # v0.2 API path). Without the override, criterion 4 would be
    # yellow; with the override, it becomes green.
    fake_eval = AsyncMock(
        return_value=_stub_eval_result(kb_precision=0.90, kb_baseline=None),
    )
    override = BaselineMetricsOverride(
        precision_at_5=0.60,
        mrr=0.40,
        coverage=0.80,
        kind="grep",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0},
                baseline_overrides={"kb": override},
                now=_NOW,
            )
    c4 = next(c for c in report.surfaces[0].criteria if c.name == "meho_vs_baseline")
    assert c4.verdict == "green"
    assert report.overall_verdict == "READY TO RETIRE"


@pytest.mark.asyncio
async def test_compute_retire_baseline_override_red_when_meho_loses() -> None:
    """An override where MEHO < baseline on any metric → red criterion 4."""
    base = _NOW - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_FIXED_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    # MEHO precision=0.85 (passes criterion 3 green) but the override
    # claims baseline=0.95, so MEHO < baseline → criterion 4 red →
    # overall NOT YET solely because of the baseline comparison.
    fake_eval = AsyncMock(
        return_value=_stub_eval_result(kb_precision=0.85, kb_baseline=None),
    )
    override = BaselineMetricsOverride(
        precision_at_5=0.95,  # MEHO precision (0.85) < baseline → red
        mrr=0.40,
        coverage=0.80,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,
                blocker_counts={"kb": 0},
                baseline_overrides={"kb": override},
                now=_NOW,
            )
    c4 = next(c for c in report.surfaces[0].criteria if c.name == "meho_vs_baseline")
    assert c4.verdict == "red"
    assert "precision@5" in c4.observed_value
    assert report.overall_verdict == "NOT YET"


@pytest.mark.asyncio
async def test_compute_retire_tenant_scoping() -> None:
    """Other-tenant search rows must not leak into the requested tenant's report."""
    base = _NOW - timedelta(days=40)
    # Seed activity for _OTHER_TENANT only.
    for op in ("op-X", "op-Y", "op-Z"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=_OTHER_TENANT,
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval):
            report = await compute_retire_checklist(
                session=session,
                surfaces=["kb"],
                tenant_id=_FIXED_TENANT,  # different tenant → no audit signal
                blocker_counts={"kb": 0},
                now=_NOW,
            )
    daily_use = next(c for c in report.surfaces[0].criteria if c.name == "daily_use_duration")
    assert daily_use.verdict == "red"
    assert daily_use.observed_value == "no usage in window"


# ---------------------------------------------------------------------------
# Schema stability — surface contract for the CLI consumer
# ---------------------------------------------------------------------------


def test_report_json_shape_is_stable() -> None:
    """The JSON envelope pins the keys CLI consumers parse."""
    report = RetireChecklistReport(
        ran_at=_NOW,
        tenant_id=_FIXED_TENANT,
        since=_NOW - timedelta(days=90),
        until=_NOW,
        surfaces=[
            SurfaceChecklist(
                surface="kb",
                verdict="READY TO RETIRE",
                criteria=[
                    CriterionResult(
                        name="daily_use_duration",
                        verdict="green",
                        observed_value="40 days since first use",
                        threshold_summary=">= 30 days",
                    )
                ],
            ),
        ],
        overall_verdict="READY TO RETIRE",
    )
    blob = json.loads(report.model_dump_json())
    assert set(blob.keys()) == {
        "ran_at",
        "tenant_id",
        "since",
        "until",
        "surfaces",
        "overall_verdict",
    }
    assert set(blob["surfaces"][0].keys()) == {"surface", "verdict", "criteria"}
    assert set(blob["surfaces"][0]["criteria"][0].keys()) == {
        "name",
        "verdict",
        "observed_value",
        "threshold_summary",
        "notes",
    }


# ---------------------------------------------------------------------------
# HTTP route — RBAC
# ---------------------------------------------------------------------------


def test_route_unauthenticated_returns_401(retire_client: TestClient) -> None:
    response = retire_client.post("/api/v1/retrieve/retire-checklist", json={"surface": "kb"})
    assert response.status_code == 401


def test_route_read_only_returns_403(retire_client: TestClient) -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-ro", tenant_role=TenantRole.READ_ONLY.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403


def test_route_operator_with_tenant_filter_returns_403(
    retire_client: TestClient,
) -> None:
    """``operator`` role + non-null ``tenant_filter`` query param → 403.

    ``tenant_filter`` lives on the query string (not in the JSON body)
    to mirror the sibling ``GET /api/v1/retrieve/usage`` shape.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-cross", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            f"/api/v1/retrieve/retire-checklist?tenant_filter={_OTHER_TENANT}",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "tenant_filter_requires_tenant_admin"}


def test_route_tenant_admin_with_tenant_filter_returns_200(
    retire_client: TestClient,
) -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            f"/api/v1/retrieve/retire-checklist?tenant_filter={_OTHER_TENANT}",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == str(_OTHER_TENANT)


@pytest.mark.asyncio
async def test_route_audit_row_written_even_on_tenant_filter_403(
    retire_client: TestClient,
) -> None:
    """The 403-denied path still writes the canonical audit row.

    Mirrors the audit-row-per-call contract from the issue body:
    even when the route raises 403 because an ``operator`` passed a
    ``tenant_filter`` they aren't entitled to, the audit metadata
    (``op_id``, ``op_class``, ``tenant_scope="other"``) must land on
    the row so the audit-trail surface still captures the denied
    cross-tenant attempt.
    """
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-denied", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            f"/api/v1/retrieve/retire-checklist?tenant_filter={_OTHER_TENANT}",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/retire-checklist")
    denied_rows = [r for r in rows if r.operator_sub == "op-denied"]
    assert denied_rows
    payload: dict[str, Any] = denied_rows[-1].payload
    assert payload["op_id"] == "meho.retrieval.retire_checklist"
    assert payload["op_class"] == "audit_query"
    assert payload["tenant_scope"] == "other"


def test_route_operator_default_returns_200(retire_client: TestClient) -> None:
    """Empty audit_log + no blocker counts → 200 with NOT YET overall."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_verdict"] == "NOT YET"
    assert [s["surface"] for s in body["surfaces"]] == ["kb", "memory", "operations"]


# ---------------------------------------------------------------------------
# HTTP route — Validation
# ---------------------------------------------------------------------------


def test_route_unknown_surface_returns_422(retire_client: TestClient) -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "bogus"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


def test_route_unknown_field_returns_422(retire_client: TestClient) -> None:
    """``extra=forbid`` catches typo'd field at the framework boundary."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb", "blocker_count": 0},  # typo: blocker_count not _counts
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# HTTP route — Audit + broadcast contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_audit_payload_carries_canonical_op_id(
    retire_client: TestClient,
) -> None:
    """Audit row pins op_id / op_class / surfaces / row_count / tenant_scope."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-audit", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/retire-checklist")
    audit_rows = [r for r in rows if r.operator_sub == "op-audit"]
    assert audit_rows
    payload: dict[str, Any] = audit_rows[-1].payload
    assert payload["op_id"] == "meho.retrieval.retire_checklist"
    assert payload["op_class"] == "audit_query"
    assert payload["surfaces"] == "kb"
    assert payload["tenant_scope"] == "self"
    assert payload["row_count"] == 1  # one surface in the report


@pytest.mark.asyncio
async def test_route_audit_tenant_scope_other_for_cross_tenant_admin(
    retire_client: TestClient,
) -> None:
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result())
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            f"/api/v1/retrieve/retire-checklist?tenant_filter={_OTHER_TENANT}",
            json={"surface": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200

    rows = await _read_audit_rows_for_path("/api/v1/retrieve/retire-checklist")
    admin_rows = [r for r in rows if r.operator_sub == "op-admin"]
    assert admin_rows
    assert admin_rows[-1].payload["tenant_scope"] == "other"


# ---------------------------------------------------------------------------
# Integration — full READY / REVIEW / NOT YET verdict combos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_ready_to_retire_via_route(
    retire_client: TestClient,
) -> None:
    """Seed eval-corpus result + usage data + zero blockers → READY.

    Uses wall-clock-relative seeds (the route can't accept a ``now``
    override) so the criterion-1 + criterion-2 math lands on whatever
    the test runner's clock says. The 40-day-old base + 4-week streak
    keeps both criteria green across a multi-year clock drift.
    """
    base = datetime.now(UTC) - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb", "blocker_counts": {"kb": 0}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_verdict"] == "READY TO RETIRE"
    assert body["surfaces"][0]["verdict"] == "READY TO RETIRE"


@pytest.mark.asyncio
async def test_integration_review_manually_via_route(
    retire_client: TestClient,
) -> None:
    """Same eval/usage as READY but no blocker_counts → REVIEW MANUALLY."""
    base = datetime.now(UTC) - timedelta(days=40)
    for op in ("op-A", "op-B", "op-C"):
        for week_offset in range(MIN_OPERATOR_STREAK_WEEKS + 1):
            await _seed_audit_row(
                operator_sub=op,
                tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
                path=f"{MCP_TOOL_PATH_PREFIX}search_knowledge",
                occurred_at=base + timedelta(days=7 * week_offset),
            )

    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-2", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb"},  # no blocker_counts
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_verdict"] == "REVIEW MANUALLY"


@pytest.mark.asyncio
async def test_integration_not_yet_via_route(retire_client: TestClient) -> None:
    """Empty audit_log → criteria 1+2 red → NOT YET regardless of blockers."""
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(key, sub="op-3", tenant_role=TenantRole.OPERATOR.value)
    fake_eval = AsyncMock(return_value=_stub_eval_result(kb_precision=0.90))
    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retire.eval_all", new=fake_eval),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = retire_client.post(
            "/api/v1/retrieve/retire-checklist",
            json={"surface": "kb", "blocker_counts": {"kb": 0}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_verdict"] == "NOT YET"
