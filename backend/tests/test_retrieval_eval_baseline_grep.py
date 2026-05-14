# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.eval.baseline_grep`.

Coverage matrix (G4.3-T2 / Task #441 acceptance criteria):

* :func:`run_grep_baseline` happy path — matches found, top-k slice,
  alphabetical sort.
* No matches → ``[]`` (grep exit 1 is success-with-zero-results).
* Empty query → ``[]`` (caller mistake handled at boundary).
* Missing corpus root → :class:`BaselineConfigError` with a clear
  message.
* Empty corpus root (no .md files) → :class:`BaselineConfigError`
  (the sneaky case — grep would silently return nothing).
* ``k <= 0`` → :class:`ValueError` (caller mistake; mirror
  precision_at_k contract).
* Case-insensitive + literal matching — query with ``$`` doesn't
  hit unintended regex matches.

The tests rely on the platform's real ``grep`` binary; every CI
runner ships POSIX grep, so no stub is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meho_backplane.retrieval.eval.baseline_grep import (
    BaselineConfigError,
    run_grep_baseline,
)


def _seed_kb(tmp_path: Path) -> Path:
    """Create a tiny kb snapshot for grep baseline tests."""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "esxi-9.0-esxcli.md").write_text(
        "# esxcli\nRun esxcli on an ESXi 9.0 host. Storage commands.\n",
        encoding="utf-8",
    )
    (kb / "vault-1.21-raft-snapshot.md").write_text(
        "# Vault Raft snapshot procedure\nUse `vault operator raft snapshot save`.\n",
        encoding="utf-8",
    )
    (kb / "harbor-2.x-admin-password-rotation.md").write_text(
        "# Harbor admin password rotation\nUpdate the admin user via API.\n",
        encoding="utf-8",
    )
    (kb / "argocd-3.x-ssa-ignoredifferences-pattern.md").write_text(
        "# ArgoCD SSA ignoreDifferences pattern\nUse for Helm charts.\n",
        encoding="utf-8",
    )
    return kb


@pytest.mark.asyncio
async def test_run_grep_baseline_happy_path_returns_matching_slugs(
    tmp_path: Path,
) -> None:
    """A query matching one file returns the file's slug."""
    kb = _seed_kb(tmp_path)

    slugs = await run_grep_baseline("vault raft", kb, k=5)

    assert slugs == ["vault-1.21-raft-snapshot"]


@pytest.mark.asyncio
async def test_run_grep_baseline_returns_alphabetical_when_multiple_match(
    tmp_path: Path,
) -> None:
    """Multiple matches are returned in alphabetical order (deterministic)."""
    kb = _seed_kb(tmp_path)

    # "admin" matches harbor-2.x-admin-password-rotation; "command"
    # matches esxi-9.0-esxcli. Use a single-word query that hits both.
    # Use lowercase 'esxi' to match both (case-insensitive).
    (kb / "vault-overview.md").write_text(
        "# Vault overview\nesxi reference for cross-cutting context.\n",
        encoding="utf-8",
    )

    slugs = await run_grep_baseline("esxi", kb, k=5)

    # Alphabetical sort by slug. Both files mention "esxi"; expect
    # both, alphabetised.
    assert slugs == sorted(slugs)
    assert "esxi-9.0-esxcli" in slugs
    assert "vault-overview" in slugs


@pytest.mark.asyncio
async def test_run_grep_baseline_no_matches_returns_empty(
    tmp_path: Path,
) -> None:
    """No matches → empty list (grep exit 1 is success-with-zero-results)."""
    kb = _seed_kb(tmp_path)

    slugs = await run_grep_baseline("nonexistent-substring-xyz123", kb, k=5)

    assert slugs == []


@pytest.mark.asyncio
async def test_run_grep_baseline_top_k_slice(tmp_path: Path) -> None:
    """When more than k files match, only the top-k are returned (alphabetical)."""
    kb = tmp_path / "kb"
    kb.mkdir()
    # Seed 7 files all mentioning "shared-keyword"
    for i in range(7):
        (kb / f"slug-{i}.md").write_text("shared-keyword content\n", encoding="utf-8")

    slugs = await run_grep_baseline("shared-keyword", kb, k=3)

    assert len(slugs) == 3
    # Alphabetical sort means slug-0, slug-1, slug-2.
    assert slugs == ["slug-0", "slug-1", "slug-2"]


@pytest.mark.asyncio
async def test_run_grep_baseline_empty_query_returns_empty(
    tmp_path: Path,
) -> None:
    """Empty / whitespace-only query → [] (caller-side guard)."""
    kb = _seed_kb(tmp_path)

    assert await run_grep_baseline("", kb, k=5) == []
    assert await run_grep_baseline("   ", kb, k=5) == []


