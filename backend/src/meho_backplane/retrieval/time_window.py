# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Time limits for a search: when an entry was confirmed or last changed.

:func:`~meho_backplane.retrieval.retriever.retrieve` takes one
:class:`TimeWindow` and checks it inside the candidate SQL, before each
signal's ``LIMIT``. So a row outside the window never takes a candidate
slot from a row inside it.

* ``confirmed_after`` / ``confirmed_before`` check the ``confirmed_at``
  date of the lesson header (:mod:`meho_backplane.lesson_header`). A row
  without a readable ``confirmed_at`` never matches a confirmed limit.
* ``updated_after`` / ``updated_before`` check the row's ``updated_at``
  column: the time of the last write.

Both ends are inclusive. A limit that is ``None`` is not checked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["TimeWindow", "parse_time_limit"]


class TimeWindow(BaseModel):
    """Inclusive time limits on ``confirmed_at`` and ``updated_at``.

    Every value is timezone-aware. A window where an ``after`` is later
    than its ``before`` raises :class:`ValueError`, because it can match
    nothing and is most likely a mistake.
    """

    model_config = ConfigDict(frozen=True)

    confirmed_after: datetime | None = None
    confirmed_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> TimeWindow:
        for name, value in self:
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must carry a time zone")
        for field in ("confirmed", "updated"):
            after = getattr(self, f"{field}_after")
            before = getattr(self, f"{field}_before")
            if after is not None and before is not None and after > before:
                raise ValueError(f"{field}_after is later than {field}_before")
        return self

    def is_empty(self) -> bool:
        """Say whether no limit is set."""
        return all(value is None for _, value in self)

    def binds(self) -> dict[str, datetime | None]:
        """Return the SQL bind values of the window."""
        return dict(self)


def parse_time_limit(name: str, value: object) -> datetime | None:
    """Read one time limit given as ISO 8601 text; ``None`` stays ``None``.

    A date alone (``2026-11-03``) means midnight UTC of that day. A time
    without an offset is read as UTC, like the header dates.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 date or time text; got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{name} must be an ISO 8601 date or time (for example 2026-11-03); got {value!r}"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
