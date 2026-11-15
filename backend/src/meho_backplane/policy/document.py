# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The policy file (``policy: v1``): its models, its strict parser, its stored form.

The policy file is a file of its own, not a section of the settings file.
It holds:

* ``policy``: the file format, always ``v1``;
* ``description``: free text;
* ``taxonomy``: the categories (``categories``) and, in the install's
  file only, the category of an operation no entry classifies
  (``default``);
* ``classifications``: the entries that give connectors, operation groups
  and operations a category, labels, a higher risk level or the "always
  wait" mark (:mod:`meho_backplane.policy.classification`);
* ``routing``: who is asked to approve a change that waits, by category,
  connector or risk level (:mod:`meho_backplane.policy.routing`).

The parser uses the strict YAML loader of the settings file
(:mod:`meho_backplane.instance_settings.schema`): a repeated key, a YAML
anchor or alias, and an unknown key are each a mistake. Every mistake is
one sentence that names the path, for example
``classifications[2].match.op``.

The checks against the operations MEHO knows (unknown category, the
floors) are in :mod:`meho_backplane.policy.validate`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from meho_backplane.auth.operator import RoleGroup
from meho_backplane.instance_settings.schema import (
    _load_yaml,
    format_validation_errors,
    render_path,
)
from meho_backplane.policy.store import _check_labels

__all__ = [
    "GROUP_KINDS",
    "MAX_POLICY_BYTES",
    "POLICY_VERSION",
    "ParsedPolicy",
    "PolicyFile",
    "canonical_sha256",
    "document_of",
    "file_sha256",
    "normalise_document",
    "parse_policy_file",
]

#: The one file format this release reads and writes.
POLICY_VERSION: Final[str] = "v1"

#: Refuse a larger file before parsing it.
MAX_POLICY_BYTES: Final[int] = 1024 * 1024

_CATEGORY_RE: Final[str] = r"^[a-z][a-z0-9_-]{0,62}$"

#: The five login group kinds a routing entry may ask, in this order.
GROUP_KINDS: Final[tuple[str, ...]] = tuple(kind.value for kind in RoleGroup)
_PATTERN = Annotated[str, Field(min_length=1, max_length=200)]


