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
the fixed mark :data:`NOT_SHOWN`.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

__all__ = ["NOT_SHOWN", "ApplyManifestError"]

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
    ) -> None:
        self.problem = problem
        self.number = number
        self.api_version = None if api_version is None else _safe_api_version(api_version)
        self.kind = None if kind is None else _safe_kind(kind)
        super().__init__(self.text())

    def _detail(self, api_version: str | None, kind: str | None) -> str:
        if self.number is None:
            return self.problem
        parts = [f"apiVersion {api_version}" if api_version is not None else None]
        parts.append(f"kind {kind}" if kind is not None else None)
        shown = ", ".join(p for p in parts if p)
        where = f" ({shown})" if shown else ""
        return f"document {self.number}{where} {self.problem}"

    def text(self, tail: str = "") -> str:
        """The refusal text, ending in *tail*, at most :data:`TEXT_LIMIT` long.

        A long ``apiVersion`` or ``kind`` gives way to :data:`NOT_SHOWN`
        first, so *tail* (the form the caller must send) is never cut.
        """
        hidden_api = None if self.api_version is None else NOT_SHOWN
        hidden_kind = None if self.kind is None else NOT_SHOWN
        for api_version, kind in (
            (self.api_version, self.kind),
            (hidden_api, self.kind),
            (hidden_api, hidden_kind),
        ):
            text = f"{_PREFIX}{self._detail(api_version, kind)}{tail}"
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
    if kind == "List" or "items" in doc:
        raise fail("is a list object")
    metadata = doc.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise fail("has a metadata that is not a mapping")
    name = _given((metadata or {}).get("name"))
    if name is None:
        raise fail("is missing metadata.name")
    if not isinstance(name, str):
        raise fail("has a metadata.name that is not a text (put it in quotes)")


def _parse_manifest(manifest: str) -> list[dict[str, Any]]:
    """Parse a single- or multi-document YAML/JSON manifest into doc dicts.

    ``yaml.safe_load_all`` handles both JSON (a subset of YAML) and
    multi-doc YAML separated by ``---``. Empty documents (a trailing
    ``---`` or blank stanza) are dropped, but still counted, so the
    number in a refusal is the place of the document in the text. Each
    surviving doc must be a mapping with a ``metadata`` mapping, and
    ``apiVersion``, ``kind`` and ``metadata.name`` must be nonempty texts
    -- SSA cannot address an object without them. A list object is
    refused too: a document whose ``kind`` is ``List``, or that has a
    top-level ``items`` field, in any group. A Kubernetes API server
    refuses the server-side apply of such a document.
    Raises :class:`ApplyManifestError` on any structural problem.
    """
    docs = iter(yaml.safe_load_all(manifest))
    parsed: list[dict[str, Any]] = []
    number = 0
    while True:
        number += 1
        try:
            doc = next(docs)
        except StopIteration:
            break
        except yaml.reader.ReaderError as exc:
            # The reader checks the whole text before document 1, so no
            # document number would be right.
            raise ApplyManifestError(
                "the text has a character that YAML does not allow, for example a control character"
            ) from exc
        except (yaml.YAMLError, ValueError) as exc:
            # A value YAML cannot build (a date that does not exist) is a ValueError.
            raise ApplyManifestError("is not valid YAML or JSON", number=number) from exc
        if doc is None:
            continue
        _check_document(number, doc)
        parsed.append(doc)
    if not parsed:
        raise ApplyManifestError("the text has no documents")
    return parsed
