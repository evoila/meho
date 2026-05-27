# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`scripts.release.check_release_body_paths`.

The release-body path-freshness gate (G0.13-T6 / #1136) is the
release-time sister to the PR-time #928 OpenAPI snapshot freshness
check. Three consecutive releases shipped with broken path
citations in the release body (v0.5.0 missing notes entirely;
v0.5.1's connector raw-REST on-ramp pointing at the catalog;
v0.6.0's ``audit/replay`` + ``tenant_conventions`` drift) — a
recurring class of defect that deserves a CI-style gate, not a
per-cycle spot-check.

These tests pin three contracts the script promises:

1. **Happy path** — every cited path that resolves (as either a
   literal OpenAPI key or a templated form) returns exit 0.
2. **Failure path** — a cited path that has no resolution in the
   snapshot returns exit 1 with a diagnostic on stderr.
3. **Template-awareness** — a citation like
   ``/api/v1/audit/sessions/<uuid>/replay`` resolves against the
   OpenAPI template ``/api/v1/audit/sessions/{session_id}/replay``
   without manual whitelisting; a citation of the wrong shape
   (e.g. the drifted ``/api/v1/audit/replay``) does not.

The tests invoke the script as a subprocess so the entry-point
behaviour (argparse + exit codes) is exercised end-to-end, in line
with ``test_ci_run_eval_gate.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


def _repo_root(start: Path) -> Path:
    """Walk up from the test file to the repo root (where scripts/ lives)."""
    here = start.resolve()
    for parent in (here, *here.parents):
        if (parent / "scripts" / "release" / "check_release_body_paths.py").exists():
            return parent
    raise RuntimeError(
        "could not find repo root containing scripts/release/check_release_body_paths.py"
    )


REPO_ROOT = _repo_root(Path(__file__))
GATE_SCRIPT = REPO_ROOT / "scripts" / "release" / "check_release_body_paths.py"


def _run_gate(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke check_release_body_paths.py with the given args."""
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_openapi(tmp: Path, paths: list[str]) -> Path:
    """Write a minimal OpenAPI snapshot with the requested paths."""
    snapshot = tmp / "openapi.json"
    snapshot.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "test", "version": "0.0.0"},
                "paths": {p: {"get": {"responses": {"200": {"description": "ok"}}}} for p in paths},
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def _write_release_body(tmp: Path, text: str) -> Path:
    body = tmp / "release-body.md"
    body.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
    return body


def test_happy_path_literal_match(tmp_path: Path) -> None:
    """A citation that is verbatim an OpenAPI path returns exit 0."""
    snapshot = _write_openapi(tmp_path, ["/api/v1/conventions", "/api/v1/conventions/{slug}"])
    body = _write_release_body(
        tmp_path,
        """
        ## What ships

        - 3 routes under `/api/v1/conventions` (list + show + history).
        - Show one: `/api/v1/conventions/{slug}`.
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "release-body paths OK" in result.stdout


def test_failure_path_v060_audit_replay_drift(tmp_path: Path) -> None:
    """The v0.6.0 ``audit/replay`` drift is caught by the gate.

    Reproduces the exact defect this Task closes: the release body
    cites ``GET /api/v1/audit/replay`` while the shipped route is
    ``GET /api/v1/audit/sessions/{session_id}/replay``. The gate
    must flag this even though both paths share a common prefix.
    """
    snapshot = _write_openapi(
        tmp_path,
        ["/api/v1/audit/sessions/{session_id}/replay", "/api/v1/audit/query"],
    )
    body = _write_release_body(
        tmp_path,
        """
        ## Audit replay

        Surfaced as `GET /api/v1/audit/replay` with a 10k cap.
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 1, (
        f"expected exit 1; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "release-body paths FAILED" in result.stderr
    assert "/api/v1/audit/replay" in result.stderr
    # Closest-match hint guides the maintainer to the right path.
    assert "/api/v1/audit/sessions/{session_id}/replay" in result.stderr


def test_uuid_resolves_against_template(tmp_path: Path) -> None:
    """A citation with a concrete UUID resolves against the templated OpenAPI path.

    Release bodies sometimes include example URLs with concrete IDs
    in the prose; the gate templatises the citation so a legitimate
    example doesn't trip the drift check.
    """
    snapshot = _write_openapi(
        tmp_path,
        ["/api/v1/audit/sessions/{session_id}/replay"],
    )
    body = _write_release_body(
        tmp_path,
        """
        Example call:
        `/api/v1/audit/sessions/01234567-89ab-cdef-0123-456789abcdef/replay`
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"expected exit 0 for UUID-against-template match; got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_allow_path_whitelist(tmp_path: Path) -> None:
    """``--allow-path`` lets a maintainer waive a path that isn't in the snapshot.

    Useful for forward-looking citations (v0.7 paths announced in a
    v0.6 release body) or paths served by a sibling service.
    """
    snapshot = _write_openapi(tmp_path, ["/api/v1/conventions"])
    body = _write_release_body(
        tmp_path,
        """
        See also the forthcoming `/api/v2/projected-future-path`
        landing in the next minor.
        """,
    )

    # Without the whitelist, the gate fails.
    failing = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )
    assert failing.returncode == 1

    # With the whitelist, the gate passes.
    passing = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        "--allow-path",
        "/api/v2/projected-future-path",
        cwd=tmp_path,
    )
    assert passing.returncode == 0, (
        f"expected exit 0 with whitelist; got {passing.returncode}.\n"
        f"stdout: {passing.stdout}\nstderr: {passing.stderr}"
    )


def test_no_paths_in_body_passes(tmp_path: Path) -> None:
    """A release body with no ``/api/v*`` citations trivially passes."""
    snapshot = _write_openapi(tmp_path, ["/api/v1/conventions"])
    body = _write_release_body(
        tmp_path,
        """
        ## What ships

        Pure-CLI release; no API changes.
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert "0 cited path(s)" in result.stdout


def test_trailing_punctuation_stripped(tmp_path: Path) -> None:
    """A citation followed by sentence-final punctuation matches.

    Markdown prose frequently writes ``... at `/api/v1/foo`.`` —
    the trailing period is not part of the path. The extractor
    strips trailing punctuation post-match so the citation resolves.
    """
    snapshot = _write_openapi(tmp_path, ["/api/v1/conventions"])
    body = _write_release_body(
        tmp_path,
        """
        See `/api/v1/conventions`, the routes list. Also try `/api/v1/conventions`!
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )
    assert result.returncode == 0


def test_missing_release_body_exits_two(tmp_path: Path) -> None:
    """Unreadable release body → exit 2 with a clear error message."""
    snapshot = _write_openapi(tmp_path, ["/api/v1/conventions"])
    nonexistent = tmp_path / "missing.md"

    result = _run_gate(
        "--release-body",
        str(nonexistent),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "cannot read release body" in result.stderr


def test_malformed_openapi_exits_two(tmp_path: Path) -> None:
    """Malformed OpenAPI snapshot → exit 2.

    The gate refuses to declare the release body OK when it can't
    actually validate the citations against the snapshot.
    """
    bad_snapshot = tmp_path / "bad.json"
    bad_snapshot.write_text("not valid json {", encoding="utf-8")
    body = _write_release_body(tmp_path, "## Empty\n\nNo paths.\n")

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(bad_snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "OpenAPI snapshot" in result.stderr


def test_openapi_with_no_paths_key_exits_two(tmp_path: Path) -> None:
    """OpenAPI JSON missing the ``paths`` key → exit 2.

    Defends against an empty / truncated snapshot file silently
    passing the gate because every path looks "missing".
    """
    snapshot = tmp_path / "no-paths.json"
    snapshot.write_text(
        json.dumps({"openapi": "3.1.0", "info": {"title": "t", "version": "0"}}),
        encoding="utf-8",
    )
    body = _write_release_body(tmp_path, "See `/api/v1/foo`.\n")

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(snapshot),
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "paths" in result.stderr


def test_v060_real_release_body_after_amendment(tmp_path: Path) -> None:
    """End-to-end smoke: the amended v0.6.0 body passes against shipped OpenAPI.

    Reads the live ``cli/api/openapi.json`` and a small fixture
    that mirrors the structure of the amended v0.6.0 release body
    section. Pinning the live snapshot path here means a future
    drift between the script and the published OpenAPI surface is
    caught at PR time.
    """
    live_snapshot = REPO_ROOT / "cli" / "api" / "openapi.json"
    if not live_snapshot.exists():  # pragma: no cover - CI always has it
        return

    # Mirror the amended phrasing the v0.6.0 release body should land on.
    body = _write_release_body(
        tmp_path,
        """
        ### Audit replay

        Surfaced as `GET /api/v1/audit/sessions/{session_id}/replay`
        with a 10k cap.

        ### Conventions

        3 tenant-scoped routes mounted at `/api/v1/conventions`,
        `/api/v1/conventions/{slug}`, and
        `/api/v1/conventions/{slug}/history`.
        """,
    )

    result = _run_gate(
        "--release-body",
        str(body),
        "--openapi-snapshot",
        str(live_snapshot),
        cwd=tmp_path,
    )

    assert result.returncode == 0, (
        f"amended v0.6.0 body should pass against live OpenAPI; "
        f"got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
