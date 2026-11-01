#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the connector kit's name list again and raise the kit version.

The connector kit is the module ``meho_connector_kit`` in
``backend/connector-kit/``. The file ``backend/connector-kit/kit-names.json``
lists every kit name with its inputs, and the kit version.
``tests/test_connector_kit.py`` fails when the code and the file differ.

This command builds the list from the code, compares it with the file, and:

* raises the **first** number of the kit version when a name is removed or
  its inputs change (a package may break);
* raises the **second** number when names are only added, or a name is
  marked as old;
* refuses to remove a name that the file does not mark as old. An old name
  stays for one kit version, then it may go.

It writes the new version into the file and into
``backend/connector-kit/pyproject.toml``. Then run ``uv lock`` so
``uv.lock`` has the new kit version too.

Run from ``backend/``::

    uv run python -m scripts.write_connector_kit_names

See ``docs/codebase/connector-packages.md``, "The connector kit".
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from annotationlib import Format, get_annotations

KIT_DIR = Path(__file__).resolve().parents[1] / "connector-kit"
NAMES_FILE = KIT_DIR / "kit-names.json"
KIT_PYPROJECT = KIT_DIR / "pyproject.toml"
COMMAND = "cd backend && uv run python -m scripts.write_connector_kit_names"

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
_PYPROJECT_VERSION = re.compile(r'^version = "([^"]*)"$', re.MULTILINE)

type Entry = dict[str, Any]
type Level = Literal["first", "second"]


class KitError(Exception):
    """The kit module does not match its own promise list."""


# ---------------------------------------------------------------------------
# Build the list from the code
# ---------------------------------------------------------------------------


def _type_text(annotation: object) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _default_text(default: object) -> str:
    # Some defaults are plain objects whose repr holds a memory address.
    return _ADDRESS.sub("", repr(default))


def _input_text(param: inspect.Parameter) -> str:
    text = param.name
    if param.kind is inspect.Parameter.VAR_POSITIONAL:
        text = "*" + text
    elif param.kind is inspect.Parameter.VAR_KEYWORD:
        text = "**" + text
    type_text = _type_text(param.annotation)
    if type_text is not None:
        text += f": {type_text}"
    if param.default is not inspect.Parameter.empty:
        text += f" = {_default_text(param.default)}"
    return text


def describe_callable(obj: Any) -> Entry:
    """The inputs and the answer type of a function, in Python's own notation.

    A ``/`` marks the end of the inputs that can only be given by position,
    and a ``*`` the start of the inputs that can only be given by name.
    """
    signature = inspect.signature(obj, annotation_format=Format.STRING)
    inputs: list[str] = []
    params = list(signature.parameters.values())
    for index, param in enumerate(params):
        if param.kind is inspect.Parameter.KEYWORD_ONLY and (
            index == 0 or params[index - 1].kind not in _NAMED_OR_STAR
        ):
            inputs.append("*")
        inputs.append(_input_text(param))
        is_last_positional_only = param.kind is inspect.Parameter.POSITIONAL_ONLY and (
            index + 1 == len(params) or params[index + 1].kind is not param.kind
        )
        if is_last_positional_only:
            inputs.append("/")
    return {"inputs": inputs, "returns": _type_text(signature.return_annotation)}


_NAMED_OR_STAR = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.VAR_POSITIONAL)


def _field_types(cls: type) -> dict[str, str | None]:
    types: dict[str, str | None] = {}
    for klass in reversed(cls.__mro__):
        for name, annotation in get_annotations(klass, format=Format.STRING).items():
            types[name] = _type_text(annotation)
    return types


def _describe_part(cls: type, part: str) -> Entry:
    raw = inspect.getattr_static(cls, part, None)
    if isinstance(raw, property):
        if raw.fget is None:
            raise KitError(f"{cls.__name__}.{part} is a property without a getter.")
        returns = describe_callable(raw.fget)["returns"]
        return {"kind": "field", "type": returns}
    if inspect.isfunction(raw) or isinstance(raw, (staticmethod, classmethod)):
        return {"kind": "method", **describe_callable(getattr(cls, part))}
    fields = _field_types(cls)
    if part in fields:
        return {"kind": "field", "type": fields[part]}
    raise KitError(f"{cls.__name__} has no part named {part!r}.")


def describe(obj: Any, parts: tuple[str, ...] = ()) -> Entry:
    """One entry of the list: what the name is, and what the kit promises of it."""
    if inspect.isclass(obj):
        entry: Entry = {"kind": "class"}
        if parts:
            entry["parts"] = {part: _describe_part(obj, part) for part in parts}
        return entry
    if callable(obj):
        return {"kind": "function", **describe_callable(obj)}
    return {"kind": "value", "type": type(obj).__name__}


def build_names() -> dict[str, Entry]:
    """Build the name list from the kit module."""
    import meho_connector_kit as kit

    promised: dict[str, tuple[str, ...]] = kit._PROMISED_PARTS
    old: dict[str, str] = kit._OLD_NAMES
    unknown = sorted((set(promised) | set(old)) - set(kit.__all__))
    if unknown:
        raise KitError(f"These names are not in meho_connector_kit.__all__: {unknown}.")
    names: dict[str, Entry] = {}
    for name in sorted(kit.__all__):
        entry = describe(getattr(kit, name), promised.get(name, ()))
        if name in old:
            entry["old"] = old[name]
        names[name] = entry
    return names


