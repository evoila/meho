# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Resolve a citation's ``source_url`` to a navigable link + human title (#1919).

A retrieved :class:`~meho_backplane.docs_search.service.DocsChunk` carries a
``source_url`` that, for the GCS-backed vendor corpus, is a **raw object
path** -- ``gs://meho-knowledge-vmware-corpus/kb/broadcom-kb/articles/41/414551.html``
or ``gs://.../community/williamlam/blog/.../post.md``. An operator can't open
a ``gs://`` URL: a browser has no handler for the scheme, so rendering it as an
``href`` produces a dead link. Yet the source *identity* is right there in the
path -- a Broadcom KB **article id** (``414551``), a named **community** post
-- and the path's final segment is a serviceable human title when the chunk
carries none.

This module maps each ``source_url`` to a :class:`CitationLink`: a navigable
canonical ``href`` where the source kind allows, a human ``label`` for the
link text, and a ``kind`` tag naming which rule matched. It is **declarative**
-- a fixed, ordered list of :class:`_Rule` keyed on path *shape*, not a
per-document mapping. The substrate stays dumb: adding a corpus that mirrors a
new source kind means appending one rule here, never a config row per
document.

Degradation is the whole point of the unrecognised arm. A path shape no rule
claims still yields a :class:`CitationLink` -- with a human ``label`` and a
``None`` ``href`` (``clickable`` is ``False``) -- so the caller renders *title
+ non-clickable path* rather than a broken ``gs://`` anchor. The one
invariant: **a ``gs://`` URL is never returned as an ``href``** (#1919 AC 2).
A non-``gs://`` ``source_url`` (an already-canonical ``https://`` link the
corpus handed back) passes straight through as the ``href`` -- there is
nothing to resolve.

