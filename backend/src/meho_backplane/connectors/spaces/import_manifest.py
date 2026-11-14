# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the manifest of ``spaces.import`` into plain, checked values.

The manifest is built at the site, offline. It lists the labs
(``environments``) and the people. A bad shape (not an object, a lab with no
name, a person with no issuer) is a clear error before anything is planned.
A *missing* owner is not a shape error: the plan lists it as a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Lab", "Manifest", "PersonKey", "parse_manifest"]

#: A person is the Keycloak issuer and subject, never an email address.
PersonKey = tuple[str, str]


@dataclass(frozen=True)
class Lab:
    """One lab of the manifest."""

    name: str
    targets: tuple[str, ...]
    owners: tuple[PersonKey, ...]
    users: tuple[PersonKey, ...]


@dataclass(frozen=True)
class Manifest:
    """The checked manifest: the labs and the people with an account."""

    labs: tuple[Lab, ...]
    people: tuple[PersonKey, ...]


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _person(raw: Any, where: str) -> PersonKey:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object with issuer and subject")
    return _text(raw.get("issuer"), f"{where}.issuer"), _text(
        raw.get("subject"), f"{where}.subject"
    )


def _persons(raw: Any, where: str) -> tuple[PersonKey, ...]:
    """A list of people, or one person, or nothing."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    return tuple(_person(item, f"{where}[{i}]") for i, item in enumerate(items))


def _lab(raw: Any, index: int) -> Lab:
    where = f"environments[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    targets_raw = raw.get("targets") or []
    if not isinstance(targets_raw, list):
        raise ValueError(f"{where}.targets must be a list of target names")
    return Lab(
        name=_text(raw.get("name"), f"{where}.name"),
        targets=tuple(_text(t, f"{where}.targets[{i}]") for i, t in enumerate(targets_raw)),
        owners=_persons(raw.get("owner"), f"{where}.owner"),
        users=_persons(raw.get("users"), f"{where}.users"),
    )


def parse_manifest(raw: Any, only: list[str] | None = None) -> Manifest:
    """Check the manifest and narrow it to the labs named in ``only``.

    The people list is always kept whole: it says which accounts exist.
    """
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object with environments and people")
    envs = raw.get("environments")
    if not isinstance(envs, list):
        raise ValueError("manifest.environments must be a list")
    people_raw = raw.get("people") or []
    if not isinstance(people_raw, list):
        raise ValueError("manifest.people must be a list")
    labs = tuple(_lab(item, i) for i, item in enumerate(envs))
    people = tuple(_person(item, f"people[{i}]") for i, item in enumerate(people_raw))
    if only is not None:
        wanted = [_text(n, "only[]") for n in only]
        known = {lab.name for lab in labs}
        missing = sorted(set(wanted) - known)
        if missing:
            raise ValueError(f"only names labs that are not in the manifest: {', '.join(missing)}")
        labs = tuple(lab for lab in labs if lab.name in set(wanted))
    return Manifest(labs=labs, people=people)