# ---------------------------------------------------------------------------
# Compare two lists
# ---------------------------------------------------------------------------


@dataclass
class Changes:
    """How a new name list differs from the one in the file."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    marked_old: list[str] = field(default_factory=list)
    #: Removed names that the file did not mark as old.
    refused: list[str] = field(default_factory=list)

    @property
    def level(self) -> Level | None:
        """The number of the kit version that must go up, or ``None``."""
        if self.removed or self.changed:
            return "first"
        if self.added or self.marked_old:
            return "second"
        return None


def _without_old(entry: Entry) -> Entry:
    return {key: value for key, value in entry.items() if key != "old"}


def compare(old: dict[str, Entry], new: dict[str, Entry]) -> Changes:
    """Compare the list in the file (``old``) with the list from the code."""
    changes = Changes()
    changes.added = sorted(set(new) - set(old))
    changes.removed = sorted(set(old) - set(new))
    changes.refused = [name for name in changes.removed if "old" not in old[name]]
    for name in sorted(set(old) & set(new)):
        if _without_old(old[name]) != _without_old(new[name]):
            changes.changed.append(name)
        elif old[name].get("old") != new[name].get("old"):
            changes.marked_old.append(name)
    return changes


def raise_version(version: str, level: Level) -> str:
    """Raise the first or the second number of a ``<first>.<second>`` version."""
    first, second = (int(part) for part in version.split("."))
    if level == "first":
        return f"{first + 1}.0"
    return f"{first}.{second + 1}"


def describe_changes(changes: Changes, old: dict[str, Entry], new: dict[str, Entry]) -> str:
    """The lines a person reads: every changed name, and for a change, old and new."""
    lines: list[str] = []
    for name in changes.removed:
        lines.append(f"- removed: {name}")
    for name in changes.changed:
        lines.append(f"- changed: {name}")
        lines.append(f"    in the file: {json.dumps(_without_old(old[name]), sort_keys=True)}")
        lines.append(f"    in the code: {json.dumps(_without_old(new[name]), sort_keys=True)}")
    for name in changes.added:
        lines.append(f"- added: {name}")
    for name in changes.marked_old:
        lines.append(f"- marked as old: {name} ({new[name].get('old', 'no longer old')})")
    return "\n".join(lines)


def refusal_text(changes: Changes) -> str:
    """Why a removal is refused, and what to do instead."""
    return (
        f"These kit names were removed, but the kit version before did not mark them "
        f"as old: {', '.join(changes.refused)}. Put the name back, add it to _OLD_NAMES "
        f"in meho_connector_kit with what to use instead, and run `{COMMAND}`. Remove "
        f"it in a later kit version."
    )


def check_message(old: dict[str, Entry], new: dict[str, Entry]) -> str | None:
    """Why the list in the file (``old``) does not fit the code, or ``None``."""
    changes = compare(old, new)
    if changes.refused:
        return refusal_text(changes)
    if changes.level is None:
        return None
    return (
        "The connector kit changed, but kit-names.json was not written again:\n"
        f"{describe_changes(changes, old, new)}\n"
        f"Raise the {changes.level} number of the kit version: run `{COMMAND}`, "
        "then `uv lock`."
    )


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def read_names_file(path: Path = NAMES_FILE) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def read_pyproject_version(path: Path = KIT_PYPROJECT) -> str:
    match = _PYPROJECT_VERSION.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise KitError(f'{path} has no `version = "..."` line.')
    return match.group(1)


def _write_files(version: str, names: dict[str, Entry], names_file: Path, pyproject: Path) -> None:
    data = {"kit_version": version, "names": names}
    names_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        _PYPROJECT_VERSION.sub(f'version = "{version}"', text, count=1), encoding="utf-8"
    )


def write(
    names: dict[str, Entry],
    names_file: Path = NAMES_FILE,
    pyproject: Path = KIT_PYPROJECT,
) -> int:
    """Compare, raise the version and write both files. Returns the exit code."""
    if not names_file.exists():
        version = read_pyproject_version(pyproject)
        _write_files(version, names, names_file, pyproject)
        print(f"Wrote {names_file.name} with kit version {version}.")
        return 0
    current = read_names_file(names_file)
    changes = compare(current["names"], names)
    if changes.refused:
        print(refusal_text(changes), file=sys.stderr)
        return 1
    level = changes.level
    if level is None:
        print(f"The kit names did not change. The kit version stays {current['kit_version']}.")
        return 0
    version = raise_version(current["kit_version"], level)
    _write_files(version, names, names_file, pyproject)
    print(describe_changes(changes, current["names"], names))
    print(
        f"Kit version {current['kit_version']} -> {version} (the {level} number went up).\n"
        "Now run `uv lock` in backend/, so uv.lock has the new kit version. The "
        "release notes of the next MEHO release need a `### Connector kit` section "
        f"that names kit version {version}."
    )
    return 0


def main() -> int:
    return write(build_names())


if __name__ == "__main__":
    sys.exit(main())
