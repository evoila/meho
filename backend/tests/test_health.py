# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``/healthz``, ``/version``, and ``/ready``.

Coverage matrix (per Task #19 acceptance criteria):

* ``/healthz`` always returns 200, even with a failing probe registered.
* ``/version`` returns the env-injected triple (``GIT_SHA``,
  ``BUILD_DATE``, ``CHART_VERSION``). ``git_sha`` / ``build_date`` fall
  back to ``"unknown"`` when their env vars are absent or empty;
  ``chart_version`` falls back to ``None`` (#631 — the chart's
  Deployment injects ``CHART_VERSION`` from ``.Chart.Version``).
* ``/ready`` returns 503 with an empty registry (default v0.1 state),
  200 once a passing probe is registered, and 503 again with one
  failing probe registered alongside a passing one (so the failure
  detail is visible in the payload).
* :func:`~meho_backplane.version.deployed_version_label` (#1698 — the
  UI footer's source string) prefers ``CHART_VERSION`` (``v``-prefixed)
  over a 12-char ``GIT_SHA`` truncation over the literal ``"unknown"``,
  reading the same env vars as ``/version`` so the two surfaces cannot
  disagree.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meho_backplane.health import (
    ProbeResult,
    clear_probes,
    register_probe,
    run_probes,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.version import deployed_version_label


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Reset the module-level probe registry around every test.

    The registry is a module global; without this fixture, tests that
    register probes leak state into siblings and run-order becomes
    load-bearing. Clearing both before *and* after defends against
    failures that abort mid-test.
    """
    clear_probes()
    yield
    clear_probes()


@pytest.fixture(autouse=True)
def _default_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for the ``/ready`` features block.

    G0.14-T7 (#1148) added a ``features`` block on ``/ready`` built from
    :func:`~meho_backplane.settings.get_settings`. The settings ctor
    requires :attr:`Settings.keycloak_issuer_url`,
    :attr:`Settings.keycloak_audience`, and :attr:`Settings.vault_addr`
    to be non-empty; the chassis-level health tests don't otherwise
    care about these values, so pin them here at sentinel defaults
    and bracket the cache.

    Tests that need different values (e.g. the features-block
    behavioural tests below) re-setenv inside their own body — the
    last-write semantics of ``monkeypatch.setenv`` keep that working.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Registry API surface
# ---------------------------------------------------------------------------


def test_registry_symbols_importable() -> None:
    """``register_probe`` and ``run_probes`` are part of the public API."""
    # Imports at module top would already have failed; this assertion
    # documents the contract for the acceptance-criteria reviewer.
    assert callable(register_probe)
    assert callable(run_probes)
    assert run_probes() == []


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_ignores_failing_probes(client: TestClient) -> None:
    """``/healthz`` is liveness, not readiness — registry state is irrelevant."""
    register_probe("always-fail", lambda: ProbeResult(name="always-fail", ok=False))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------


def test_version_falls_back_to_unknown(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_DATE", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "unknown",
        "build_date": "unknown",
        "chart_version": None,
    }


def test_version_reads_env_vars(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_DATE", "2026-05-09T12:00:00Z")
    monkeypatch.setenv("CHART_VERSION", "0.1.20260518-abc1234")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "abc1234",
        "build_date": "2026-05-09T12:00:00Z",
        "chart_version": "0.1.20260518-abc1234",
    }


def test_version_treats_empty_env_as_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty strings are as uninformative as unset.

    ``git_sha`` / ``build_date`` coerce to ``"unknown"``;
    ``chart_version`` coerces to ``None`` (its unset sentinel — the
    field is typed ``str | None`` and a null release is more honest
    than the string ``"unknown"`` for a value that is only ever a
    semver-shaped chart version when known).
    """
    monkeypatch.setenv("GIT_SHA", "")
    monkeypatch.setenv("BUILD_DATE", "")
    monkeypatch.setenv("CHART_VERSION", "")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "unknown",
        "build_date": "unknown",
        "chart_version": None,
    }


def test_version_chart_version_unset_is_null(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CHART_VERSION`` unset (bare-image / local run) → ``null``, no exception."""
    monkeypatch.setenv("GIT_SHA", "deadbeef")
    monkeypatch.setenv("BUILD_DATE", "2026-05-18T00:00:00Z")
    monkeypatch.delenv("CHART_VERSION", raising=False)

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["chart_version"] is None


# ---------------------------------------------------------------------------
# deployed_version_label (#1698 — the UI footer's build-identity string)
# ---------------------------------------------------------------------------


def test_label_prefers_chart_version_with_v_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chart deploy shows the release identity, ``v``-prefixed.

    ``chart.yml`` stamps plain semver on tag pushes (Helm rejects a
    leading ``v`` in ``Chart.yaml``), so the label adds the prefix for
    the operator-facing rendering: ``CHART_VERSION=0.14.0`` →
    ``v0.14.0``. ``GIT_SHA`` being set too must not matter.
    """
    monkeypatch.setenv("CHART_VERSION", "0.14.0")
    monkeypatch.setenv("GIT_SHA", "deadbeefcafe0123456789aa")

    assert deployed_version_label() == "v0.14.0"


def test_label_does_not_double_v_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``CHART_VERSION`` already carrying ``v`` is passed through."""
    monkeypatch.setenv("CHART_VERSION", "v0.15.0-rc1")

    assert deployed_version_label() == "v0.15.0-rc1"


def test_label_falls_back_to_short_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare-image runs (no chart) show the first 12 hash chars, unprefixed."""
    monkeypatch.delenv("CHART_VERSION", raising=False)
    monkeypatch.setenv("GIT_SHA", "2bbea9ad00112233445566778899aabbccddeeff")

    assert deployed_version_label() == "2bbea9ad0011"


def test_label_unknown_when_no_build_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local ``uvicorn`` runs degrade to ``unknown`` — never ``0.1.0-dev``."""
    monkeypatch.delenv("CHART_VERSION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)

    assert deployed_version_label() == "unknown"


def test_label_treats_dockerfile_default_sha_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ARG GIT_SHA=unknown`` (Dockerfile default) is not a real hash.

    A locally-built image without ``--build-arg GIT_SHA=...`` carries
    the literal ``unknown`` in the env; the label must not render it as
    if it were a 7-char commit id. Empty strings degrade the same way
    (mirrors ``/version``'s ``or "unknown"`` coercion).
    """
    monkeypatch.delenv("CHART_VERSION", raising=False)
    monkeypatch.setenv("GIT_SHA", "unknown")
    assert deployed_version_label() == "unknown"

    monkeypatch.setenv("GIT_SHA", "")
    monkeypatch.setenv("CHART_VERSION", "")
    assert deployed_version_label() == "unknown"


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------


def test_ready_with_empty_registry_returns_503(client: TestClient) -> None:
    """The chassis fails closed until a downstream Initiative wires probes.

    The 503 still carries the ``features`` block (G0.14-T7 #1148) —
    feature-gate visibility is independent of the probe verdict so
    an operator's "what's wired?" question is answerable even when
    the chassis is otherwise un-ready.
    """
    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == []
    # Features block is always present; the per-feature shape is
    # covered exhaustively in tests/test_features.py — this assertion
    # only pins the wire-presence + key set.
    assert set(body["features"].keys()) == {
        "agent_runtime",
        "ui_surface",
        "audit_replay",
        "approval_queue",
        "mcp",
    }


def test_ready_with_all_passing_probes_returns_200(client: TestClient) -> None:
    register_probe(
        "vault",
        lambda: ProbeResult(name="vault", ok=True, detail="auth ok"),
    )
    register_probe("db", lambda: ProbeResult(name="db", ok=True))

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == [
        {"name": "vault", "ok": True, "detail": "auth ok"},
        {"name": "db", "ok": True, "detail": None},
    ]
    # 200 branch also carries the features block.
    assert set(body["features"].keys()) == {
        "agent_runtime",
        "ui_surface",
        "audit_replay",
        "approval_queue",
        "mcp",
    }


def test_ready_with_one_failing_probe_returns_503_with_detail(
    client: TestClient,
) -> None:
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe(
        "db",
        lambda: ProbeResult(name="db", ok=False, detail="migration pending"),
    )

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == [
        {"name": "vault", "ok": True, "detail": None},
        {"name": "db", "ok": False, "detail": "migration pending"},
    ]
    # Features block survives a probe failure — the probe verdict is
    # orthogonal to the feature-gate visibility.
    assert "features" in body


def test_ready_features_block_reflects_unwired_keycloak_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``features`` block reads from the live :class:`Settings`.

    When the admin env vars are unset, ``agent_runtime.configured`` is
    ``False`` and ``missing_env`` lists the three KEYCLOAK_ADMIN_*
    keys. This is the load-bearing operator-facing answer to
    signals 16 + 17: hitting one GET tells you whether the
    agent-principal surface will work before you trip the 503.
    """
    from meho_backplane.settings import get_settings

    monkeypatch.delenv("KEYCLOAK_ADMIN_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        response = client.get("/ready")
    finally:
        get_settings.cache_clear()

    body = response.json()
    agent_runtime = body["features"]["agent_runtime"]
    assert agent_runtime["configured"] is False
    assert agent_runtime["missing_env"] == [
        "KEYCLOAK_ADMIN_URL",
        "KEYCLOAK_ADMIN_CLIENT_ID",
        "KEYCLOAK_ADMIN_CLIENT_SECRET",
    ]
    assert agent_runtime["docs"] == "docs/cross-repo/keycloak-agent-client.md"


def test_ready_features_block_reflects_wired_keycloak_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the admin env vars are wired, the gate flips to configured."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    get_settings.cache_clear()
    try:
        response = client.get("/ready")
    finally:
        get_settings.cache_clear()

    body = response.json()
    agent_runtime = body["features"]["agent_runtime"]
    assert agent_runtime["configured"] is True
    assert agent_runtime["missing_env"] == []
    # When agent_runtime is wired, approval_queue follows.
    approval_queue = body["features"]["approval_queue"]
    assert approval_queue["configured"] is True
    assert approval_queue["depends_on"] == "agent_runtime"


def test_ready_features_block_carries_mcp_protocol_version(
    client: TestClient,
) -> None:
    """/ready surfaces the server-pinned MCP protocol revision.

    Acceptance criterion (G0.14-T13 #1202): the ``features.mcp`` block
    on ``/ready`` carries ``protocol_version`` matching the build-time
    :data:`~meho_backplane.mcp.schemas.PROTOCOL_VERSION` constant.
    Operators get a single unauthenticated GET that answers "which MCP
    revision will this server pin in handshake responses?". The
    matching :class:`~meho_backplane.api.v1.health.HealthResponse`
    field (``mcp_protocol_version``) carries the same value on the
    authenticated surface so both views stay consistent.
    """
    from meho_backplane.mcp.schemas import PROTOCOL_VERSION

    response = client.get("/ready")

    body = response.json()
    mcp = body["features"]["mcp"]
    assert mcp["configured"] is True
    assert mcp["protocol_version"] == PROTOCOL_VERSION
    assert mcp["missing_env"] == []
