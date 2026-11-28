# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parse the ``k8s.apply`` manifest, and say what is wrong with one.

One function, :func:`_parse_manifest`, serves both the check before the
call waits for approval (``ops_write_preview._apply_park_check``) and the
run (``ops_write_dangerous.k8s_apply``). So a manifest that passes the
check passes the parse step of the run, and both give the same text.

The text of :class:`ApplyManifestError` names the document by its number,
counted from 1, and shows the document's ``apiVersion`` and ``kind`` only
when each has the shape Kubernetes allows for it. Any other value of the
manifest, and any YAML error text, is never shown: an invalid value is
the fixed mark :data:`NOT_SHOWN`. A document that is not valid YAML or
JSON is named with the line and the column where the YAML reader stopped,
and one fixed phrase for the kind of problem (:func:`_yaml_problem`).

A document is measured before YAML builds it (:func:`_written_out_size`):
one that is larger than :data:`WRITTEN_OUT_LIMIT` with each alias and
merge key written out is refused, and is never built.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

import yaml

__all__ = ["NOT_SHOWN", "WRITTEN_OUT_LIMIT", "ApplyManifestError", "manifest_size_problem"]

#: The largest size of one document, in characters, with each YAML alias
#: and merge key written out. The Kubernetes API server refuses a request
#: body larger than 3 MiB by default (``MaxRequestBodyBytes``).
WRITTEN_OUT_LIMIT = 3 * 1024 * 1024

_TOO_LARGE = f"is larger than {WRITTEN_OUT_LIMIT} characters when its YAML aliases are written out"

#: The last manifest text parsed in this task and its documents, so the
#: check before the wait and the preview copy share one parse.
_LAST_PARSE: ContextVar[tuple[str, list[dict[str, Any]]] | None] = ContextVar(
    "k8s_apply_last_parse", default=None
)

#: What the refusal shows in place of an ``apiVersion`` or ``kind`` that
#: does not have the shape Kubernetes allows.
NOT_SHOWN = "(not shown)"

#: The refusal texts are cut at this length (``_errors._EXC_MESSAGE_CAP``).
TEXT_LIMIT = 256

_PREFIX = "The manifest cannot be applied: "

