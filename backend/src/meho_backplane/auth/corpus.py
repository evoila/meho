# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backplane→corpus federation client (G4.5-T2 #1520).

The ``search_docs`` add-on (Initiative #1518) routes vendor-document
queries through the backplane to an **external** corpus service the ops
team runs, rather than ingesting the corpus into MEHO's own substrate.
This module is the one place that federation happens: a thin async
``httpx`` client that POSTs a search request to a screened ``https``
corpus URL carrying a **deployment-configured** corpus service credential
(``settings.corpus_service_token``).

The caller's raw inbound operator JWT is **never** forwarded
(evoila-bosnia/meho-internal#290). The corpus URL is read from a
tenant-configurable ``backend.ref`` (the ``meho-docs`` add-on's
per-collection endpoint), so replaying the operator's Vault-capable
bearer to it let a ``tenant_admin`` capture that credential (a
credential-capture + SSRF path). Two controls close it here:

* **Destination screen.** Every corpus dial requires the ``https``
  scheme and screens the resolved host through the shared target SSRF
  guard (:func:`~meho_backplane.targets.ssrf_guard.assert_public_destination_async`),
  so a corpus URL cannot be pointed at ``http://``, loopback, RFC 1918
  space, or ``169.254.169.254``. On-prem corpora on private space are
  opted back in via ``MEHO_TARGET_SSRF_ALLOWLIST`` (the same allowlist
  the connector target-dial guard reads).
* **Configured downstream credential.** The bearer presented to the
  corpus is ``settings.corpus_service_token`` — a dedicated, corpus-scoped
  service credential owned by the deployment, not the operator's inbound
  bearer. An empty setting sends no ``Authorization`` header (a corpus
  that requires auth then fails closed) rather than falling back to the
  operator JWT.

The tradeoff of no longer forwarding the operator JWT: the corpus's own
audit log now attributes the call to the service principal, not the
operator. MEHO's central ``audit_log`` (bound from the JWT the operator
presented to the backplane) is unaffected.

Fail-closed by construction. The corpus being unconfigured
(``corpus_url`` unset), unreachable (network / timeout), or returning a
non-2xx status all collapse to one typed :class:`CorpusUnavailable`,
which the consuming ``search_docs`` route (T3, #1521) maps to HTTP 503 —
never a silent empty result. This mirrors the
``LlmClientUnavailable`` → 503 precedent (#1386).

The corpus request/response contract is a **consumer-side** dependency
(the corpus is owned elsewhere), so it is modelled behind a small typed
Pydantic adapter (:class:`CorpusChunk` / :class:`CorpusSearchResponse`)
that can be pinned without churning the call sites. The route, the
mandatory REQUIRE_FILTERS posture, and the central audit binding are
**out of scope here** — they land in T3.

G4.6-T2 (#1551) re-homes this transport behind the backend-agnostic
search router: :class:`~meho_backplane.docs_search.backends.corpus_http.CorpusHttpBackend`
wraps :func:`search_corpus` as the first
:class:`~meho_backplane.docs_search.backends.base.SearchBackend` adapter,
passing a per-collection ``corpus_url`` / ``audience`` resolved from the
collection's ``backend.ref``. The optional ``corpus_url`` / ``audience``
overrides on :func:`search_corpus` are the seam for that — ``None`` keeps
the legacy global-settings behaviour for an unmigrated single-collection
deploy. The wire adapters (:class:`CorpusChunk` /
:class:`CorpusSearchResponse`) and the one typed
:class:`CorpusUnavailable` stay here, imported by the adapter and every
other consumer.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Literal, get_args
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import Settings, get_settings
from meho_backplane.targets.ssrf_guard import (
    TargetDestinationBlockedError,
    assert_public_destination_async,
)

__all__ = [
    "READ_CALLER_HEADER",
    "CorpusAnswerError",
    "CorpusChunk",
    "CorpusEndpointBlockedError",
    "CorpusReadError",
    "CorpusSearchResponse",
    "CorpusStatusResponse",
    "CorpusUnavailable",
    "ReadDisclosure",
    "ReadMode",
    "ReadReason",
    "ScoreKind",
    "UpstreamAnswer",
    "UpstreamAnswerTiming",
    "UpstreamCitation",
    "UpstreamRead",
    "ask_corpus",
    "corpus_endpoint_host",
    "corpus_status",
    "derive_answer_url",
    "derive_read_url",
    "derive_status_url",
    "opaque_token_or_none",
    "read_caller_id",
    "read_corpus",
    "search_corpus",
    "web_link_or_none",
]

_log = structlog.get_logger(__name__)


class CorpusUnavailable(RuntimeError):  # noqa: N818 -- "Unavailable" reads better than "Error" in the 503 detail
    """Raised when the external corpus cannot serve a search request.

    One typed error for every fail-closed branch — unconfigured,
    unreachable, or a non-2xx response — so the consuming ``search_docs``
    route (T3, #1521) maps it onto HTTP 503 without branching on the
    cause. ``status`` carries the upstream HTTP status code when the
    failure was a non-2xx corpus response (``None`` for an unconfigured
    or unreachable corpus) so callers can log the cause without parsing
    the message; the raw response body is **never** attached, so a
    corpus error page cannot leak through the 503.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class CorpusAnswerError(RuntimeError):
    """The corpus answer endpoint responded, but not with a usable answer (#3911).

    :func:`ask_corpus` raises this for every outcome where the endpoint
    *responded*: a non-2xx status, or a 2xx body that does not parse. The
    answer seam can then name *how* it failed instead of collapsing every
    cause into one "unavailable". A transport failure (unreachable, timeout,
    an unconfigured or blocked endpoint) stays :class:`CorpusUnavailable`,
    exactly as on the search path.

    ``kind`` is one of the ``KIND_*`` constants; ``status`` is the upstream
    HTTP status; ``retry_after`` is the upstream ``Retry-After`` in whole
    seconds when a rate-limited answer sent one. The response body is
    **never** attached, so an upstream error page cannot leak through the
    error envelope.
    """

    #: A 4xx (or any other non-2xx below 500): the endpoint rejected the
    #: request. A contract or configuration fault, not an outage.
    KIND_REJECTED: Final[str] = "rejected"
    #: A 503 carrying the backend's ``llm_unavailable`` code: the backend has
    #: no answer model.
    KIND_ANSWER_UNAVAILABLE: Final[str] = "answer_unavailable"
    #: A 503 carrying the backend's ``llm_rate_limited`` code, or a 429: the
    #: backend's answer model is throttled. Retryable.
    KIND_RATE_LIMITED: Final[str] = "rate_limited"
    #: Any other 5xx.
    KIND_SERVER_ERROR: Final[str] = "server_error"
    #: A 2xx whose body is not JSON or does not match :class:`UpstreamAnswer`.
    KIND_MALFORMED: Final[str] = "malformed"

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int,
        retry_after: int | None = None,
    ) -> None:
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


class CorpusEndpointBlockedError(TargetDestinationBlockedError):
    """A corpus endpoint URL is not an allowed ``https`` public destination.

    Subclasses :class:`~meho_backplane.targets.ssrf_guard.TargetDestinationBlockedError`
    (itself a :class:`ValueError`) so a single ``except`` at each
    enforcement point catches both the scheme rejection raised here and the
    non-public-address rejection the shared SSRF host guard raises — the
    create path renders one 422, the dial path one :class:`CorpusUnavailable`.
    """


def corpus_endpoint_host(url: str) -> str:
    """Return the host to SSRF-screen for a corpus *url*, requiring ``https``.

    Parses *url* and returns the host component the transport will dial.
    Rejects — with :class:`CorpusEndpointBlockedError` — any URL whose
    scheme is not ``https`` or that names no host, so a corpus endpoint can
    never be a plaintext (`http://`) or structurally-degenerate destination.
    The returned host is what the caller passes to
    :func:`~meho_backplane.targets.ssrf_guard.assert_public_destination_async`;
    keeping the parse here (not in the guard) leaves the guard a
    host-screening primitive shared verbatim with the connector target dial.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme != "https" or not host:
        raise CorpusEndpointBlockedError("corpus endpoint must be an https:// URL naming a host")
    return host


async def _screen_corpus_dial(url: str) -> None:
    """Fail closed unless *url* is an ``https`` public corpus destination.

    The connect-time enforcement point for both the search and the
    readiness dial: requires ``https`` + a public host (allowlist-aware)
    and maps any rejection onto :class:`CorpusUnavailable` so the consuming
    route renders a 503. The message never echoes the resolved address (no
    internal-topology oracle — the guard's posture).
    """
    try:
        host = corpus_endpoint_host(url)
        await assert_public_destination_async(host)
    except TargetDestinationBlockedError as exc:
        _log.warning("corpus_endpoint_blocked")
        raise CorpusUnavailable(
            "corpus endpoint is not an allowed https public destination"
        ) from exc


def _corpus_auth_headers(settings: Settings) -> dict[str, str]:
    """Build the corpus ``Authorization`` header from the configured token.

    Presents ``settings.corpus_service_token`` as a Bearer credential — the
    deployment-owned, corpus-scoped service token, never the caller's
    operator JWT (#290). An empty setting sends **no** header, so a corpus
    that requires auth fails closed (401 → :class:`CorpusUnavailable`)
    rather than the operator bearer being forwarded to it.
    """
    token = settings.corpus_service_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def _parse_2xx_body[ModelT: BaseModel](
    response: httpx.Response,
    model: type[ModelT],
    *,
    event_prefix: str,
) -> ModelT:
    """Decode a 2xx corpus response body and validate it against *model*.

    Both fail-closed branches map to one :class:`CorpusUnavailable` so the
    consuming route renders a single 503: a non-JSON body and a body that
    does not match the model (a dropped/renamed consumed field, or — for
    :class:`CorpusSearchResponse` — an unrecognised envelope that names
    neither ``chunks`` nor ``results``, #1732). Neither the raw body nor
    the validation error detail is echoed, so a corpus error page or a
    leaky field value cannot ride out through the 503. *event_prefix*
    namespaces the structlog event (``corpus`` vs ``corpus_status``).
    """
    try:
        body: Any = response.json()
    except ValueError as exc:
        _log.warning(f"{event_prefix}_response_not_json")
        raise CorpusUnavailable("corpus returned a non-JSON body") from exc

    try:
        return model.model_validate(body)
    except ValueError as exc:
        _log.warning(f"{event_prefix}_response_invalid_schema")
        raise CorpusUnavailable("corpus response did not match the expected schema") from exc


#: The direction of a hit's ``score``, as the corpus names it: ``similarity``
#: (higher is better) or ``distance`` (lower is better).
ScoreKind = Literal["similarity", "distance"]

_SCORE_KINDS: Final[frozenset[str]] = frozenset(get_args(ScoreKind))

#: Longest ``upstream_url`` kept. A longer value is not a link a person
#: follows, so it is dropped as unusable.
_UPSTREAM_URL_MAX: Final[int] = 2048

#: Highest ``upstream_page`` kept. No real document has more pages; a larger
#: number is unusable, and a huge one would not fit the CLI's integer type
#: and would break decoding the whole response there.
_UPSTREAM_PAGE_MAX: Final[int] = 1_000_000


def web_link_or_none(value: object) -> str | None:
    """Return *value* stripped when it is a usable ``http(s)`` link, else ``None``.

    ``upstream_url`` is shown to people and agents as a link, so only an
    absolute ``http`` / ``https`` URL that names a host is kept. Anything
    else reads as absent: another scheme (``javascript:``, ``gs://``), no
    host, a space, a control or bidi character (``str.isprintable`` is false
    for both), more than :data:`_UPSTREAM_URL_MAX` characters, a URL
    ``urlsplit`` cannot parse (for example a broken ``[`` IPv6 host), or a
    value that is not a string. It never raises.

    The one rule for an ``upstream_url``: the corpus parse
    (:class:`CorpusChunk`) and the citation reference
    (:func:`~meho_backplane.docs_search.citation_links.normalize_source_ref`)
    both use it.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _UPSTREAM_URL_MAX or " " in url or not url.isprintable():
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


#: Longest opaque token kept: a read handle or a read cursor (#3948). A real
#: one is a few hundred characters; a longer value is not one MEHO passes on.
_OPAQUE_TOKEN_MAX: Final[int] = 8192


def opaque_token_or_none(value: object) -> str | None:
    """Return *value* when it is a usable opaque token, else ``None``.

    A read handle and a read cursor (#3948) are opaque strings the backend
    signs. MEHO never looks inside them; it only passes them back. So the
    check is about shape, not meaning: a non-empty ASCII string with no
    space, no control character and at most :data:`_OPAQUE_TOKEN_MAX`
    characters. Anything else (another type, a blank, a non-ASCII or an
    over-long string) reads as absent. It never raises.

    A token is as sensitive as the hit text it points to, so it is never
    logged: a log line names the chunk id instead.
    """
    if not isinstance(value, str):
        return None
    if not value or len(value) > _OPAQUE_TOKEN_MAX:
        return None
    if not value.isascii() or not value.isprintable() or any(ch.isspace() for ch in value):
        return None
    return value


class CorpusChunk(BaseModel):
    """One cited chunk returned by the external corpus.

    A deliberately small, frozen adapter over the corpus's response
    contract. The corpus is owned by the ops team, so its wire shape can
    drift; pinning the fields MEHO actually consumes here means a corpus
    change that adds fields is absorbed silently (``extra="ignore"``)
    while a change that drops a consumed field fails loudly at parse
    time — surfaced as :class:`CorpusUnavailable` rather than a partial
    result. ``metadata`` keeps the corpus's per-chunk attributes (e.g.
    ``product`` / ``version`` the T3 filters key off) without MEHO
    having to model every one.

    The text and source-link fields each accept **two** wire names via a
    validation alias (#1732): MEHO.Knowledge's ``/search`` returns
    ``text`` / ``source_uri`` while an earlier corpus shape (and MEHO's
    own internal projection) uses ``content`` / ``source_url``. Accepting
    both keeps the one consumed name (``content`` / ``source_url``) stable
    for downstream callers regardless of which wire dialect the corpus
    speaks. ``populate_by_name=True`` keeps the internal name usable when
    constructing the model directly in tests.

    ``document_id`` is modelled ``str | None`` (#2004). The contract names
    the field ``document_id`` — MEHO.Knowledge speaks that exact key, so
    there is no second wire name to alias (the #1732 fix aliased the three
    fields that *did* drift; ``document_id`` is not one of them). What it
    can be is **absent**: MEHO.Knowledge returns ``document_id: ""`` when a
    chunk has no owning-document concept. ``document_id`` is only ever read
    as a *citation-label fallback* (``title -> document_id -> filename ->
    URL`` in :func:`~meho_backplane.docs_search.citation_links._label_for`)
    and is never used to resolve or ground a citation, so a blank value is
    not a contract breach — but carrying it as a required ``""`` is a lie.
    The validator below normalises blank-after-strip to ``None`` so the
    absence is honestly typed and the label chain skips a cleanly-``None``
    rung rather than a misleading empty string.

    ``title`` is an **optional** human-legible chunk title (#2475). The corpus
    is the only place a title can originate — MEHO has no doc-ingest path to
    derive one (federation-only by design, #1864 -> #2049). It is read as the
    *preferred* citation label (``title -> document_id -> humanised filename ->
    URL`` in :func:`~meho_backplane.docs_search.citation_links._label_for`),
    which is starved today because no title survives the corpus projection. A
    title can arrive top-level (``title``) or nested under the per-chunk
    ``metadata`` (``metadata["title"]``); the top-level key wins, the metadata
    key is the fallback. Blank-after-strip (or absent) normalises to ``None``
    (the #2004 pattern) so the label chain skips a cleanly-``None`` rung.

    The **page identity** (#3913) is what a citation title is derived from when
    the corpus sends no ``title``
    (:func:`~meho_backplane.docs_search.citation_links.derive_chunk_title`):
    ``filename`` (the source file's name), ``breadcrumb`` (the page's place in
    its document tree, ``>``-separated) and ``heading_path`` (the headings
    above the chunk, outermost first). ``score_kind`` names the direction of
    ``score``: ``distance`` means lower is better, ``similarity`` higher is
    better.

    Two optional **link** fields name the public source of the hit:
    ``upstream_url`` (an ``http(s)`` link to the source page or document; for
    a PDF with a known page it already ends in ``#page=N``) and
    ``upstream_page`` (that page, counted in the whole source document). The
    projection prefers ``upstream_url`` over an opaque ``meho://`` reference
    when the hit's own ``source_url`` is not an ``https`` link.

    All six are optional, and an **unusable value reads as absent** rather
    than failing the parse: they label and link a hit, they never decide
    whether it is grounded. So a non-string ``filename`` / ``breadcrumb`` is
    ``""``, ``heading_path`` keeps only its string items, an unknown
    ``score_kind`` is ``None``, ``upstream_url`` must be an ``http(s)`` URL
    with a host (see :func:`web_link_or_none`), and ``upstream_page`` must be
    a whole number from 1 to :data:`_UPSTREAM_PAGE_MAX`.

    ``read_handle`` (#3948) is the backend's signed, expiring handle for
    reading more around the hit (``read_docs``). It is opaque: MEHO passes it
    back unchanged and never logs it, because it carries a few words of the
    hit. A value that is not a usable token (see :func:`opaque_token_or_none`)
    reads as absent; ``None`` when the backend sends none (a PDF hit, or a
    backend without a read endpoint).
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    chunk_id: str
    document_id: str | None = None
    title: str | None = None
    content: str = Field(validation_alias=AliasChoices("content", "text"))
    source_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("source_url", "source_uri"),
    )
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    filename: str = ""
    breadcrumb: str = ""
    heading_path: list[str] = Field(default_factory=list)
    score_kind: ScoreKind | None = None
    upstream_url: str | None = None
    upstream_page: int | None = None
    read_handle: str | None = None

    @field_validator("filename", "breadcrumb", mode="before")
    @classmethod
    def _text_or_empty(cls, value: object) -> object:
        """A missing or non-string ``filename`` / ``breadcrumb`` is ``""``."""
        return value if isinstance(value, str) else ""

    @field_validator("heading_path", mode="before")
    @classmethod
    def _string_headings(cls, value: object) -> object:
        """Keep the string headings of a list; anything else is ``[]``."""
        if not isinstance(value, list | tuple):
            return []
        return [heading for heading in value if isinstance(heading, str)]

    @field_validator("score_kind", mode="before")
    @classmethod
    def _known_score_kind(cls, value: object) -> object:
        """An unknown ``score_kind`` is ``None``, never a guessed direction."""
        return value if isinstance(value, str) and value in _SCORE_KINDS else None

    @field_validator("upstream_url", mode="before")
    @classmethod
    def _usable_upstream_url(cls, value: object) -> object:
        """Keep ``upstream_url`` only when it is a usable ``http(s)`` link."""
        return web_link_or_none(value)

    @field_validator("read_handle", mode="before")
    @classmethod
    def _usable_read_handle(cls, value: object) -> object:
        """Keep ``read_handle`` only when it is a usable opaque token (#3948)."""
        return opaque_token_or_none(value)

    @field_validator("upstream_page", mode="before")
    @classmethod
    def _positive_page(cls, value: object) -> object:
        """Keep ``upstream_page`` only when it is a whole number from 1 to the cap.

        A number above :data:`_UPSTREAM_PAGE_MAX` reads as absent like any
        other unusable value: it is no real page, and a huge one would break
        the CLI's decoding of the whole response.
        """
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= _UPSTREAM_PAGE_MAX
        ):
            return value
        return None

    @field_validator("document_id", mode="before")
    @classmethod
    def _blank_document_id_to_none(cls, value: object) -> object:
        """Normalise a blank ``document_id`` to ``None`` (#2004).

        MEHO.Knowledge returns ``document_id: ""`` for a chunk with no
        owning-document concept. A required ``""`` would validate but lie;
        ``Optional`` alone keeps the empty string. Mapping blank-after-strip
        to ``None`` here makes the absence honest, so the citation-label
        fallback skips a cleanly-``None`` rung.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _resolve_title(cls, data: object) -> object:
        """Resolve the optional chunk title, top-level or from metadata (#2475).

        A human-legible title can arrive as a top-level ``title`` or nested
        under the per-chunk ``metadata`` (``metadata["title"]``). The
        top-level key wins; the metadata key is the fallback. Blank-after-strip
        (or absent) normalises to ``None`` so the citation-label chain skips a
        cleanly-``None`` rung rather than preferring an empty string. Runs
        pre-validation because the metadata fallback needs to see a sibling
        field a per-field validator cannot reach.
        """
        if not isinstance(data, dict):
            return data
        title = data.get("title")
        if not (isinstance(title, str) and title.strip()):
            metadata = data.get("metadata")
            title = metadata.get("title") if isinstance(metadata, dict) else None
        resolved = title.strip() if isinstance(title, str) and title.strip() else None
        return {**data, "title": resolved}


class CorpusSearchResponse(BaseModel):
    """Parsed corpus search result: the cited chunks for a query.

    Frozen adapter; ``chunks`` is the ordered hit list (best first, as
    the corpus ranks them). The consuming route (T3) projects these into
    MEHO's own cited-chunk surface and binds the central audit row.

    The hit list accepts **two** envelope names via a validation alias
    (#1732): MEHO.Knowledge's ``/search`` returns ``{"results": [...]}``
    while an earlier corpus shape (and MEHO's own internal projection)
    uses ``{"chunks": [...]}``. The field is **required** — it carries no
    default — so a 2xx body that names *neither* envelope fails parse
    loudly (surfaced as :class:`CorpusUnavailable`) rather than silently
    validating to an empty hit list. That fail-loud posture is the whole
    point of #1732: a populated corpus returning an unrecognised envelope
    must not read back as "zero hits". ``populate_by_name=True`` keeps the
    internal ``chunks`` name usable when constructing the model directly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    chunks: list[CorpusChunk] = Field(validation_alias=AliasChoices("chunks", "results"))


async def search_corpus(
    operator: Operator,
    query: str,
    *,
    metadata_filters: dict[str, Any] | None = None,
    soft_scope: Mapping[str, str] | None = None,
    limit: int = 10,
    corpus_url: str | None = None,
    audience: str | None = None,
) -> CorpusSearchResponse:
    """Search the external corpus, returning cited chunks.

    Screens *corpus_url* (``https`` + a public host, allowlist-aware) and
    POSTs a JSON search request to it carrying the deployment-configured
    corpus service credential (``settings.corpus_service_token``) — the
    caller's raw operator JWT is **never** forwarded (#290). The request is
    bounded by ``settings.corpus_timeout_seconds`` across connect / read
    / write so a slow or hung corpus raises rather than blocking the
    event loop. *audience* (RFC 8707), when set, is forwarded as the
    requested resource indicator; the corpus may use it to bind the
    token to itself.

    Args:
        operator: The verified operator. Retained for the
            :class:`~meho_backplane.docs_search.backends.base.SearchBackend`
            seam and central-audit context; it is **not** used to
            authenticate to the corpus (the operator JWT is no longer
            forwarded, #290).
        query: The free-text search query.
        metadata_filters: Optional binary ``{key: scalar}`` narrowing
            (e.g. ``{"product": "vsphere", "version": "8.0"}``). The
            mandatory product/version REQUIRE_FILTERS posture is enforced
            by the consuming route (T3, #1521), **not** here — this
            transport forwards whatever filters it is given.
        soft_scope: Optional soft scope sent as the request's ``scope``
            object (#3912), e.g. ``{"product": "vsphere", "version":
            "8.0.3", "source": "caller"}``: the corpus ranks with it and
            never filters on it. Omitted from the body when ``None`` or
            empty, so a corpus that refuses unknown request keys gets the
            body it always got.
        limit: Maximum number of chunks to request.
        corpus_url: The corpus search endpoint. ``None`` falls back to
            ``settings.corpus_url`` — the single-collection deploy that
            predates the per-collection backend router (T2 #1551). The
            ``corpus-http`` backend adapter passes the collection's
            ``backend.ref`` endpoint here so each collection can federate
            to its own corpus.
        audience: The RFC 8707 resource indicator to forward. ``None``
            falls back to ``settings.corpus_audience``; an empty string
            forwards no audience.

    Raises:
        CorpusUnavailable: when *corpus_url* is unset (unconfigured), is
            not an allowed ``https`` public destination, the corpus is
            unreachable / times out, or it returns a non-2xx status. The
            upstream status is carried on the exception (``status``) for
            non-2xx responses; the raw response body is never included.
    """
    settings = get_settings()
    resolved_url = corpus_url if corpus_url is not None else settings.corpus_url
    if not resolved_url:
        # Fail-closed: an unconfigured corpus is unavailable, not empty.
        raise CorpusUnavailable("corpus_url is not configured")
    await _screen_corpus_dial(resolved_url)
    resolved_audience = audience if audience is not None else settings.corpus_audience

    # MEHO.Knowledge's ``/search`` reads ``top_k`` for the hit cap and
    # silently ignores ``limit`` (#1732) — sending ``limit`` let the
    # corpus fall back to its server-side default. Send the key the corpus
    # actually honours so ``limit`` reaches it. No ``with_rerank`` is sent:
    # how hits are ranked is the corpus's decision, not the backplane's.
    payload: dict[str, Any] = {"query": query, "top_k": limit}
    if metadata_filters:
        payload["metadata_filters"] = metadata_filters
    if soft_scope:
        payload["scope"] = dict(soft_scope)
    if resolved_audience:
        payload["audience"] = resolved_audience

    headers = _corpus_auth_headers(settings)
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(resolved_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # ConnectError / TimeoutException / any transport failure — the
        # corpus is unreachable. Log the cause by type (never the service
        # credential or the query body) and fail closed.
        _log.warning("corpus_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        # Non-2xx: surface the status for observability, but never the
        # response body — a corpus error page must not leak through the
        # 503 the route renders.
        _log.warning("corpus_request_failed", status=response.status_code)
        raise CorpusUnavailable(
            f"corpus returned HTTP {response.status_code}",
            status=response.status_code,
        )

    return _parse_2xx_body(response, CorpusSearchResponse, event_prefix="corpus")


class UpstreamCitation(BaseModel):
    """One citation of an upstream answer: which hit backs which ``[N]`` marker.

    ``chunk_index`` is the 0-based position of the cited chunk in the list
    the backend's answer model saw -- the ``N`` of the ``[N]`` markers in the
    answer text. It is **not** a position in the returned ``hits`` (those come
    back reordered to citation order), so a citation is matched to its hit by
    ``chunk_id`` only. The quote and the per-citation page and link fields
    (``upstream_url`` / ``upstream_page`` included) are not consumed: the
    cited hit carries the same values.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    chunk_index: int
    chunk_id: str


class UpstreamAnswerTiming(BaseModel):
    """The backend's own timing for one answer, logged (never returned)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    total_ms: float | None = None
    llm_ms: float | None = None


class UpstreamAnswer(BaseModel):
    """Parsed upstream grounded answer (``POST /ask?include=hits``, #3911).

    The consumer-side adapter over the corpus's answer endpoint, consumed
    only for a collection that opts in (``backend.ref["answer"] ==
    "upstream"``). Pins the fields the backplane maps: the ``answer`` text
    with its ``[N]`` markers, the ``citations`` (``chunk_index`` +
    ``chunk_id``), the retrieved ``hits`` (each a :class:`CorpusChunk`: the
    search-hit shape with its page identity and link fields) and the backend
    ``timing``. ``extra="ignore"`` absorbs everything else (the echoed
    ``query``, quotes, page numbers).

    ``hits`` is **required**: the request always asks for ``include=hits``,
    and a citation can only be resolved against the hits. A 2xx body without
    them fails parse loudly (the #1732 posture) instead of reading back as an
    unresolvable answer.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    answer: str = Field(min_length=1)
    citations: list[UpstreamCitation] = Field(default_factory=list)
    hits: list[CorpusChunk]
    timing: UpstreamAnswerTiming = Field(default_factory=UpstreamAnswerTiming)


#: The answer endpoint's machine error codes the transport classifies on (the
#: ``error.code`` of the backend's JSON error body). Only these two are read;
#: any other body is treated as an unclassified error and never echoed.
_UPSTREAM_CODE_ANSWER_UNAVAILABLE: Final[str] = "llm_unavailable"
_UPSTREAM_CODE_RATE_LIMITED: Final[str] = "llm_rate_limited"


def derive_answer_url(search_url: str) -> str:
    """Derive the corpus answer URL from its *search_url* (#3911).

    The answer endpoint sits beside the search endpoint, so its URL is the
    search URL with the **last path segment** replaced by ``ask``
    (``https://corpus/search`` -> ``https://corpus/ask``,
    ``https://corpus/v1/search`` -> ``https://corpus/v1/ask``). A trailing
    slash is ignored, and a search URL with no path maps to ``/ask``. Query
    string and fragment are dropped: they are request shape, not endpoint
    identity. A collection whose answer endpoint lives elsewhere names it
    explicitly in ``backend.ref["answer_endpoint"]``.
    """
    parts = urlsplit(search_url)
    head, _sep, _last = parts.path.rstrip("/").rpartition("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{head}/ask", "", ""))


#: Upper bound, in seconds, on the ``Retry-After`` the transport forwards. An
#: upstream hint above it is clamped to it: the value is passed on verbatim as
#: the REST ``Retry-After`` header and the MCP ``data.retry_after``, and a
#: backend throttle longer than an hour is not a retry hint a caller can act
#: on, so an absurd upstream number never reaches either face.
_RETRY_AFTER_MAX_S: Final[int] = 3600

#: Longest delta-seconds digit run (leading zeros stripped) that is parsed
#: with ``int()``. A longer run is far above :data:`_RETRY_AFTER_MAX_S` and
#: clamps without a parse, so ``int()`` never meets a string near its
#: 4300-digit conversion limit.
_RETRY_AFTER_MAX_DIGITS: Final[int] = 9


def _retry_after_seconds(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header into whole seconds in ``[0, 3600]``.

    Accepts both RFC 9110 forms, which are ASCII by grammar:

    * **delta-seconds** -- ASCII digits only. The value is clamped to
      :data:`_RETRY_AFTER_MAX_S` (3600 s); a digit run too long to matter is
      clamped without being parsed.
    * **HTTP-date** -- converted to the seconds remaining, floored at 0 and
      clamped to :data:`_RETRY_AFTER_MAX_S`.

    Anything else is ``None`` -- a blank or signed value, a non-ASCII header
    (httpx decodes a raw non-ASCII byte as Latin-1, e.g. ``0xB2`` as U+00B2
    SUPERSCRIPT TWO, which ``str.isdigit()`` accepts but ``int()`` rejects),
    or a date that does not parse or overflows -- so a garbled header is
    dropped rather than forwarded, and never raises out of the error
    classification (a raise there would turn the typed rate-limited error
    into an unclassified 500 / ``-32603``).
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw or not raw.isascii():
        return None
    if raw.isdigit():
        digits = raw.lstrip("0") or "0"
        if len(digits) > _RETRY_AFTER_MAX_DIGITS:
            return _RETRY_AFTER_MAX_S
        return min(int(digits), _RETRY_AFTER_MAX_S)
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            return None
        remaining = (when - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return min(_RETRY_AFTER_MAX_S, max(0, int(remaining)))


def _upstream_error_code(response: httpx.Response) -> str | None:
    """Return the backend's ``error.code`` when it is one the transport reads.

    The answer endpoint renders an error as ``{"error": {"code": ...}}``. Only
    the two codes the error table classifies on are returned; anything else
    (a proxy error page, another code, a non-JSON body) is ``None``, so no
    upstream-chosen string reaches a log or an error envelope.
    """
    try:
        body: Any = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code in (_UPSTREAM_CODE_ANSWER_UNAVAILABLE, _UPSTREAM_CODE_RATE_LIMITED):
        return str(code)
    return None


def _classify_answer_failure(response: httpx.Response) -> CorpusAnswerError:
    """Map a non-2xx answer response onto a typed :class:`CorpusAnswerError`.

    * 429, or a 503 with the ``llm_rate_limited`` code -> rate limited, with
      the upstream ``Retry-After`` carried along;
    * a 503 with the ``llm_unavailable`` code -> no answer model;
    * any other 5xx -> server error;
    * everything else (4xx, and the unexpected 1xx / 3xx) -> rejected.
    """
    status = response.status_code
    code = _upstream_error_code(response) if status == 503 else None
    if status == 429 or code == _UPSTREAM_CODE_RATE_LIMITED:
        return CorpusAnswerError(
            f"corpus answer model is rate limited (HTTP {status})",
            kind=CorpusAnswerError.KIND_RATE_LIMITED,
            status=status,
            retry_after=_retry_after_seconds(response.headers.get("retry-after")),
        )
    if code == _UPSTREAM_CODE_ANSWER_UNAVAILABLE:
        return CorpusAnswerError(
            f"corpus answer endpoint has no answer model (HTTP {status})",
            kind=CorpusAnswerError.KIND_ANSWER_UNAVAILABLE,
            status=status,
        )
    if status >= 500:
        return CorpusAnswerError(
            f"corpus answer endpoint returned HTTP {status}",
            kind=CorpusAnswerError.KIND_SERVER_ERROR,
            status=status,
        )
    return CorpusAnswerError(
        f"corpus answer endpoint rejected the request (HTTP {status})",
        kind=CorpusAnswerError.KIND_REJECTED,
        status=status,
    )


async def ask_corpus(
    operator: Operator,
    query: str,
    *,
    filters: Mapping[str, Any] | None = None,
    soft_scope: Mapping[str, str] | None = None,
    limit: int = 10,
    answer_url: str | None,
    audience: str | None = None,
) -> UpstreamAnswer:
    """Ask the corpus's grounded-answer endpoint, with its retrieved hits (#3911).

    The answer-side sibling of :func:`search_corpus`, with the same transport
    posture: the dial is screened (``https`` + a public host,
    allowlist-aware), the deployment's corpus service credential is presented
    (the operator JWT is **never** forwarded, #290), and the response body is
    never echoed. It POSTs ``{query, top_k}`` (plus ``audience`` when set) to
    *answer_url* with ``?include=hits``.

    The product / version refinements ride the body only when the caller
    passes them, and the caller passes them only for a collection that opts
    in (:func:`~meho_backplane.docs_search.forwarded_scope`, #3912): *filters*
    as the answer endpoint's ``filters`` object, *soft_scope* as its
    ``scope`` object. Each key is omitted when ``None`` or empty, so with
    both gates off the body is exactly ``{query, top_k}`` (+ ``audience``),
    which a backend that refuses unknown request keys needs. Deliberately
    never sent: ``with_rerank`` (ranking policy belongs to the backend).

    The request has its own bound, ``settings.corpus_answer_timeout_seconds``
    (default 60): a grounded answer runs retrieval plus one or more model
    calls, so the 10 s search bound would cut it off.

    Args:
        operator: The verified operator. Kept for the backend seam and audit
            context; never used to authenticate to the corpus.
        query: The operator's question.
        filters: Optional hard ``{key: scalar}`` filters, sent as the body's
            ``filters`` (the answer endpoint's name for the search side's
            ``metadata_filters``), e.g. ``{"product": "vsphere", "version":
            "8.0"}``. Forwarded as given.
        soft_scope: Optional soft scope sent as the body's ``scope`` object,
            e.g. ``{"product": "vsphere", "version": "8.0.3", "source":
            "caller"}``: the backend ranks with it and labels its answer,
            never filters on it.
        limit: The retrieval depth to request (``top_k``). The backend may
            cap it lower.
        answer_url: The answer endpoint. ``None`` / empty is unconfigured.
        audience: The RFC 8707 resource indicator. ``None`` falls back to
            ``settings.corpus_audience``; an empty string sends none.

    Raises:
        CorpusUnavailable: the endpoint is unconfigured, not an allowed
            ``https`` public destination, unreachable, or timed out.
        CorpusAnswerError: the endpoint responded with a non-2xx status or a
            2xx body that does not match :class:`UpstreamAnswer` (``kind``
            names which).
    """
    settings = get_settings()
    if not answer_url:
        raise CorpusUnavailable("corpus answer endpoint is not configured")
    await _screen_corpus_dial(answer_url)
    resolved_audience = audience if audience is not None else settings.corpus_audience

    payload: dict[str, Any] = {"query": query, "top_k": limit}
    if filters:
        payload["filters"] = dict(filters)
    if soft_scope:
        payload["scope"] = dict(soft_scope)
    if resolved_audience:
        payload["audience"] = resolved_audience

    headers = _corpus_auth_headers(settings)
    timeout = httpx.Timeout(settings.corpus_answer_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                answer_url,
                json=payload,
                headers=headers,
                params={"include": "hits"},
            )
    except httpx.HTTPError as exc:
        _log.warning("corpus_answer_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        failure = _classify_answer_failure(response)
        _log.warning(
            "corpus_answer_request_failed",
            status=response.status_code,
            kind=failure.kind,
        )
        raise failure

    try:
        body: Any = response.json()
    except ValueError as exc:
        _log.warning("corpus_answer_response_not_json")
        raise CorpusAnswerError(
            "corpus answer endpoint returned a non-JSON body",
            kind=CorpusAnswerError.KIND_MALFORMED,
            status=response.status_code,
        ) from exc
    try:
        return UpstreamAnswer.model_validate(body)
    except ValueError as exc:
        _log.warning("corpus_answer_response_invalid_schema")
        raise CorpusAnswerError(
            "corpus answer response did not match the expected schema",
            kind=CorpusAnswerError.KIND_MALFORMED,
            status=response.status_code,
        ) from exc


# ---------------------------------------------------------------------------
# Read around a hit (#3948)
# ---------------------------------------------------------------------------

#: How much ``read_corpus`` asks for: the text ``around`` the hit (a window of
#: chunks before and after it), the whole ``page``, or the ``section`` the hit
#: sits in.
ReadMode = Literal["around", "page", "section"]

#: Whether a read may return text: ``full`` (yes) or ``link`` (the file's owner
#: allows only its link, so ``text`` is ``None``).
ReadDisclosure = Literal["full", "link"]

#: Why a read returned no text: the file is ``link_only``, or the backend cannot
#: read its type (``pdf_not_supported`` / ``type_not_supported``).
ReadReason = Literal["link_only", "pdf_not_supported", "type_not_supported"]

_READ_MODES: Final[frozenset[str]] = frozenset(get_args(ReadMode))
_READ_REASONS: Final[frozenset[str]] = frozenset(get_args(ReadReason))

#: The request header that names the caller to the backend's read endpoint, so
#: the backend can limit reads per person. The value is :func:`read_caller_id`:
#: a keyed hash of the operator's subject, never the subject itself. The backend
#: reads it through its own caller-header setting.
READ_CALLER_HEADER: Final[str] = "X-Meho-Caller"

#: Domain label mixed into the caller hash, so it can never equal another HMAC
#: the same key makes (the CSRF token, the run hand-off token).
_READ_CALLER_DOMAIN: Final[bytes] = b"meho-docs-read-caller-v1\x00"

#: Hex characters of the caller hash that are sent (128 bits).
_READ_CALLER_HEX_LEN: Final[int] = 32


def read_caller_id(operator: Operator) -> str:
    """Return the caller id the read call sends in :data:`READ_CALLER_HEADER`.

    A keyed hash (HMAC-SHA256) of the operator's subject, as 32 hex
    characters. It is stable for one person, so the backend can limit reads
    per person, and it cannot be turned back into the subject without the
    key. The raw subject is never sent.

    The key is the deployment's ``UI_SESSION_ENCRYPTION_KEY`` with its own
    domain label, the same key the CSRF and run hand-off tokens use. It never
    leaves the backplane. Rotating that key changes every caller id, which
    only resets the backend's read counters.

    Raises:
        CorpusUnavailable: the key is not set. A read then fails closed
            instead of sending an unkeyed (reversible) hash or no caller at
            all, which would put every caller behind one shared limit.
    """
    key = get_settings().ui_session_encryption_key
    if not key:
        _log.warning("corpus_read_caller_key_missing")
        raise CorpusUnavailable("docs read is not configured on this backplane")
    mac = hmac.new(
        key.encode("utf-8"),
        _READ_CALLER_DOMAIN + operator.sub.encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()[:_READ_CALLER_HEX_LEN]


class CorpusReadError(RuntimeError):
    """The corpus read endpoint answered with a refusal the caller can act on (#3948).

    :func:`read_corpus` raises this for three answers only. Every other
    failure (unconfigured, unreachable, a 5xx, another 4xx, a malformed 2xx)
    is :class:`CorpusUnavailable`, exactly as on search.

    * ``not_found`` (HTTP 404): the backend gives one answer for every refusal
      (a bad or foreign handle, a file it may not read, a filter mismatch).
    * ``search_again`` (HTTP 409): the handle is too old, the file changed or
      the cursor went too far. A new search gives a new handle.
    * ``rate_limited`` (HTTP 429): this caller read too much. ``retry_after``
      is the backend's ``Retry-After`` in whole seconds, when it sent a usable
      one.

    The response body is never attached, and neither is the handle.
    """

    KIND_NOT_FOUND: Final[str] = "not_found"
    KIND_SEARCH_AGAIN: Final[str] = "search_again"
    KIND_RATE_LIMITED: Final[str] = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int,
        retry_after: int | None = None,
    ) -> None:
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


class UpstreamRead(BaseModel):
    """Parsed answer of the corpus read endpoint (``POST /read``, #3948).

    The consumer-side adapter over the backend's read reply. ``extra="ignore"``
    absorbs fields MEHO does not use.

    * ``mode`` echoes the request (``around`` / ``page`` / ``section``); an
      unknown value fails the parse.
    * ``text`` is the text read, or ``None``. It is forced to ``None`` unless
      ``disclosure`` is ``full``: a link-only file never returns text through
      MEHO, even if a backend sent some.
    * ``disclosure`` is ``full`` or ``link``. Any other or missing value reads
      as ``link``, the safe side.
    * ``reason`` says why there is no text (``link_only``,
      ``pdf_not_supported``, ``type_not_supported``); an unknown value is
      ``None``.
    * ``title``, ``source_uri`` and ``upstream_url`` name the source. A blank
      title is ``None``; ``upstream_url`` must be a usable ``http(s)`` link
      (:func:`web_link_or_none`). ``source_uri`` may be a storage path: the
      service maps it to a public link and never returns it raw.
    * ``located`` is whether the backend found the hit in the file (``None``
      when it does not say). ``truncated`` is whether the reply was cut at the
      backend's size cap.
    * ``next`` / ``up`` are opaque cursors for reading on, or ``None``. A value
      that is not a usable token (:func:`opaque_token_or_none`) is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    mode: ReadMode
    text: str | None = None
    title: str | None = None
    source_uri: str | None = None
    upstream_url: str | None = None
    disclosure: ReadDisclosure = "link"
    reason: ReadReason | None = None
    located: bool | None = None
    truncated: bool = False
    next: str | None = None
    up: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: object) -> object:
        """Read unusable optional values as absent, and never keep link-only text.

        Runs before validation because the text rule needs the sibling
        ``disclosure`` value.
        """
        if not isinstance(data, dict):
            return data
        disclosure = "full" if data.get("disclosure") == "full" else "link"
        text = data.get("text") if disclosure == "full" else None
        title = data.get("title")
        reason = data.get("reason")
        located = data.get("located")
        source_uri = data.get("source_uri")
        return {
            **data,
            "disclosure": disclosure,
            "text": text if isinstance(text, str) else None,
            "title": title.strip() if isinstance(title, str) and title.strip() else None,
            "source_uri": source_uri if isinstance(source_uri, str) else None,
            "upstream_url": web_link_or_none(data.get("upstream_url")),
            "reason": reason if isinstance(reason, str) and reason in _READ_REASONS else None,
            "located": located if isinstance(located, bool) else None,
            "next": opaque_token_or_none(data.get("next")),
            "up": opaque_token_or_none(data.get("up")),
        }


def derive_read_url(search_url: str) -> str:
    """Derive the corpus read URL from its *search_url* (#3948).

    The read endpoint sits beside the search endpoint, so its URL is the search
    URL with the **last path segment** replaced by ``read``
    (``https://corpus/search`` -> ``https://corpus/read``,
    ``https://corpus/v1/search`` -> ``https://corpus/v1/read``). A trailing
    slash is ignored, a search URL with no path maps to ``/read``, and the
    query string and fragment are dropped. A collection whose read endpoint
    lives elsewhere names it in ``backend.ref["read_endpoint"]``.
    """
    parts = urlsplit(search_url)
    head, _sep, _last = parts.path.rstrip("/").rpartition("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{head}/read", "", ""))


def _classify_read_failure(response: httpx.Response) -> CorpusReadError | CorpusUnavailable:
    """Map a non-2xx read response onto the error the caller gets.

    404 -> not found, 409 -> search again, 429 -> rate limited (with the
    bounded ``Retry-After``). Everything else (a 5xx such as the backend's
    ``503 read_unavailable``, a 422 for a request MEHO built wrong, a 401 for
    a bad service credential) is :class:`CorpusUnavailable` with the status.
    """
    status = response.status_code
    if status == 404:
        return CorpusReadError(
            "corpus read endpoint found no readable source",
            kind=CorpusReadError.KIND_NOT_FOUND,
            status=status,
        )
    if status == 409:
        return CorpusReadError(
            "corpus read handle must be renewed by a new search",
            kind=CorpusReadError.KIND_SEARCH_AGAIN,
            status=status,
        )
    if status == 429:
        return CorpusReadError(
            "corpus read endpoint is rate limiting this caller",
            kind=CorpusReadError.KIND_RATE_LIMITED,
            status=status,
            retry_after=_retry_after_seconds(response.headers.get("retry-after")),
        )
    return CorpusUnavailable(f"corpus read endpoint returned HTTP {status}", status=status)


async def read_corpus(
    operator: Operator,
    read_handle: str,
    *,
    mode: ReadMode = "around",
    before: int = 1,
    after: int = 1,
    cursor: str | None = None,
    filters: Mapping[str, Any] | None = None,
    read_url: str | None,
) -> UpstreamRead:
    """Read more around a hit from the corpus's read endpoint (#3948).

    The read-side sibling of :func:`search_corpus`, with the same transport
    posture: the dial is screened (``https`` + a public host,
    allowlist-aware), the deployment's corpus service credential is presented
    (the operator JWT is **never** forwarded), the request is bounded by
    ``settings.corpus_timeout_seconds``, and the response body is never echoed.

    It POSTs ``{read_handle, mode, before, after}`` to *read_url*, plus
    ``cursor`` and ``filters`` only when given. The backend refuses unknown
    request keys, so nothing else is sent (no ``audience``, no ``scope``).
    *filters* must be the hard filters the hit's search sent, because the
    handle is bound to them; the caller passes them only for a collection
    whose scope gates send filters.

    Every call carries :data:`READ_CALLER_HEADER` with :func:`read_caller_id`,
    so the backend can limit reads per person.

    The read handle and the cursor are opaque and as sensitive as the hit
    text: they are sent, never logged and never put on an error.

    Raises:
        CorpusUnavailable: the endpoint is unconfigured, not an allowed
            ``https`` public destination, unreachable, timed out, answered a
            status other than 2xx / 404 / 409 / 429, or sent a 2xx body that
            does not match :class:`UpstreamRead`. Also when the caller key is
            not configured (:func:`read_caller_id`).
        CorpusReadError: the endpoint answered 404, 409 or 429.
    """
    settings = get_settings()
    if not read_url:
        raise CorpusUnavailable("corpus read endpoint is not configured")
    await _screen_corpus_dial(read_url)
    caller = read_caller_id(operator)

    payload: dict[str, Any] = {
        "read_handle": read_handle,
        "mode": mode,
        "before": before,
        "after": after,
    }
    if cursor:
        payload["cursor"] = cursor
    if filters:
        payload["filters"] = dict(filters)

    headers = {**_corpus_auth_headers(settings), READ_CALLER_HEADER: caller}
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(read_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        _log.warning("corpus_read_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        failure = _classify_read_failure(response)
        _log.warning(
            "corpus_read_request_failed",
            status=response.status_code,
            kind=failure.kind if isinstance(failure, CorpusReadError) else "unavailable",
        )
        raise failure

    return _parse_2xx_body(response, UpstreamRead, event_prefix="corpus_read")


class CorpusStatusResponse(BaseModel):
    """Parsed corpus readiness response — the liveness the probe reads back.

    The consumer-side adapter over the corpus's readiness endpoint. The
    probe Task (T6 #1555) reads ``index_built`` / ``doc_count`` /
    ``last_ingested_at`` here and the collection-probe route persists them
    onto the ``doc_collections`` row on success. ``extra="ignore"`` absorbs
    corpus fields MEHO does not consume.

    ``index_built`` is the managed-RAG footgun: ``False`` means the corpus
    is reachable but its ANN index is not yet answerable (a rebuild is in
    flight, or the corpus was registered but never ingested), so the
    search path can fail typed instead of returning an empty 200. Frozen.

    Readiness wire shape (#1732). MEHO.Knowledge has **no** ``/status``
    route; it exposes ``GET /readyz`` returning a ``HealthResponse`` whose
    200 *is* the "ready" signal — it need not carry an ``index_built``
    field. So this adapter:

    * accepts ``index_built`` under any of ``index_built`` / ``ready`` /
      ``index_ready`` (the names a health body may use), and
    * **defaults it to ``True``** — a corpus that answers its readiness
      probe with a 2xx and no explicit readiness flag is treated as
      answerable. A corpus that wants to advertise an in-flight rebuild
      still can, by returning the flag set ``False``.

    ``doc_count`` / ``last_ingested_at`` stay optional liveness, populated
    only when the health body carries them. ``populate_by_name=True`` keeps
    the canonical names usable when constructing the model directly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    index_built: bool = Field(
        default=True,
        validation_alias=AliasChoices("index_built", "ready", "index_ready"),
    )
    doc_count: int | None = None
    last_ingested_at: datetime | None = None


def derive_status_url(search_url: str) -> str:
    """Derive the corpus readiness URL from its *search_url*.

    MEHO.Knowledge exposes its readiness as ``GET /readyz`` at the service
    root (no ``/status`` route, #1732), so the readiness URL is the search
    URL's **host root** plus ``/readyz``
    (``https://corpus/v1/search`` → ``https://corpus/readyz``). Anchoring
    at the root rather than swapping the final path segment matches a
    health endpoint that lives beside the API version prefix, not inside
    it. Query string and fragment are dropped — they are search-request
    shape, never part of the readiness endpoint.
    """
    parts = urlsplit(search_url)
    return urlunsplit((parts.scheme, parts.netloc, "/readyz", "", ""))


async def corpus_status(
    operator: Operator,
    *,
    corpus_url: str | None = None,
    audience: str | None = None,
) -> CorpusStatusResponse:
    """Read the external corpus's readiness (T6 #1555).

    Screens the corpus URL (``https`` + a public host, allowlist-aware),
    then GETs the corpus readiness endpoint (:func:`derive_status_url` of
    the search URL) carrying the deployment-configured corpus service
    credential (``settings.corpus_service_token``) — the caller's operator
    JWT is **never** forwarded (#290), the same downstream-credential
    contract as :func:`search_corpus`. Bounded by
    ``settings.corpus_timeout_seconds``; every fail-closed branch
    collapses to one :class:`CorpusUnavailable` so the probe route never
    persists a partial / stale liveness snapshot.

    Args:
        operator: The verified operator. Retained for the backend-probe
            seam and audit context; not used to authenticate to the corpus
            (the operator JWT is no longer forwarded, #290).
        corpus_url: The corpus *search* endpoint (the readiness URL is
            derived from it). ``None`` falls back to ``settings.corpus_url``
            — the legacy single-collection deploy. The ``corpus-http``
            backend adapter passes the collection's ``backend.ref``
            endpoint so each collection probes its own corpus.
        audience: The RFC 8707 resource indicator to forward as a query
            param. ``None`` falls back to ``settings.corpus_audience``; an
            empty string forwards none.

    Raises:
        CorpusUnavailable: when *corpus_url* is unset (unconfigured), is
            not an allowed ``https`` public destination, the corpus is
            unreachable / times out, returns a non-2xx status, or returns
            a body that does not match :class:`CorpusStatusResponse`. The
            upstream status rides on the exception (``status``) for
            non-2xx; the raw body is never attached.
    """
    settings = get_settings()
    resolved_url = corpus_url if corpus_url is not None else settings.corpus_url
    if not resolved_url:
        # Fail-closed: an unconfigured corpus has no readiness to report.
        raise CorpusUnavailable("corpus_url is not configured")
    await _screen_corpus_dial(resolved_url)
    status_url = derive_status_url(resolved_url)
    resolved_audience = audience if audience is not None else settings.corpus_audience

    params = {"audience": resolved_audience} if resolved_audience else None
    headers = _corpus_auth_headers(settings)
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(status_url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        _log.warning("corpus_status_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        _log.warning("corpus_status_request_failed", status=response.status_code)
        raise CorpusUnavailable(
            f"corpus returned HTTP {response.status_code}",
            status=response.status_code,
        )

    return _parse_2xx_body(response, CorpusStatusResponse, event_prefix="corpus_status")
