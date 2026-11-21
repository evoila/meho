# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The question file of the agent search runner.

A YAML file with one key, ``questions``. Each question has:

* ``id``: a name that is used once in the file;
* ``question``: the text sent to ``search_knowledge`` as ``query``;
* ``expect``: the path of the page that should be found;
* ``repo`` (optional): the knowledge repo the page is in;
* ``kind`` (optional): ``meaning`` (default) or ``exact``;
* ``filters`` (optional): the ``filters`` argument, a mapping that can be
  sent as JSON.

The loader refuses a missing field, an unknown field, an id used twice and
filters that cannot be sent as JSON (also NaN and infinity). Test tooling only: nothing here is
read by MEHO itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

KINDS: Final[tuple[str, ...]] = ("meaning", "exact")
_REQUIRED: Final[frozenset[str]] = frozenset({"id", "question", "expect"})
_OPTIONAL: Final[frozenset[str]] = frozenset({"repo", "kind", "filters"})


class QuestionFileError(ValueError):
    """The question file cannot be used. The text says why."""


@dataclass(frozen=True)
class Question:
    """One question and the page that should answer it."""

    id: str
    question: str
    expect: str
    repo: str | None = None
    kind: str = "meaning"
    filters: dict[str, Any] = field(default_factory=dict)


def load_questions(path: Path) -> list[Question]:
    """Read and check the question file at *path*."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise QuestionFileError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"questions"}:
        raise QuestionFileError("the file must have exactly one key: questions")
    entries = raw["questions"]
    if not isinstance(entries, list) or not entries:
        raise QuestionFileError("questions must be a list with at least one question")
    questions: list[Question] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        question = _parse_entry(entry, index)
        if question.id in seen:
            raise QuestionFileError(f"id used twice: {question.id}")
        seen.add(question.id)
        questions.append(question)
    return questions


def _parse_entry(entry: object, index: int) -> Question:
    where = f"question {index}"
    if not isinstance(entry, dict):
        raise QuestionFileError(f"{where}: must be a mapping")
    if isinstance(entry.get("id"), str):
        where = f"question {entry['id']}"
    _check_field_names(entry, where)
    for name in ("id", "question", "expect"):
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise QuestionFileError(f"{where}: {name} must be a text that is not empty")
    repo = entry.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise QuestionFileError(f"{where}: repo must be a text that is not empty")
    kind = entry.get("kind", "meaning")
    if kind not in KINDS:
        raise QuestionFileError(f"{where}: kind must be one of {', '.join(KINDS)}")
    filters = _parse_filters(entry.get("filters") or {}, where)
    return Question(
        id=entry["id"],
        question=entry["question"],
        expect=entry["expect"].strip("/"),
        repo=repo,
        kind=kind,
        filters=filters,
    )


def _check_field_names(entry: dict[Any, Any], where: str) -> None:
    missing = sorted(_REQUIRED - set(entry))
    if missing:
        raise QuestionFileError(f"{where}: missing field {', '.join(missing)}")
    unknown = sorted(str(name) for name in set(entry) - _REQUIRED - _OPTIONAL)
    if unknown:
        raise QuestionFileError(f"{where}: unknown field {', '.join(unknown)}")


def _parse_filters(filters: object, where: str) -> dict[str, Any]:
    if not isinstance(filters, dict):
        raise QuestionFileError(f"{where}: filters must be a mapping")
    try:
        json.dumps(filters, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise QuestionFileError(f"{where}: filters cannot be sent as JSON: {exc}") from exc
    return filters
