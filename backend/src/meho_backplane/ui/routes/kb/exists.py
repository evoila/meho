# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's answer when a save or an upload hits a slug that exists.

The console can only create entries (editing comes later). A save or
an upload for a slug that exists is refused and changes nothing. The
text names the current revision; the template adds a link to the entry.
"""

from __future__ import annotations

from meho_backplane.kb.stale_revision import KbStaleRevisionError

__all__ = ["exists_message"]


def exists_message(exc: KbStaleRevisionError) -> str:
    """Return the plain text that tells the user the entry exists and was not changed."""
    detail = exc.detail
    number = detail.current_revision_number
    revision = f" (current revision {number})" if number is not None else ""
    return (
        f"The entry {detail.slug!r} exists already{revision}. "
        "Nothing was changed. Open the entry to read it."
    )
