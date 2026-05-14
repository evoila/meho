# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Directory walker that turns a kb directory into ingest-ready records.

Given a root path, the walker yields :class:`KbFileRecord` instances
for every Markdown file under the root that is not hidden and not
matched by the optional ``.kb-ignore`` file. Each record carries the
extracted slug (filename stem, or the ``slug:`` front-matter
override when present), the body text (with the front-matter
stripped if any was present), and the front-matter metadata dict.

Filters
-------

* **Hidden files** -- any path with a component starting with ``.``
  is skipped. Catches editor swap files (``.foo.md.swp``),
  ``.git/`` checkouts, ``.DS_Store`` directories, and the
  ``.kb-ignore`` file itself. Implemented by walking ``Path.parts``
  rather than relying on POSIX ``hidden`` semantics, so a Windows
  operator's checkout produces the same skip set as a Linux operator's.
* **``.kb-ignore`` patterns** -- when the root contains a
  ``.kb-ignore`` file, its non-comment, non-blank lines are parsed
  as ``fnmatch`` glob patterns matched against the file's path
  relative to the kb root (forward-slash separators on every
  platform). Patterns matching a directory name (e.g.
  ``drafts/*``) match any file beneath that directory. The file
  format is intentionally tiny:

  * One pattern per line.
  * Lines starting with ``#`` are comments.
  * Blank lines are ignored.
  * Patterns are glob patterns evaluated via :func:`fnmatch.fnmatch`.

  Gitignore-style negation (``!``), anchoring (``/``), and ``**``
  recursion are deliberately out of scope -- the operator's expected
  use case is "skip drafts/ and todo.md", not "model a full VCS
  ignore policy". Operators with richer ignore needs should pre-
  filter their kb directory before pointing the walker at it.

Slug extraction
---------------

* Default: ``Path.stem`` of the filename (``vcenter-9.0-snapshot-revert.md``
  → ``vcenter-9.0-snapshot-revert``).
* Override: when the front-matter contains a non-empty string at the
  ``slug`` key, that string wins. The override is the future-compat
  hook for cases where the filesystem-name and operator-facing slug
  must diverge (renaming a slug without breaking the URL the way a
  filename rename would).
* Either way the resulting slug is validated against
  :data:`~meho_backplane.kb.schemas.SLUG_PATTERN`. A slug that fails
  validation surfaces as :class:`~meho_backplane.kb.schemas.InvalidKbSlugError`
  from :func:`walk_kb_directory`; the service-level wrapper catches
  the exception and counts the file as an error.

Front-matter parsing
--------------------

Front-matter is parsed via the ``python-frontmatter`` 1.x library:
``frontmatter.loads(text)`` returns a :class:`frontmatter.Post`
whose ``.metadata`` is a dict and ``.content`` is the body text with
the ``---`` delimiters stripped. Files without front-matter return
``Post.metadata == {}`` and ``Post.content == <original text>``;
malformed YAML raises ``yaml.YAMLError`` which the walker re-raises
as :class:`KbFileParseError` so callers can distinguish parse
failure from disk-read failure.

Out of scope (deferred)
-----------------------

* Non-Markdown formats (``.rst`` / ``.org`` / ``.html``) -- the
  consumer's kb is Markdown-only, and supporting more formats means
  picking a multi-format parser. v0.2.next.
* Symlink loop detection -- :func:`Path.rglob` follows symlinks by
  default. A pathological kb with a self-referential symlink would
  raise :class:`OSError` from the underlying walker; the service
  catches and counts as error. A real cycle detector lands when an
  operator hits it in practice.
* Watching for filesystem changes (auto-reingest) -- v0.2 is
  explicitly on-demand only per the parent Initiative.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import structlog
import yaml

from meho_backplane.kb.schemas import validate_slug

__all__ = [
    "KB_IGNORE_FILENAME",
    "KbFileParseError",
    "KbFileRecord",
    "walk_kb_directory",
]


#: Operator-config filename consulted at the kb root. One file at the
#: root only; nested ``.kb-ignore`` files are not consulted (the
#: alternative -- per-directory cascading patterns -- expands the
#: operator's mental model without a clear v0.2 win).
KB_IGNORE_FILENAME: str = ".kb-ignore"


@dataclass(frozen=True)
class KbFileRecord:
    """One discovered Markdown file, ready to feed into ``index_document``.

    ``path`` is the absolute path to the source file (useful for
    error messages and metadata enrichment); ``slug`` is the
    operator-facing identifier the kb service uses as
    ``documents.source_id``; ``body`` is the file content with
    front-matter stripped; ``metadata`` is the front-matter dict
    (``{}`` when the file has no front-matter).

    Frozen so the walker's output cannot be silently mutated by a
    caller mid-loop. Lightweight enough to hold for an entire kb
    in memory (typical corpus: ~50 files, ~5 KB each → ~250 KB).
    """

    path: Path
    slug: str
    body: str
    metadata: dict[str, object]


class KbFileParseError(ValueError):
    """The file's front-matter could not be parsed as YAML.

    Subclass of :class:`ValueError` -- same parent as
    :class:`~meho_backplane.kb.schemas.InvalidKbSlugError` so a caller
    treating the failure as input-shape problem can use one ``except``
    clause. The original :class:`yaml.YAMLError` is chained via the
    standard ``raise ... from ...`` so the underlying YAML error
    detail (line number, expected token) is preserved.
    """


def walk_kb_directory(
    root: Path,
    errors: list[str] | None = None,
) -> Iterator[KbFileRecord]:
    """Yield one :class:`KbFileRecord` per ingestible Markdown file under *root*.

    Walks *root* recursively, skipping hidden paths and any path
    matched by the optional :data:`KB_IGNORE_FILENAME` patterns at
    the root. Each yielded record carries the slug (validated) plus
    the body and metadata extracted from the file's front-matter.

    Per-file error handling depends on whether *errors* is supplied:

    * ``errors is None`` (strict mode, default) -- the first
      per-file failure (read error, front-matter parse error, slug
      validation failure) propagates out of the generator and
      terminates iteration. Use this shape when a single bad file
      should abort the run.
    * ``errors`` is a list (best-effort mode) -- per-file failures
      are caught, formatted as ``"<path>: <reason>"``, and appended
      to the supplied list; the walker continues with the next
      file. Use this shape from :meth:`KbService.ingest_directory`
      where a single bad file must not abort a 44-entry corpus.
      A Python generator is closed once an internal exception
      propagates -- recovering across files requires catching
      *inside* the loop rather than wrapping the consumer's
      ``next()`` call.

    Parameters
    ----------
    root
        Path to the kb directory. Must exist and be a directory;
        otherwise :class:`NotADirectoryError` / :class:`FileNotFoundError`
        is raised from the underlying :meth:`Path.is_dir` check. The
        path is resolved (symlinks normalised) once at the top so
        downstream relative-path computations are stable.
    errors
        Optional list to which per-file error strings are appended
        (best-effort mode). When ``None`` (strict mode) per-file
        failures propagate.

    Yields
    ------
    KbFileRecord
        One per discovered ingestible file, in lexicographic order
        within each directory (the order :func:`Path.rglob` produces
        on a sorted filesystem is platform-dependent, so callers that
        need deterministic ordering should sort the iterator output
        explicitly).
    """
    log = structlog.get_logger()
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"kb root is not a directory: {root}")

    ignore_patterns = _read_ignore_patterns(root)
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        if _is_hidden(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        if _is_ignored(rel, ignore_patterns):
            log.debug("kb_file_ignored", path=str(path), patterns=ignore_patterns)
            continue
        try:
            yield _build_record(path)
        except (UnicodeDecodeError, KbFileParseError, ValueError, OSError) as exc:
            # ValueError covers InvalidKbSlugError (subclass).
            # OSError covers PermissionError + other read-time
            # failures the rglob iterator may surface lazily on
            # individual entries (network mounts, race with a
            # concurrent rm). In strict mode (errors is None) we
            # re-raise so the caller sees the original exception
            # type; in best-effort mode we append a formatted entry
            # and continue.
            if errors is None:
                raise
            errors.append(f"{path}: {exc}")


def _read_ignore_patterns(root: Path) -> tuple[str, ...]:
    """Read root's ``.kb-ignore`` file into a tuple of glob patterns.

    Returns an empty tuple when no file exists. Lines starting with
    ``#`` and blank lines are filtered out. Trailing whitespace on
    every pattern is stripped so a stray space at end of line does
    not produce a never-matching pattern. The file is read with
    UTF-8; any decode error surfaces as :class:`UnicodeDecodeError`
    to the caller -- a kb root with a non-UTF-8 ``.kb-ignore`` is
    operator misconfiguration we want to fail loudly on, not
    silently swallow.
    """
    ignore_file = root / KB_IGNORE_FILENAME
    if not ignore_file.is_file():
        return ()
    lines = ignore_file.read_text(encoding="utf-8").splitlines()
    patterns = tuple(
        stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")
    )
    return patterns


def _is_hidden(path: Path, root: Path) -> bool:
    """Return True when any component of *path* relative to *root* starts with ``.``.

    Forward-slash separator works on every platform because
    :meth:`Path.relative_to` produces a :class:`PurePath` whose
    ``.parts`` are OS-agnostic. The root's own components are
    intentionally excluded from the check -- an operator with a kb
    living at ``/Users/.config/kb`` should be able to ingest it.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        # ``relative_to`` raises when *path* is not under *root*. The
        # ``rglob`` iterator never produces a path outside root, but
        # defensive coding keeps this helper testable in isolation.
        return True
    return any(part.startswith(".") for part in rel.parts)


def _is_ignored(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Return True when *rel_path* matches any of *patterns* via :func:`fnmatch.fnmatch`.

    Patterns are matched against the full forward-slash relative path
    *and* against each path component independently, so a pattern
    like ``drafts/*`` matches ``drafts/foo.md`` (full-path match)
    and a pattern like ``drafts`` matches every file under
    ``drafts/`` (component match). The component pass is what makes
    ``.kb-ignore`` ergonomic for the common "skip this whole
    directory" intent without forcing the operator to write
    ``drafts/**``.
    """
    if not patterns:
        return False
    parts = rel_path.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _build_record(path: Path) -> KbFileRecord:
    """Read *path*, extract front-matter, return a :class:`KbFileRecord`.

    The read is UTF-8 with strict decoding -- a binary file
    masquerading as ``.md`` raises :class:`UnicodeDecodeError`,
    which the caller catches and counts as an error. Front-matter
    parse failures raise :class:`KbFileParseError` chained from the
    underlying :class:`yaml.YAMLError`.

    Slug extraction order: front-matter ``slug`` key (when present
    and a non-empty string) wins over the filename stem. Either way
    the resulting value is validated against the slug regex.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # Re-raise with the path baked into the message so the
        # caller's error string is operator-actionable. Chaining
        # preserves the original codec error for log forensics.
        raise UnicodeDecodeError(
            exc.encoding,
            exc.object,
            exc.start,
            exc.end,
            f"could not decode {path} as UTF-8: {exc.reason}",
        ) from exc

    try:
        post = frontmatter.loads(raw)
    except yaml.YAMLError as exc:
        raise KbFileParseError(f"front-matter parse error in {path}: {exc}") from exc

    metadata = dict(post.metadata)
    body = post.content

    override = metadata.get("slug")
    if isinstance(override, str) and override:
        slug = validate_slug(override)
    else:
        slug = validate_slug(path.stem)

    return KbFileRecord(path=path, slug=slug, body=body, metadata=metadata)
