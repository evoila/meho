# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``grep -r`` retrieval baseline for the kb surface (G4.3-T2, #441).

The Initiative #373 retire-decision contract requires "MEHO ranking
≥ baseline" before retire can even be considered. The baseline is
the operator's pre-MEHO workflow: a literal ``grep -r`` against the
checked-in ``kb/`` snapshot, returning matching files in alphabetical
order. If MEHO's ranked retrieval is *worse* than running ``grep`` by
hand, retire is blocked regardless of absolute thresholds — there's
no point shipping a retrieval substrate that loses to the tool it
replaces.

Why a subprocess wrapper instead of in-process ``re``
-----------------------------------------------------

Two reasons, both load-bearing:

1. **The baseline is what operators actually run.** The
   pre-MEHO retire-criterion in locked-decisions.md decision #2 is
   literally "operators retire the in-repo path only once
   ``meho kb search`` is in daily use ... and grep returns
   equivalent answers". Calling the same ``grep`` binary the
   operators use on their laptops keeps the comparison apples-to-
   apples — a Python ``re.search`` baseline would silently differ
   on regex grammar (POSIX BRE vs. Python's PCRE-flavoured re),
   case-insensitivity defaults, and word-boundary semantics.

2. **Async I/O without blocking the event loop.** The runner is
   async; an in-process ``re.search`` walking the kb tree would
   either block the loop (sync filesystem walk) or require a
   separate ``asyncio.to_thread`` wrapper around the same logic
   the subprocess does for free. ``asyncio.create_subprocess_exec``
   is the v0.2-canonical pattern for "shell out to a non-Python
   tool from an async route".

Determinism
-----------

The result list is sorted alphabetically by slug — the same order
``grep -r`` produces on a POSIX filesystem (alphabetical sort by
path name). The sort is explicit because some filesystems (HFS+,
NTFS) walk entries in insertion order rather than lexicographic
order; explicit sort keeps the eval verdict stable across operator
laptops. The top-``k`` slice happens after the sort, so a corpus
with 12 matches always returns the same first 5 regardless of
filesystem.

Out of scope
------------

* **Memory + operations baselines.** Memory has no equivalent
  pre-MEHO workflow (operators don't ``grep`` their personal
  notes); operations baseline is "the operator's
  ``grep paths.txt + yq`` flow against locally-cloned spec" per
  the Initiative body, deferred to T3 (#442) when the operations
  corpus lands. Today this module ships kb-only.
* **Ranked grep alternatives** (ag, rg, ack-grep). locked-decisions
  pinned the baseline to literal ``grep`` per the operator
  workflow; switching to a ranked grep would produce different
  numbers and obscure the "is MEHO better than what they actually
  use?" signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import shutil
from pathlib import Path

import structlog

__all__ = [
    "GREP_BINARY",
    "GREP_TIMEOUT_SECONDS",
    "BaselineConfigError",
    "run_grep_baseline",
]

#: Hard wall-clock cap on a single grep invocation. A wedged grep
#: (network FS, large symlink loop, runaway corpus root) would
#: otherwise hang the FastAPI worker that called us, since the route
#: is request-path. The kb corpus is checked-in + small (<1k files,
#: <100KB each), so 15s is comfortably above the p99 walk time but
#: well below the operator-perceptible "the route is stuck" threshold.
#: Tunable at module scope so a future server-side corpus snapshot
#: with a larger tree can raise the cap without touching the call
#: site (T7 / v0.2.next).
GREP_TIMEOUT_SECONDS: float = 15.0

#: The grep binary the baseline shells out to. Module-level so tests
#: can patch it (e.g. to point at a stub script) without monkeypatching
#: the os.environ lookup at every call site. Default ``grep`` resolves
#: via ``$PATH``; the runner asserts the binary is reachable before
#: invoking it (see :func:`_resolve_grep`).
GREP_BINARY: str = "grep"


class BaselineConfigError(Exception):
    """Raised when the baseline cannot be computed for a configuration reason.

    Three failure modes:

    * The corpus root path doesn't exist on disk → operator pointed
      ``--corpus-root`` at a stale snapshot.
    * The corpus root exists but contains no ``.md`` files → wrong
      directory passed.
    * The ``grep`` binary is missing from ``$PATH`` → unusual but
      surfaces cleanly rather than as a confusing ``FileNotFoundError``
      from inside :func:`asyncio.create_subprocess_exec`.

    The runner translates this exception into a per-query "baseline
    skipped" entry rather than aborting the whole eval — the MEHO-
    ranking metrics are still useful even when the baseline can't run.
    """


def _resolve_grep() -> str:
    """Return the absolute path to the configured grep binary, or raise.

    Performs the ``$PATH`` lookup once per call so a misconfigured
    environment surfaces with a clear message before the subprocess
    spawn. ``shutil.which`` honours the same lookup rules
    :class:`subprocess.Popen` does, so a "found here" return is the
    same path the spawn would use.
    """
    resolved = shutil.which(GREP_BINARY)
    if resolved is None:
        raise BaselineConfigError(
            f"grep binary {GREP_BINARY!r} not found on $PATH; install grep "
            "or set GREP_BINARY at module scope to the absolute path"
        )
    return resolved


def _validate_corpus_root(corpus_root: Path) -> Path:
    """Resolve + sanity-check the corpus root before shelling out.

    Returns the absolute path. Raises :class:`BaselineConfigError`
    when the directory doesn't exist or is empty of ``.md`` files —
    the empty case is the sneaky one because ``grep -r`` against an
    empty tree exits 1 (no matches) which the runner would otherwise
    interpret as "baseline ran, returned nothing".
    """
    resolved = corpus_root.resolve()
    if not resolved.is_dir():
        raise BaselineConfigError(
            f"corpus root {corpus_root!s} does not exist or is not a directory"
        )
    # Cheap O(N) probe — kb corpora are at most a few hundred files;
    # the alternative (let grep find nothing and silently return) loses
    # the "you pointed at the wrong directory" diagnostic.
    if not any(resolved.rglob("*.md")):
        raise BaselineConfigError(
            f"corpus root {corpus_root!s} contains no .md files; pointed at the "
            "wrong directory or the snapshot is empty"
        )
    return resolved


async def run_grep_baseline(
    query: str,
    corpus_root: Path,
    *,
    k: int = 5,
) -> list[str]:
    """Run ``grep -r -l -i -F <query> <corpus_root>``; return top-``k`` slugs.

    Uses literal-string matching (``-F``) and case-insensitive
    comparison (``-i``) — what an operator types into a terminal on
    instinct. ``-l`` returns matching file paths only (not the
    matched lines), which is what the eval needs to compare against
    expected slugs. The result is sorted alphabetically by slug for
    determinism (see module docstring).

    A query with no matches returns ``[]`` cleanly — grep's exit code
    1 ("no lines matched") is treated as success-with-zero-results,
    not an error. Other non-zero exit codes (2 = trouble accessing a
    file) propagate as :class:`BaselineConfigError` because they
    indicate operator-actionable problems (permissions, broken
    symlinks).

    Parameters
    ----------
    query
        The free-form query string. Treated as a literal pattern
        (no regex metacharacter expansion) so an operator query
        containing ``$`` or ``.`` doesn't hit unintended matches.
    corpus_root
        Path to the kb directory snapshot (e.g. the consumer's
        checked-in ``kb/``).
    k
        Top-k slug count to return. Default 5 to mirror the
        precision@5 / coverage@5 contract.

    Returns
    -------
    list[str]
        Slugs (filenames without ``.md``) in alphabetical order, at
        most ``k`` entries. Empty list on no matches.

    Raises
    ------
    BaselineConfigError
        When ``corpus_root`` is missing/empty, ``grep`` is missing
        from ``$PATH``, or grep exits with a non-1, non-0 status.
    ValueError
        ``k <= 0`` — caller mistake; mirror the precision_at_k
        contract.
    """
    if k <= 0:
        raise ValueError(f"k must be > 0; got {k}")
    if not query.strip():
        # An empty query against grep would match every line of every
        # file, returning a meaningless top-k. Reject at the helper
        # boundary so the runner doesn't accidentally feed the
        # baseline a placeholder.
        return []

    grep_path = _resolve_grep()
    resolved_root = _validate_corpus_root(corpus_root)

    stdout_bytes = await _spawn_grep(grep_path, query, resolved_root)
    if not stdout_bytes:
        return []
    return _slugs_from_grep_stdout(stdout_bytes, k=k)


async def _spawn_grep(
    grep_path: str,
    query: str,
    corpus_root: Path,
) -> bytes:
    """Run grep against *query* + *corpus_root*; return stdout bytes.

    Returns empty bytes when grep exits 1 (no matches found). Raises
    :class:`BaselineConfigError` for any other non-zero exit code (2 =
    permissions / broken symlinks).

    Pass the query via argv (not via stdin) — it's a single opaque
    pattern, not a stream. ``asyncio.create_subprocess_exec`` uses
    execve under the hood, so argv values are not shell-interpreted:
    an operator query containing ``;`` or backticks cannot inject
    shell commands. Use ``--`` before the pattern so a leading
    ``-`` in the query (e.g. ``-flag``) is interpreted as the pattern,
    not as a grep flag.
    """
    proc = await asyncio.create_subprocess_exec(
        grep_path,
        "-r",
        "-l",
        "-i",
        "-F",
        "--include=*.md",
        "--",
        query,
        str(corpus_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Wall-clock cap on the subprocess to guarantee the FastAPI worker
    # is never blocked indefinitely by a wedged grep (network FS, big
    # symlink loop, etc.). On TimeoutError we kill -9 the child and
    # drain its pipes before raising, so we don't leak a zombie. The
    # raise path uses an aggregate-only message (no raw ``query``)
    # consistent with the route's audit_query PII posture.
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=GREP_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        proc.kill()
        # Drain stdout/stderr pipes so the kernel reclaims the FDs
        # immediately; we discard the bytes since the call timed out.
        # The drain itself is best-effort — if the kernel already
        # reaped the child between kill() and communicate() we don't
        # care, the FDs go either way.
        with contextlib.suppress(Exception):
            await proc.communicate()
        structlog.get_logger().warning(
            "baseline_grep_timeout",
            timeout_seconds=GREP_TIMEOUT_SECONDS,
            query_len=len(query),
            query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            corpus_root=str(corpus_root),
        )
        raise BaselineConfigError(
            f"grep baseline timed out after {GREP_TIMEOUT_SECONDS:.0f}s"
        ) from exc

    # grep exit codes: 0 = matches found, 1 = no matches, 2 = error.
    # Treat 0/1 as success-with-different-result-shapes; anything else
    # is an operator-actionable failure.
    if proc.returncode not in (0, 1):
        # Operator-sensitive queries ("is the operator searching for
        # the disk-failure runbook?") must not leak into structured
        # logs — the route's documented PII-aware aggregate-only
        # audit posture (retrieve_eval.py docstring) wins. Record
        # length + truncated hash so we can correlate the failure to
        # a specific input without writing the content to disk /
        # log-aggregation pipelines.
        structlog.get_logger().warning(
            "baseline_grep_nonzero_exit",
            returncode=proc.returncode,
            stderr=stderr_bytes.decode("utf-8", errors="replace")[:500],
            query_len=len(query),
            query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            corpus_root=str(corpus_root),
        )
        raise BaselineConfigError(
            f"grep exited with status {proc.returncode}; "
            f"stderr: {stderr_bytes.decode('utf-8', errors='replace')[:200]}"
        )
    if proc.returncode == 1:
        return b""
    return stdout_bytes


def _slugs_from_grep_stdout(stdout_bytes: bytes, *, k: int) -> list[str]:
    """Map grep stdout (one path per line) to deduped, sorted slug list.

    De-dup guards against grep listing the same file twice (shouldn't
    happen with ``-l`` but cheap to enforce). Sort is alphabetical
    for cross-filesystem determinism (see module docstring).
    """
    seen: set[str] = set()
    slugs: list[str] = []
    for raw in stdout_bytes.decode("utf-8").splitlines():
        path = raw.strip()
        if not path:
            continue
        slug = Path(path).stem
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    slugs.sort()
    return slugs[:k]