class _Strict(BaseModel):
    """Base for every file model: unknown keys are mistakes, values never echo."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PolicyMatch(_Strict):
    """``classifications[].match``: what the entry classifies."""

    connector: _PATTERN | None = None
    group: _PATTERN | None = None
    op: _PATTERN | None = None

    @model_validator(mode="after")
    def _one_field_at_least(self) -> PolicyMatch:
        if self.connector is None and self.group is None and self.op is None:
            raise ValueError("needs at least one of connector, group or op")
        return self


class PolicyClassification(_Strict):
    """``classifications[]``: one entry. ``risk`` and ``category`` are checked later."""

    match: PolicyMatch
    category: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    labels: dict[str, str] | None = None
    risk: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    always_wait: StrictBool | None = None

    @field_validator("labels")
    @classmethod
    def _labels_fit(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            _check_labels(value)
        return value

    @model_validator(mode="after")
    def _sets_a_value(self) -> PolicyClassification:
        if (
            self.category is None
            and self.labels is None
            and self.risk is None
            and self.always_wait is None
        ):
            raise ValueError("sets nothing: give category, labels, risk or always_wait")
        return self


class PolicyTaxonomy(_Strict):
    """``taxonomy``: the categories, and the install's default category."""

    default: str | None = None
    categories: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("categories")
    @classmethod
    def _names_fit(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.match(_CATEGORY_RE, name):
                raise ValueError(
                    "holds a name that is not a category name: use 1 to 63 lower case "
                    "letters, digits, - and _, starting with a letter"
                )
        if len(set(value)) != len(value):
            raise ValueError("lists a category twice")
        return value


class PolicyRouteMatch(_Strict):
    """``routing[].match``: which waiting changes the entry routes.

    ``category`` and ``risk`` are checked later
    (:func:`meho_backplane.policy.validate.check_document`).
    """

    category: Annotated[str, Field(min_length=1, max_length=63)] | None = None
    connector: _PATTERN | None = None
    risk: Annotated[str, Field(min_length=1, max_length=63)] | None = None

    @model_validator(mode="after")
    def _one_field_at_least(self) -> PolicyRouteMatch:
        if self.category is None and self.connector is None and self.risk is None:
            raise ValueError("needs at least one of category, connector or risk")
        return self


class PolicyRoute(_Strict):
    """``routing[]``: one entry. The group kinds that are asked to approve."""

    match: PolicyRouteMatch
    approvers: list[str] = Field(min_length=1, max_length=len(GROUP_KINDS))

    @field_validator("approvers")
    @classmethod
    def _known_kinds(cls, value: list[str]) -> list[str]:
        if any(kind not in GROUP_KINDS for kind in value):
            raise ValueError(
                "names a group kind that does not exist; use " + ", ".join(GROUP_KINDS)
            )
        if len(set(value)) != len(value):
            raise ValueError("names a group kind twice")
        return value


class PolicyFile(_Strict):
    """The whole file. ``policy`` is checked separately for a clear sentence."""

    policy: str
    description: Annotated[str, Field(max_length=2000)] | None = None
    taxonomy: PolicyTaxonomy | None = None
    classifications: list[PolicyClassification] = Field(default_factory=list, max_length=2000)
    routing: list[PolicyRoute] = Field(default_factory=list, max_length=200)


@dataclass
class ParsedPolicy:
    """Result of :func:`parse_policy_file`: the model (if valid) and every mistake."""

    model: PolicyFile | None
    mistakes: list[str] = field(default_factory=list)


def _repeated_match_mistakes(model: PolicyFile) -> list[str]:
    mistakes: list[str] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(model.classifications):
        key = json.dumps(entry.match.model_dump(exclude_none=True), sort_keys=True)
        if key in seen:
            mistakes.append(
                f"`classifications[{index}].match` repeats `classifications[{seen[key]}]`; "
                "list each match once."
            )
        else:
            seen[key] = index
    seen = {}
    for index, route in enumerate(model.routing):
        key = json.dumps(route.match.model_dump(exclude_none=True), sort_keys=True)
        if key in seen:
            mistakes.append(
                f"`routing[{index}].match` repeats `routing[{seen[key]}]`; the first one "
                "always wins, so list each match once."
            )
        else:
            seen[key] = index
    return mistakes


def parse_policy_file(content: bytes) -> ParsedPolicy:
    """Parse and check *content*; collect every mistake as one sentence each."""
    if len(content) > MAX_POLICY_BYTES:
        return ParsedPolicy(None, [f"The file is larger than {MAX_POLICY_BYTES // 1024} KiB."])
    loaded = _load_yaml(content)
    if isinstance(loaded, str):
        return ParsedPolicy(None, [loaded])
    raw, loader = loaded
    mistakes = [
        f"Line {line}: the key `{render_path((key,))}` appears twice in the same block."
        for key, line in loader.duplicates
    ]
    mistakes.extend(
        f"Line {line}: the value looks like a date but is not a real date."
        for line in loader.unreadable
    )
    mistakes.extend(
        f"Line {line}: `!!{yaml_type}` values are not accepted; the policy file holds "
        "only text, true/false, lists and mappings."
        for line, yaml_type in dict.fromkeys(loader.refused)
    )
    if loader.refused:
        return ParsedPolicy(None, mistakes)
    if not isinstance(raw, dict):
        mistakes.append("The file must be a mapping that starts with `policy: v1`.")
        return ParsedPolicy(None, mistakes)
    if "policy" in raw and raw["policy"] != POLICY_VERSION:
        mistakes.append(f"`policy` is not a file format this MEHO reads; use `{POLICY_VERSION}`.")
    model: PolicyFile | None = None
    try:
        model = PolicyFile.model_validate(raw)
    except ValidationError as exc:
        mistakes.extend(format_validation_errors(exc))
    if model is not None:
        mistakes.extend(_repeated_match_mistakes(model))
    return ParsedPolicy(None if mistakes else model, mistakes)


# ---------------------------------------------------------------------------
# The stored form
# ---------------------------------------------------------------------------


def _entry_dict(entry: PolicyClassification) -> dict[str, Any]:
    out: dict[str, Any] = {"match": entry.match.model_dump(exclude_none=True)}
    for key in ("category", "labels", "risk", "always_wait"):
        value = getattr(entry, key)
        if value is not None:
            out[key] = value
    return out


def document_of(model: PolicyFile) -> dict[str, Any]:
    """The stored document of a valid file: only the keys that are set."""
    document: dict[str, Any] = {}
    if model.description:
        document["description"] = model.description
    if model.taxonomy is not None:
        taxonomy: dict[str, Any] = {}
        if model.taxonomy.default is not None:
            taxonomy["default"] = model.taxonomy.default
        if model.taxonomy.categories:
            taxonomy["categories"] = list(model.taxonomy.categories)
        if taxonomy:
            document["taxonomy"] = taxonomy
    document["classifications"] = [_entry_dict(e) for e in model.classifications]
    if model.routing:
        document["routing"] = [
            {"match": r.match.model_dump(exclude_none=True), "approvers": list(r.approvers)}
            for r in model.routing
        ]
    return document


def normalise_document(document: dict[str, Any] | None) -> dict[str, Any]:
    """*document* without empty values, so two documents with one meaning compare equal."""
    out: dict[str, Any] = {}
    for key, value in (document or {}).items():
        if value in (None, "", [], {}):
            continue
        out[key] = normalise_document(value) if isinstance(value, dict) else value
    return out


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_sha256(document: dict[str, Any] | None) -> str:
    """The SHA-256 of *document*'s meaning.

    Key order, comments and empty keys do not count, and neither does the
    order of the classification entries or of the categories: the most
    specific entry wins, not the first one. The order of the ``routing``
    entries counts: the first entry that matches wins.
    """
    meaning = normalise_document(document)
    if "classifications" in meaning:
        meaning["classifications"] = sorted(meaning["classifications"], key=_json)
    taxonomy = meaning.get("taxonomy")
    if isinstance(taxonomy, dict) and "categories" in taxonomy:
        taxonomy["categories"] = sorted(taxonomy["categories"], key=_json)
    return hashlib.sha256(_json(meaning).encode()).hexdigest()


def file_sha256(content: bytes) -> str:
    """The SHA-256 of the file as sent."""
    return hashlib.sha256(content).hexdigest()