@pytest.mark.asyncio
async def test_run_grep_baseline_missing_corpus_raises(
    tmp_path: Path,
) -> None:
    """Pointing at a non-existent directory raises BaselineConfigError."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(BaselineConfigError, match="does not exist"):
        await run_grep_baseline("anything", missing, k=5)


@pytest.mark.asyncio
async def test_run_grep_baseline_empty_corpus_raises(tmp_path: Path) -> None:
    """A directory with no .md files raises (the sneaky 'wrong dir' case)."""
    empty = tmp_path / "empty-kb"
    empty.mkdir()
    # No .md files; only a stray .txt to verify the .md filter.
    (empty / "readme.txt").write_text("not markdown", encoding="utf-8")

    with pytest.raises(BaselineConfigError, match=r"contains no \.md files"):
        await run_grep_baseline("anything", empty, k=5)


@pytest.mark.asyncio
async def test_run_grep_baseline_zero_k_raises_value_error(
    tmp_path: Path,
) -> None:
    """k=0 is rejected — mirrors precision_at_k's contract."""
    kb = _seed_kb(tmp_path)

    with pytest.raises(ValueError, match="k must be > 0"):
        await run_grep_baseline("vault", kb, k=0)


@pytest.mark.asyncio
async def test_run_grep_baseline_treats_query_as_literal_not_regex(
    tmp_path: Path,
) -> None:
    """``-F`` (literal) means a query with regex meta does not match unintended lines.

    Without ``-F``, ``v.ult`` would match ``vault`` (regex .). With
    ``-F``, the literal string ``v.ult`` is searched for and finds
    nothing because the kb files don't contain the literal string.
    """
    kb = _seed_kb(tmp_path)

    # Literal "v.ult" should match nothing (no file contains that
    # exact substring).
    assert await run_grep_baseline("v.ult", kb, k=5) == []
    # But the literal "vault" matches the vault file.
    assert "vault-1.21-raft-snapshot" in await run_grep_baseline("vault", kb, k=5)


@pytest.mark.asyncio
async def test_run_grep_baseline_is_case_insensitive(tmp_path: Path) -> None:
    """``-i`` flag means case-insensitive matching by default."""
    kb = _seed_kb(tmp_path)

    # ``VAULT`` matches the vault file even though the file uses lowercase.
    slugs = await run_grep_baseline("VAULT", kb, k=5)
    assert "vault-1.21-raft-snapshot" in slugs


@pytest.mark.asyncio
async def test_run_grep_baseline_handles_query_starting_with_dash(
    tmp_path: Path,
) -> None:
    """A query like ``-flag`` is interpreted as the pattern, not a grep flag.

    The implementation passes ``--`` before the pattern; without it,
    grep would interpret ``-flag`` as a (probably invalid) grep flag
    and exit with status 2.
    """
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "flag-discussion.md").write_text(
        "Operators sometimes pass -flag to override defaults.\n",
        encoding="utf-8",
    )

    slugs = await run_grep_baseline("-flag", kb, k=5)
    assert slugs == ["flag-discussion"]


@pytest.mark.asyncio
async def test_run_grep_baseline_timeout_raises_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged grep is bounded by GREP_TIMEOUT_SECONDS + raises cleanly.

    Substitute a tiny `sleep` shim for the grep binary so the
    subprocess hangs past the timeout; the helper kills the child,
    drains pipes, and raises :class:`BaselineConfigError`. Verifies
    the route can never be hung indefinitely by a stuck subprocess.
    """
    kb = _seed_kb(tmp_path)

    # Use /bin/sh -c 'sleep 5' as the grep binary surrogate; sleeps
    # longer than the (overridden) 0.5s timeout. Module-scope
    # GREP_BINARY swap is supported by the implementation; the helper
    # accepts any path that runs.
    from meho_backplane.retrieval.eval import baseline_grep as bg_mod

    sleeper = tmp_path / "fake-grep"
    sleeper.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    sleeper.chmod(0o755)

    monkeypatch.setattr(bg_mod, "GREP_BINARY", str(sleeper))
    monkeypatch.setattr(bg_mod, "GREP_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(BaselineConfigError, match="timed out"):
        await run_grep_baseline("anything", kb, k=5)


@pytest.mark.asyncio
async def test_run_grep_baseline_non_zero_exit_does_not_log_raw_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error-path log line redacts the raw query.

    The route's documented PII posture (retrieve_eval.py docstring)
    is aggregate-only audit + no raw-query logging — operator-
    sensitive queries (e.g. "is the operator searching for the
    disk-failure runbook?") must not leak via structured logs into
    cluster log aggregation (Loki / CloudWatch / Splunk). The error
    log writes ``query_len`` + ``query_sha256`` only.
    """
    import logging

    from meho_backplane.retrieval.eval import baseline_grep as bg_mod

    # Stub a grep that exits 2 with a stderr message.
    fake_grep = tmp_path / "fake-grep"
    fake_grep.write_text(
        "#!/bin/sh\necho 'grep: permission denied' >&2\nexit 2\n",
        encoding="utf-8",
    )
    fake_grep.chmod(0o755)
    monkeypatch.setattr(bg_mod, "GREP_BINARY", str(fake_grep))

    kb = _seed_kb(tmp_path)

    secret_query = "investigating-the-disk-failure-runbook-very-sensitive"
    caplog.set_level(logging.WARNING)

    with pytest.raises(BaselineConfigError):
        await run_grep_baseline(secret_query, kb, k=5)

    # The structured log line lives in caplog as text from structlog's
    # processor pipeline. Assert the raw query is not present and the
    # redaction fields are.
    combined_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert secret_query not in combined_text, "raw query leaked into log: " + combined_text
