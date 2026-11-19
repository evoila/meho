# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The report of the weekly knowledge upkeep (:mod:`meho_backplane.lessons.upkeep`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from meho_backplane.lessons.metrics import LearningMetrics

__all__ = ["MergeForReview", "MergedEntry", "RefreshedEntry", "ReviewCandidate", "UpkeepReport"]


class MergedEntry(BaseModel):
    """A knowledge entry marked ``replaced`` because *into* has the same body."""

    model_config = ConfigDict(frozen=True)

    slug: str
    into: str


class MergeForReview(BaseModel):
    """Two knowledge entries with the same body that the job did not merge.

    The job merges a pair only when the kept entry is ``confirmed`` or
    both entries have the same creator. Any other pair waits here for a
    reviewer: *slug* has the same body as *same_as*, the entry the job
    would keep. Neither entry is changed.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    same_as: str


class RefreshedEntry(BaseModel):
    """A confirmed entry whose ``confirmed_at`` the job set again."""

    model_config = ConfigDict(frozen=True)

    store: str
    slug: str
    confirmed_at: datetime


class ReviewCandidate(BaseModel):
    """A used candidate, for a reviewer to confirm or not.

    ``sessions`` counts the different sessions that used it.
    ``search_sessions`` counts those that used it after a search, and
    ``recall_sessions`` those that used it after MEHO showed it before an
    operation. A session can count in both.
    """

    model_config = ConfigDict(frozen=True)

    store: str
    slug: str
    sessions: int
    targets: int
    search_sessions: int
    recall_sessions: int


class UpkeepReport(BaseModel):
    """What one run did in one tenant, and the tenant's learning numbers."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    since: datetime
    until: datetime
    merged_count: int = 0
    retired_count: int = 0
    refreshed_count: int = 0
    for_review_count: int = 0
    merge_for_review_count: int = 0
    merged: list[MergedEntry] = []
    retired: list[str] = []
    refreshed: list[RefreshedEntry] = []
    for_review: list[ReviewCandidate] = []
    merge_for_review: list[MergeForReview] = []
    numbers: LearningMetrics | None = None