The resolver does **no** network I/O: it derives links from the path alone (or
from an explicit canonical URL the chunk's metadata already carries). A
``stored_object`` proxy/signed link -- a server-issued URL to the object
behind the ``gs://`` path -- is a future arm (it needs a signing/proxy
endpoint, out of scope for #1919); until that endpoint exists the unrecognised
and product-PDF arms degrade to *title + path* rather than mint a link that
404s.

Faces
=====

The same function backs every ``source_url`` render so KB / community /
unknown resolve identically across surfaces:

* the MCP ``ask_docs`` payload enriches each citation with
  :func:`citation_link_payload` (the JSON form of a :class:`CitationLink`);
* the ``/ui/corpus`` cited-chunk render calls :func:`resolve_citation_link`
  per chunk for the anchor href + link text;
* a future ``POST /api/v1/ask_docs`` (#1917) reuses :func:`citation_link_payload`
  unchanged -- which is why the function takes the loose ``source_url`` /
  ``title`` / ``document_id`` triple, not a ``DocsChunk``: it must serve a
  REST response model and a Jinja context with one signature.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "CitationLink",
    "citation_link_payload",
    "normalize_source_ref",
    "resolve_citation_link",
]

#: The object-store scheme whose URLs are never returned as a clickable
#: ``href`` -- a browser has no handler for it, so it would render a dead
#: link (#1919 AC 2). Resolution either rewrites it to a canonical web URL
#: or degrades to a non-clickable label.
_GCS_SCHEME: Final[str] = "gs"

#: The backend-agnostic MEHO citation reference scheme (#132). When a chunk's
#: raw ``source_url`` has no derivable public URL, the wire carries an opaque
#: ``meho://docs/<collection>/<chunk_id>`` reference instead of the corpus's
#: raw object path -- so no storage-backend scheme (``gs://``, ``qdrant://``)
#: or internal bucket/directory layout reaches the consumer. MEHO owns the
#: reference->object resolution internally; the ref is stable per chunk.
_MEHO_DOCS_REF_SCHEME: Final[str] = "meho"

#: Placeholder collection segment in a ``meho://`` ref when the routing
#: collection key is unknown at projection time -- keeps the ref well-formed
#: (never ``meho://docs//<id>``) rather than emitting an empty segment.
_UNKNOWN_COLLECTION_SEGMENT: Final[str] = "_"

#: The canonical Broadcom KB article URL stem. A KB object path's numeric
#: article id (the filename, e.g. ``414551.html`` -> ``414551``) appends to
#: this. Verified against the live portal: ``/external/article/<id>`` resolves
#: to the article (knowledge.broadcom.com, 2026-06).
_BROADCOM_KB_ARTICLE_BASE: Final[str] = "https://knowledge.broadcom.com/external/article/"

#: A Broadcom KB article id is a run of digits (the filename stem of a KB
#: object). Anchored so a stem like ``414551-extra`` or ``index`` does not
#: smuggle a malformed id into the canonical URL -- a non-matching stem falls
#: through to the degraded arm rather than minting ``.../external/article/index``.
_KB_ARTICLE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class CitationLink:
    """A citation's resolved navigable link + human-readable label.

    ``href`` is a browser-navigable URL (canonical KB / community / a
    pass-through ``https`` source), or ``None`` when the source kind is not
    resolvable to one -- in which case ``label`` still names the source and the
    caller renders it as non-clickable text. ``href`` is **never** a ``gs://``
    URL. ``clickable`` is the derived convenience the template branches on so a
    Jinja author never re-checks ``href is not None``.

    ``kind`` names the rule that matched (``broadcom_kb`` / ``community`` /
    ``product_pdf`` / ``external`` / ``stored_object`` / ``unknown``) -- a
    stable tag a payload consumer or a test can assert on without re-deriving
    the classification from the href.
    """

    label: str
    href: str | None
    kind: str

    @property
    def clickable(self) -> bool:
        """Whether ``href`` is a navigable link (i.e. not ``None``)."""
        return self.href is not None


@dataclass(frozen=True, slots=True)
class _Source:
    """The parsed pieces of a ``source_url`` a rule predicate reads.

    Built once per resolution so each rule keys off the already-split scheme /
    path / segments instead of re-parsing. ``segments`` drops the leading
    empty element ``urlsplit`` yields for an absolute path, so
    ``segments[0]`` is the first real path component (``kb``, ``community``).
    """

    raw: str
    scheme: str
    path: PurePosixPath
    segments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Rule:
    """One declarative source-kind rule: a path-shape predicate + a builder.

    ``matches`` decides whether *this* rule claims a parsed source; ``build``
    turns it into the :class:`CitationLink`. Rules are tried in registration
    order, first match wins -- so the ordered :data:`_RULES` list *is* the
    classification policy, and adding a source kind is appending one entry.
    """

    kind: str
    matches: Callable[[_Source], bool]
    build: Callable[[_Source, str], CitationLink]


def _segments(path: PurePosixPath) -> tuple[str, ...]:
    """Return the non-root path components of *path* as a tuple.

    ``PurePosixPath('/kb/x/414551.html').parts`` is ``('/', 'kb', 'x',
    '414551.html')``; the leading ``'/'`` is dropped so callers index real
    segments from zero.
    """
    return tuple(part for part in path.parts if part != "/")


def _humanise_segment(segment: str) -> str:
    """Turn a path filename into a readable title (last-resort label).

    Drops the file extension and replaces ``-``/``_`` runs with spaces:
    ``quiesce-snapshots.md`` -> ``quiesce snapshots``, ``414551.html`` ->
    ``414551``. Used only when neither an explicit title nor a document id is
    available, so a citation always has *some* human label rather than a raw
    object path. An empty or whitespace-only result is rejected by the caller,
    which falls back further.
    """
    stem = PurePosixPath(segment).stem
    return re.sub(r"[-_]+", " ", stem).strip()


def _label_for(source: _Source, title: str | None, document_id: str | None) -> str:
    """Pick the human link text, preferring an explicit title.

    Order: an explicit ``title`` (the chunk's human title, when the corpus
    supplies one) -> the ``document_id`` (a stable, human-ish identifier) ->
    the humanised final path segment -> the raw ``source_url`` (so the label is
    never empty). Each candidate is stripped and skipped when blank.
    """
    for candidate in (title, document_id):
        if candidate and candidate.strip():
            return candidate.strip()
    if source.segments:
        humanised = _humanise_segment(source.segments[-1])
        if humanised:
            return humanised
    return source.raw


def _is_broadcom_kb(source: _Source) -> bool:
    """A GCS KB object: a ``broadcom-kb`` segment under a ``kb`` prefix.

    Keys on the path shape the corpus mirror uses
    (``.../kb/broadcom-kb/articles/.../<id>.html``) rather than the bucket
    name, so a re-bucketed corpus still classifies. Only ``gs://`` paths are
    rewritten to the canonical portal URL; an already-``https`` KB link is
    handled by the pass-through arm.
    """
    return source.scheme == _GCS_SCHEME and "broadcom-kb" in source.segments


def _build_broadcom_kb(source: _Source, label: str) -> CitationLink:
    """Build the canonical ``knowledge.broadcom.com`` link from the article id.

    The article id is the filename stem (``414551.html`` -> ``414551``); when
    it is a clean digit run it appends to :data:`_BROADCOM_KB_ARTICLE_BASE`.
    A stem that is not a plain numeric id (an unexpected KB object name) yields
    a non-clickable link -- we never mint ``/external/article/<garbage>`` that
    would 404; the label still names the source.
    """
    article_id = source.path.stem if source.segments else ""
    if _KB_ARTICLE_ID_RE.match(article_id):
        href = f"{_BROADCOM_KB_ARTICLE_BASE}{article_id}"
        return CitationLink(label=label, href=href, kind="broadcom_kb")
    return CitationLink(label=label, href=None, kind="broadcom_kb")


def _is_community(source: _Source) -> bool:
    """A GCS community-mirror object: a leading ``community`` path segment.

    Matches ``gs://.../community/<author>/...``. The original post URL is not
    recoverable from the mirror path alone (the path encodes author + slug, not
    the source host), so this arm degrades to *title + non-clickable path*
    unless an already-``https`` source URL is present (handled by the
    pass-through arm). The match still classifies the citation as
    ``community`` so a payload consumer / template can label it as such.
    """
    return (
        source.scheme == _GCS_SCHEME and bool(source.segments) and source.segments[0] == "community"
    )


def _build_community(source: _Source, label: str) -> CitationLink:
    """Build a community citation link.

    The mirror path does not carry the original post URL, so there is nothing
    to rewrite to: the link is non-clickable (title + path), never a broken
    ``gs://`` anchor. A community post whose canonical URL *is* known arrives
    as a pass-through ``https`` ``source_url`` and is handled before this arm.
    """
    return CitationLink(label=label, href=None, kind="community")


def _is_passthrough(source: _Source) -> bool:
    """An already-navigable web URL: an ``http``/``https`` ``source_url``.

    When the corpus hands back a canonical web link (not a ``gs://`` object
    path) there is nothing to resolve -- it is the href. Covers a KB or
    community source the corpus already canonicalised, and any product
    doc-portal page delivered as ``https``.
    """
    return source.scheme in ("http", "https")


def _build_passthrough(source: _Source, label: str) -> CitationLink:
    """Pass an already-canonical ``http``/``https`` source URL straight through."""
    return CitationLink(label=label, href=source.raw, kind="external")


#: The declarative, ordered source-kind rules. First match wins, so more
#: specific GCS shapes (KB, community) precede the generic ``http(s)``
#: pass-through. A ``gs://`` path no rule claims falls through to the degraded
#: arm in :func:`resolve_citation_link` -- never a clickable ``gs://`` href.
#: Adding a source kind (e.g. a product-PDF doc-portal mapping, or a
#: ``stored_object`` proxy once a signing endpoint exists) is appending an
#: entry here; no caller changes.
_RULES: Final[tuple[_Rule, ...]] = (
    _Rule(kind="broadcom_kb", matches=_is_broadcom_kb, build=_build_broadcom_kb),
    _Rule(kind="community", matches=_is_community, build=_build_community),
    _Rule(kind="external", matches=_is_passthrough, build=_build_passthrough),
)


def resolve_citation_link(
    source_url: str | None,
    *,
    title: str | None = None,
    document_id: str | None = None,
) -> CitationLink:
    """Resolve a citation's *source_url* to a navigable link + human label.

    The single entry point every face calls so a ``source_url`` renders
    identically across the MCP ``ask_docs`` payload, the ``/ui/corpus`` render,
    and a future REST ``ask_docs`` (#1917). Pure -- no network I/O; links are
    derived from the path (or an already-canonical web URL) alone.

    Resolution:

    1. **No source URL** -- a citation with no ``source_url`` at all. Returns a
       non-clickable link whose label is the title / document id (or the
       literal ``"(no source)"`` when neither is set). Nothing to link to.
    2. **A matching rule** -- the first :data:`_RULES` entry whose predicate
       claims the parsed source builds the link (canonical KB URL, community
       title+path, ``https`` pass-through).
    3. **Unrecognised** -- a ``gs://`` (or other non-web) path no rule claimed.
       Degrades to a non-clickable link (label + the raw path stays in the
       label / is shown beside it by the caller), tagged ``unknown``. A
       ``gs://`` URL is **never** returned as the ``href``.

    Args:
        source_url: The chunk's source citation. ``gs://`` object paths are
            resolved to a canonical web URL or degraded; ``http(s)`` URLs pass
            through; ``None`` yields a non-clickable label.
        title: The chunk's human title, when the corpus supplies one. Preferred
            as the link text.
        document_id: The chunk's document id -- the label fallback when no
            title is present.

    Returns:
        A :class:`CitationLink`. ``href`` is navigable or ``None`` (never a
        ``gs://`` URL); ``label`` is always a non-empty human string.
    """
    if source_url is None or not source_url.strip():
        empty = _Source(raw="", scheme="", path=PurePosixPath(), segments=())
        label = _label_for(empty, title, document_id)
        return CitationLink(label=label or "(no source)", href=None, kind="unknown")

    raw = source_url.strip()
    split = urlsplit(raw)
    path = PurePosixPath(split.path)
    source = _Source(raw=raw, scheme=split.scheme, path=path, segments=_segments(path))
    label = _label_for(source, title, document_id)

    for rule in _RULES:
        if rule.matches(source):
            return rule.build(source, label)

    # Unrecognised source kind (a gs:// object path no rule claimed, or any
    # other non-web scheme): degrade to a non-clickable label -- never a broken
    # gs:// href (#1919 AC 2).
    return CitationLink(label=label, href=None, kind="unknown")


def citation_link_payload(
    source_url: str | None,
    *,
    title: str | None = None,
    document_id: str | None = None,
) -> dict[str, object | None]:
    """Resolve *source_url* to the JSON shape an ``ask_docs`` citation carries.

    The serialised form of :func:`resolve_citation_link`, suitable to merge
    into a citation dict (the MCP ``ask_docs`` payload) or to back a REST
    response field (#1917). Keys mirror :class:`CitationLink`:

    * ``href`` -- the navigable URL, or ``None`` (never a ``gs://`` URL);
    * ``label`` -- the human link text;
    * ``kind`` -- the resolved source-kind tag;
    * ``clickable`` -- whether ``href`` is set (so a consumer need not
      null-check ``href``).
    """
    link = resolve_citation_link(source_url, title=title, document_id=document_id)
    return {
        "href": link.href,
        "label": link.label,
        "kind": link.kind,
        "clickable": link.clickable,
    }


def normalize_source_ref(
    source_url: str | None,
    *,
    collection_key: str | None,
    chunk_id: str,
    title: str | None = None,
    document_id: str | None = None,
) -> str:
    """Return a backend-agnostic citation reference for a chunk (#132).

    The wire ``source_url`` a ``search_docs`` / ``ask_docs`` consumer sees must
    never carry the storage backend's scheme (``gs://``, ``qdrant://``) or the
    corpus's internal bucket / directory layout -- FEATURES.md's ``doc-corpus``
    contract promises citations are "backend-agnostic ... the agent never sees
    the backend." This is the single seam both surfaces normalize their wire
    ``source_url`` through (``search_docs`` via
    :func:`~meho_backplane.docs_search.service._project_chunk`, which every
    ``DocsChunk`` -- including each ``ask_docs`` citation -- is born from).

    Normalization is **Option A (canonical public URL) with an Option B
    (opaque MEHO ref) fallback**:

    * When :func:`resolve_citation_link` derives a **canonical public URL** --
      a Broadcom KB article (``https://knowledge.broadcom.com/...``) or an
      already-``https`` source -- that URL *is* the reference. Most
      consumer-useful (a clickable citation), and a vendor/web URL exposes no
      MEHO or backend internals.
    * Otherwise -- a community / unrecognised ``gs://`` (or other non-web)
      object with no recoverable public URL, or no source at all -- the
      reference is an opaque ``meho://docs/<collection>/<chunk_id>``. Uniform
      regardless of backend, resolvable through MEHO, and **never null**
      (so a chunk always carries a usable reference). MEHO owns the
      reference->object mapping internally.

    The raw corpus object path (``gs://meho-knowledge-.../...``) is **never**
    returned -- that leak is exactly what this closes. A pure Option B (opaque
    ref for every chunk) was rejected: it would drop the clickable KB/web URL
    the resolver already derives, and Option A is the more consumer-useful
    half whenever a public URL exists.

    Args:
        source_url: The chunk's raw corpus citation (``gs://`` object path,
            an ``https`` URL, or ``None``).
        collection_key: The routing collection the chunk came from, used to
            namespace the opaque ``meho://`` ref. Falls back to
            :data:`_UNKNOWN_COLLECTION_SEGMENT` when unknown.
        chunk_id: The chunk's stable id -- the opaque ref's leaf.
        title: Optional chunk title (only affects the resolver's label, not
            the returned reference).
        document_id: Optional owning-document id (label fallback in the
            resolver; not part of the returned reference).

    Returns:
        A non-empty, backend-agnostic reference string: a canonical
        ``http(s)`` URL, or an opaque ``meho://docs/<collection>/<chunk_id>``.
        Never a ``gs://`` (or other backend-scheme) path.
    """
    link = resolve_citation_link(source_url, title=title, document_id=document_id)
    if link.href is not None:
        return link.href
    collection = (collection_key or "").strip() or _UNKNOWN_COLLECTION_SEGMENT
    return f"{_MEHO_DOCS_REF_SCHEME}://docs/{collection}/{chunk_id}"
