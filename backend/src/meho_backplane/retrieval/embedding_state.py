# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The record of which embedding model made the stored vectors.

The table ``embedding_state`` has one row. At every start MEHO compares it
with ``RETRIEVAL_EMBEDDING_MODEL`` (:func:`check_embedding_model_at_start`).
When they differ, it logs ``embedding_model_mismatch`` with both names and
keeps starting: stored vectors from another model make searches worse, but
they do not break MEHO. ``python -m meho_backplane.retrieval.reembed``
embeds every stored vector again and then calls :func:`write_embedding_state`.

On a mismatch the start also adds the readiness check ``embedding_model``
(:func:`embedding_model_probe`), which names both models and never fails
the readiness. When the models match, no check is added, so the health
answer (``/ready`` and the ``meho_status`` tool) stays as it was.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EmbeddingState
from meho_backplane.health import ProbeResult
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

__all__ = [
    "check_embedding_model_at_start",
    "embedding_model_probe",
    "read_embedding_state",
    "write_embedding_state",
]

_STATE_ROW_ID: Final[int] = 1

#: What the last start check found; read by :func:`embedding_model_probe`.
_detail: str = "not checked"


async def read_embedding_state(session: AsyncSession) -> tuple[str, int] | None:
    """Return ``(model, dimension)`` of the stored record, or ``None`` when it has no row."""
    row = (
        await session.execute(
            select(EmbeddingState.model, EmbeddingState.dimension).where(
                EmbeddingState.id == _STATE_ROW_ID
            )
        )
    ).one_or_none()
    return None if row is None else (row.model, row.dimension)


async def write_embedding_state(session: AsyncSession, model: str) -> None:
    """Store *model* as the model of the stored vectors. The caller commits."""
    row = await session.get(EmbeddingState, _STATE_ROW_ID)
    now = datetime.now(UTC)
    if row is None:
        session.add(
            EmbeddingState(
                id=_STATE_ROW_ID, model=model, dimension=EMBEDDING_DIMENSION, updated_at=now
            )
        )
    else:
        row.model = model
        row.dimension = EMBEDDING_DIMENSION
        row.updated_at = now


async def check_embedding_model_at_start() -> bool:
    """Compare the record with the set model; log a warning on a difference.

    Return ``True`` only when the record names another model. Never
    raises and never loads a model: a missing table or a database error
    is logged and the start goes on.
    """
    global _detail
    log = structlog.get_logger()
    try:
        configured = get_settings().retrieval_embedding_model
        async with get_sessionmaker()() as session:
            stored = await read_embedding_state(session)
    except Exception as exc:
        _detail = "record unreadable"
        log.warning("embedding_state_unreadable", error_class=type(exc).__name__)
        return False
    if stored is None:
        _detail = f"model={configured} no record"
    elif stored[0] != configured:
        _detail = (
            f"stored vectors come from model {stored[0]}, but the set model is {configured}; "
            "run python -m meho_backplane.retrieval.reembed"
        )
        log.warning(
            "embedding_model_mismatch",
            stored_model=stored[0],
            configured_model=configured,
            action="run python -m meho_backplane.retrieval.reembed",
        )
        return True
    else:
        _detail = f"model={configured}"
    return False


def embedding_model_probe() -> ProbeResult:
    """Readiness detail of a mismatch found at start. Always ``ok``."""
    return ProbeResult(name="embedding_model", ok=True, detail=_detail)
