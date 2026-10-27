# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FortiOS versions: what the connector covers and what is tested.

* **Covered:** FortiOS 7.2 up to, but not including, 8.0. Detection refuses
  any other version, and so does every operation.
* **Tested:** the release lines in :data:`TESTED_RELEASE_LINES`. A read on a
  covered but untested line works and adds a warning to its answer. A change
  on such a line is refused (see
  :func:`~meho_backplane.connectors.fortios.write_checks.check_before_write`).

Every FortiOS answer carries the version in its wrapper (``"version":
"v7.4.4"``), so no extra call is needed to check it.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.connectors.fortios._errors import FortiOSVersionError

__all__ = [
    "COVERED_RANGE_TEXT",
    "TESTED_RELEASE_LINES",
    "is_tested",
    "parse_version",
    "require_covered",
    "version_warning",
]

#: The lowest covered version and the first version that is not covered.
_COVERED_FROM: Final[tuple[int, int]] = (7, 2)
_COVERED_BELOW: Final[tuple[int, int]] = (8, 0)

#: In words, for messages.
COVERED_RANGE_TEXT: Final[str] = "FortiOS 7.2 up to, but not including, 8.0"

#: Release lines whose answers the tests cover. Add a line only after its
#: answers are checked; changes are refused on every other line.
TESTED_RELEASE_LINES: Final[tuple[str, ...]] = ("7.2", "7.4", "7.6")

_VERSION: Final[re.Pattern[str]] = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` of ``"v7.4.4"`` or ``"7.4.4"``, else ``None``."""
    if not isinstance(text, str):
        return None
    match = _VERSION.match(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def require_covered(text: object) -> tuple[int, int, int]:
    """Return the parsed version, or raise :class:`FortiOSVersionError`."""
    version = parse_version(text)
    if version is None:
        raise FortiOSVersionError(
            f"the FortiOS answer has no version that MEHO can read ({text!r}); "
            f"this connector covers {COVERED_RANGE_TEXT}"
        )
    if not _COVERED_FROM <= version[:2] < _COVERED_BELOW:
        raise FortiOSVersionError(
            f"FortiOS {'.'.join(map(str, version))} is not supported; this connector "
            f"covers {COVERED_RANGE_TEXT}"
        )
    return version


def is_tested(version: tuple[int, int, int]) -> bool:
    """Return ``True`` when *version* is on a tested release line."""
    return f"{version[0]}.{version[1]}" in TESTED_RELEASE_LINES


def version_warning(text: object) -> str | None:
    """Check the version of an answer for a read: a warning text, or ``None``.

    Raises :class:`FortiOSVersionError` when the version is not covered.
    """
    version = require_covered(text)
    if is_tested(version):
        return None
    return (
        f"FortiOS {'.'.join(map(str, version))} is not on the tested list "
        f"({', '.join(TESTED_RELEASE_LINES)}). The read worked, but check the answer; "
        "changes are refused on this version."
    )
