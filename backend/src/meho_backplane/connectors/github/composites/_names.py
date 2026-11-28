# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one check for GitHub owner and repository names of the gh-rest composites.

An owner or repository name becomes a part of a REST path such as
``/repos/{owner}/{repo}``. :func:`check_github_name` makes sure it can only
ever be one plain path part. :func:`owner_repo_params_problem` is the params
check the dispatcher runs for every composite with an ``owner`` or ``repo``
parameter (registered in :mod:`._register`), so a bad name is refused as
``invalid_params`` before any request is sent.
"""

from __future__ import annotations

import re
from typing import Any, Final

from meho_backplane.operations._validate import params_problem

__all__ = ["check_github_name", "owner_repo_params_problem"]

# What GitHub allows in a user, organisation or repository name.
_GITHUB_NAME: Final = re.compile(r"[A-Za-z0-9._-]+")

# The parameters that hold an owner or a repository name.
_NAME_KEYS: Final = ("owner", "repo")


def check_github_name(value: object) -> str | None:
    """Return why ``value`` is not a safe owner or repository name, or ``None``.

    GitHub names hold letters, digits, ``.``, ``-`` and ``_`` only. The
    names ``.`` and ``..`` are refused because the URL encoding keeps dots,
    so they would point at another API path. ``.github`` is a valid name.
    """
    if not isinstance(value, str) or not value:
        return "name is empty"
    if value in (".", ".."):
        return f"name {value!r} is not allowed"
    if not _GITHUB_NAME.fullmatch(value):
        return f"name {value!r} may hold only letters, digits, '.', '-' and '_'"
    return None


def owner_repo_params_problem(params: dict[str, Any]) -> dict[str, Any] | None:
    """The params check of a composite with ``owner`` / ``repo``: one problem or ``None``.

    Checks each of the two keys that the call holds. The problem names the
    field, never the value.
    """
    for key in _NAME_KEYS:
        if key in params and check_github_name(params[key]) is not None:
            return params_problem(
                key,
                "a plain GitHub name: letters, digits, '.', '-' and '_', not '.' or '..'",
                f"Give the {key} name as GitHub shows it and send the call again. "
                "Nothing was sent.",
            )
    return None