# A kind whose lower-case form is a DNS-1035 label (at most 63 characters).
_KIND = re.compile(r"[A-Za-z]([-A-Za-z0-9]{0,61}[A-Za-z0-9])?")
# A version: a DNS-1035 label (at most 63 characters).
_VERSION = re.compile(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?")
# One dot-separated part of a group (a DNS-1123 subdomain).
_GROUP_PART = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_GROUP_MAX = 253

_ARTICLE = {"apiVersion": "an", "kind": "a"}

_NOT_VALID = "is not valid YAML or JSON"

#: The fixed phrase for each class of YAML error, checked in this order.
_YAML_PROBLEMS: tuple[tuple[type[BaseException], str], ...] = (
    (yaml.scanner.ScannerError, "a token YAML refuses"),
    (yaml.parser.ParserError, "a structure YAML refuses"),
    (yaml.composer.ComposerError, "an alias or anchor YAML refuses"),
    (yaml.constructor.ConstructorError, "a tag or value YAML cannot build"),
)
#: The phrase for an error of any other class (a date not in the calendar).
_OTHER_PROBLEM = "a value YAML cannot build"


def _safe_kind(value: object) -> str:
    if isinstance(value, str) and _KIND.fullmatch(value):
        return value
    return NOT_SHOWN


def _safe_api_version(value: object) -> str:
    if not isinstance(value, str):
        return NOT_SHOWN
    group, slash, version = value.rpartition("/")
    if not _VERSION.fullmatch(version):
        return NOT_SHOWN
    if slash and (
        len(group) > _GROUP_MAX or not all(_GROUP_PART.fullmatch(p) for p in group.split("."))
    ):
        return NOT_SHOWN
    return value


class ApplyManifestError(ValueError):
    """The apply manifest could not be parsed or is missing required fields.

    Subclasses :class:`ValueError` so the dispatcher's
    ``connector_error`` envelope tags
    ``extras.exception_class="ApplyManifestError"`` and the operator
    sees the parse/validation reason rather than an opaque YAML error.

    *api_version* and *kind* are the values as the document has them (or
    ``None`` when it has none). The text shows each only in its safe form.
    """

    def __init__(
        self,
        problem: str,
        *,
        number: int | None = None,
        api_version: object = None,
        kind: object = None,
        reason: str | None = None,
        position: str | None = None,
    ) -> None:
        self.problem = problem
        self.number = number
        self.reason = reason
        self.position = position
        self.api_version = None if api_version is None else _safe_api_version(api_version)
        self.kind = None if kind is None else _safe_kind(kind)
        super().__init__(self.text())

    def _detail(self, api_version: str | None, kind: str | None, position: str | None) -> str:
        if self.number is None:
            return self.problem
        why = ", ".join(p for p in (self.reason, position) if p)
        problem = f"{self.problem} ({why})" if why else self.problem
        parts = [f"apiVersion {api_version}" if api_version is not None else None]
        parts.append(f"kind {kind}" if kind is not None else None)
        shown = ", ".join(p for p in parts if p)
        where = f" ({shown})" if shown else ""
        return f"document {self.number}{where} {problem}"

    def text(self, tail: str = "") -> str:
        """The refusal text, ending in *tail*, at most :data:`TEXT_LIMIT` long.

        A long place (the line and the column) is left out first, then a
        long ``apiVersion`` or ``kind`` gives way to :data:`NOT_SHOWN`, so
        *tail* (the form the caller must send) is never cut.
        """
        hidden_api = None if self.api_version is None else NOT_SHOWN
        hidden_kind = None if self.kind is None else NOT_SHOWN
        for api_version, kind, position in (
            (self.api_version, self.kind, self.position),
            (self.api_version, self.kind, None),
            (hidden_api, self.kind, None),
            (hidden_api, hidden_kind, None),
        ):
            text = f"{_PREFIX}{self._detail(api_version, kind, position)}{tail}"
            if len(text) <= TEXT_LIMIT:
                return text
        return text


def _given(value: object) -> object:
    """*value*, or ``None`` for an empty text: an empty text counts as missing."""
    return None if value == "" else value


def _check_document(number: int, doc: object) -> None:
    """Raise :class:`ApplyManifestError` when *doc* cannot be applied."""
    if not isinstance(doc, dict):
        raise ApplyManifestError("is not a mapping", number=number)
    api_version = _given(doc.get("apiVersion"))
    kind = _given(doc.get("kind"))

    def fail(problem: str) -> ApplyManifestError:
        return ApplyManifestError(problem, number=number, api_version=api_version, kind=kind)

    for field, value in (("apiVersion", api_version), ("kind", kind)):
        if value is None:
            raise fail(f"is missing {field}")
        if not isinstance(value, str):
            raise fail(f"has {_ARTICLE[field]} {field} that is not a text")
    if (api_version == "v1" and kind == "List") or isinstance(doc.get("items"), list):
        raise fail("is a list object")
    metadata = doc.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise fail("has a metadata that is not a mapping")
    name = _given((metadata or {}).get("name"))
    if name is None:
        raise fail("is missing metadata.name")
    if not isinstance(name, str):
        raise fail("has a metadata.name that is not a text (put it in quotes)")


def _yaml_problem(exc: BaseException) -> tuple[str, str | None]:
    """The fixed phrase for *exc*, and the line and column of its mark (or ``None``).

    Only the class of the error and the numbers of its mark are used: its
    text, and the line of the manifest that a mark carries, never are.
    """
    phrase = next((p for cls, p in _YAML_PROBLEMS if isinstance(exc, cls)), _OTHER_PROBLEM)
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line, column = getattr(mark, "line", None), getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        return phrase, f"line {line + 1}, column {column + 1}"
    return phrase, None


def _written_out_size(root: yaml.Node) -> int:
    """The size of *root* with each alias and merge key written out, capped above the limit.

    Each scalar counts its length plus one, each list and mapping counts
    one. A node that aliases name many times is measured once and its size
    is reused, so the cost follows the number of nodes in the text, not
    the written-out size. A merge list counts each mapping it names, as
    often as it names it. A node that holds itself has no end, so it
    counts as over the limit. Returns at most ``WRITTEN_OUT_LIMIT + 1``.
    """
    over = WRITTEN_OUT_LIMIT + 1
    sizes: dict[int, int] = {}
    open_nodes: set[int] = set()

    def size(node: yaml.Node) -> int:
        key = id(node)
        if key in sizes:
            return sizes[key]
        if key in open_nodes:
            return over
        if isinstance(node, yaml.ScalarNode):
            total = len(node.value) + 1
        else:
            open_nodes.add(key)
            total = 1
            children = (
                node.value
                if isinstance(node, yaml.SequenceNode)
                else [part for pair in node.value for part in pair]
            )
            for child in children:
                total += size(child)
                if total >= over:
                    total = over
                    break
            open_nodes.discard(key)
        sizes[key] = total
        return total

    return size(root)


def _check_size(number: int, node: yaml.Node) -> None:
    if _written_out_size(node) > WRITTEN_OUT_LIMIT:
        raise ApplyManifestError(_TOO_LARGE, number=number)


def manifest_size_problem(manifest: str) -> ApplyManifestError | None:
    """The refusal of the first document of *manifest* over :data:`WRITTEN_OUT_LIMIT`.

    Reads the YAML structure only: no document is built, so no alias or
    merge key is written out. Returns ``None`` when every document fits,
    and also at a YAML error, which :func:`_parse_manifest` names.
    """
    try:
        loader = yaml.SafeLoader(manifest)
    except yaml.reader.ReaderError:
        return None
    try:
        number = 0
        while True:
            number += 1
            try:
                if not loader.check_node():
                    return None
                node = loader.get_node()
            except Exception:
                return None
            try:
                _check_size(number, node)
            except ApplyManifestError as exc:
                return exc
    finally:
        loader.dispose()


def _parse_manifest(manifest: str, *, shared: bool = True) -> list[dict[str, Any]]:
    """Parse a single- or multi-document YAML/JSON manifest into doc dicts.

    ``yaml.safe_load_all`` handles both JSON (a subset of YAML) and
    multi-doc YAML separated by ``---``. Empty documents (a trailing
    ``---`` or blank stanza) are dropped, but still counted, so the
    number in a refusal is the place of the document in the text. Each
    surviving doc must be a mapping with a ``metadata`` mapping, and
    ``apiVersion``, ``kind`` and ``metadata.name`` must be nonempty texts
    -- SSA cannot address an object without them.

    Before the wait, a document is refused as a list object when its
    ``apiVersion`` is ``v1`` and its ``kind`` is ``List``, or when its
    top-level ``items`` field is a list. Any other document passes this
    check to the wait.

    A manifest that is not valid YAML or JSON is refused before the wait
    with "document N is not valid YAML or JSON (KIND, line L, column C)".
    L and C are where the YAML reader stopped, counted from the start of
    the whole text. KIND is one fixed phrase, chosen by the class of the
    error: "a token YAML refuses" (a scanner error), "a structure YAML
    refuses" (a parser error), "an alias or anchor YAML refuses" (a
    composer error), "a tag or value YAML cannot build" (a constructor
    error), and "a value YAML cannot build" for any other class (a date
    that is not in the calendar). An error with no mark gives no line and
    column. The refusal holds no text of the manifest and no text of the
    YAML reader. A character that the YAML reader refuses gives "the text
    has a character that YAML does not allow, for example a control
    character", with no document number.

    A document larger than :data:`WRITTEN_OUT_LIMIT` characters with its
    YAML aliases and merge keys written out is refused with "document N
    is larger than ... characters when its YAML aliases are written out",
    before YAML builds it (:func:`_written_out_size`).

    The documents of the last text parsed in this task are kept, so a
    second call with the same text does not parse it again. The caller
    gets a new list; the documents in it are shared and must not be
    changed. With ``shared=False`` (the run) the text is parsed again and
    the documents are the caller's own.
    Raises :class:`ApplyManifestError` on any structural problem.
    """
    if not shared:
        return _parse_documents(manifest)
    last = _LAST_PARSE.get()
    if last is not None and last[0] == manifest:
        return list(last[1])
    parsed = _parse_documents(manifest)
    _LAST_PARSE.set((manifest, parsed))
    return list(parsed)


def _parse_documents(manifest: str) -> list[dict[str, Any]]:
    """The work of :func:`_parse_manifest`: each document is measured, then built."""
    try:
        loader = yaml.SafeLoader(manifest)
    except yaml.reader.ReaderError:
        # The reader checks the whole text before document 1, so no
        # document number would be right.
        raise ApplyManifestError(
            "the text has a character that YAML does not allow, for example a control character"
        ) from None
    try:
        parsed: list[dict[str, Any]] = []
        number = 0
        while True:
            number += 1
            try:
                if not loader.check_node():
                    break
                node = loader.get_node()
                _check_size(number, node)
                doc = loader.construct_document(node)
            except ApplyManifestError:
                raise
            except Exception as exc:
                # Any class: a value YAML cannot build may be a ValueError, a KeyError or more.
                reason, position = _yaml_problem(exc)
                raise ApplyManifestError(
                    _NOT_VALID, number=number, reason=reason, position=position
                ) from None
            if doc is None:
                continue
            _check_document(number, doc)
            parsed.append(doc)
    finally:
        loader.dispose()
    if not parsed:
        raise ApplyManifestError("the text has no documents")
    return parsed
