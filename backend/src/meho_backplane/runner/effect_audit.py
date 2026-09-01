# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runner-side tamper-evident store-and-forward effect audit (#2901, #3193).

Mechanism 4 of the composed satellite write-path gate (design
``docs/research/2901-satellite-write-path.md`` §3, decision
``docs/decisions/satellite-write-path.md``). True synchronous-at-centre audit
of a remote **write** effect is structurally impossible — the executing side
(a satellite runner) has no Postgres and the mutation is off-net — so
v0.1-spec §6 cannot hold for the effect. This module is the runner half of the
consciously-recorded §6 exception: instead of pretending to audit synchronously,
the runner keeps a local **hash-chained** record of every write effect it
attempts and forwards it on next contact, where the centre ingests and verifies
the chain (:mod:`meho_backplane.gateway.effect_ingest`).

What the chain buys
-------------------

* **Tamper evidence in transit.** Each record's ``record_hash`` folds in the
  previous record's hash, so altering any forwarded record's body (or its link)
  breaks the chain at ingest.
* **Gap detection.** The per-runner ``seq`` is strictly monotonic; a dropped or
  suppressed record leaves a hole the centre detects (a compromised runner that
  executes-but-omits a record cannot hide the omission — the chain no longer
  links).
* **Non-repudiation anchor.** Each record references the signed work item's
  Ed25519 ``signature`` (#3189), binding the recorded effect to the exact
  centrally-authorised capability.

What it does **not** buy (the acknowledged residual): a fully compromised runner
holds its own genesis and can fabricate a *self-consistent* alternate chain from
scratch. Tamper evidence catches transit tampering and dropped records, **not** a
lying edge — that residual is bounded by the composition (allowlist x credential
TTL) and by the centre's un-reported-mint alarm, never by this record alone.

DB-free by construction
-----------------------

Like :mod:`meho_backplane.runner.satellite_tier` and
:mod:`meho_backplane.runner.work_item_signing`, this module imports only the
standard library + pydantic, so it runs on the DB-free runner **and** is
imported verbatim by the central ingest verifier (one hashing definition, both
ends). The chain persists to disk (mould:
:class:`meho_backplane.runner.spool.ResultSpool`) so a runner restart continues
the same chain rather than rewinding ``seq`` to zero — a rewind would read as a
gap at the centre.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

__all__ = [
    "GENESIS_PREV_HASH",
    "EffectAuditChain",
    "EffectAuditRecord",
    "EffectChainStateError",
    "EffectPhase",
    "canonical_record_body",
    "compute_record_hash",
]

_log = structlog.get_logger(__name__)

_TMP_SUFFIX = ".tmp"


class EffectChainStateError(Exception):
    """The on-disk chain head is corrupt / unrecoverable — refuse to guess."""


#: The ``prev_hash`` of the very first record on a runner's chain (``seq == 0``).
#: A fixed, well-known sentinel so the centre can verify a fresh runner's genesis
#: record without a prior head. 64 hex chars keeps it shape-compatible with a
#: real sha256 digest.
GENESIS_PREV_HASH: str = "0" * 64


class EffectPhase(StrEnum):
    """The two records the runner writes per executed write item.

    * :attr:`INTENT` — written **before** the mutation is attempted (the runner
      is about to execute a centrally-authorised write). Its presence without a
      matching :attr:`OUTCOME` is itself evidence (the runner crashed or was
      cut off mid-write).
    * :attr:`OUTCOME` — written **after** the handler returns, carrying the
      runner-level result status.
    """

    INTENT = "intent"
    OUTCOME = "outcome"


class EffectAuditRecord(BaseModel):
    """One tamper-evident link in a runner's effect-audit chain.

    Frozen wire model shared verbatim with the central ingest verifier. The
    hashed body is every field **except** ``record_hash`` (see
    :func:`canonical_record_body`); ``record_hash`` folds in ``prev_hash`` so the
    records form a chain the centre re-derives link by link.
    """

    model_config = ConfigDict(frozen=True)

    #: The runner principal **name** (wire identity — matches
    #: ``gateway_command.runner_id`` and ``RunnerResultBatch.runner_id``). The
    #: centre binds this to the authenticated runner at ingest, so one runner
    #: cannot extend another's chain.
    runner_id: str
    #: Strictly-monotonic per-runner sequence number (starts at 0). A gap is a
    #: dropped/suppressed record.
    seq: int
    phase: EffectPhase
    #: The delivered ``gateway_command.id`` this effect belongs to — the centre's
    #: link key to the mint audit row (``gateway_command.mint_audit_id``).
    command_id: str
    op_id: str
    params_hash: str
    #: The centre's base64 Ed25519 signature over the signed work item (#3189) —
    #: the non-repudiation anchor. ``None`` only if the capability was unsigned
    #: (which the write tier never mints).
    signature: str | None = None
    target_scope: str
    #: The runner-level outcome (``ok`` / ``error`` / ``refused``) — set on an
    #: :attr:`EffectPhase.OUTCOME` record, ``None`` on an :attr:`EffectPhase.INTENT`.
    outcome: str | None = None
    #: Runner-clock ISO-8601 timestamp. Advisory only — the centre never trusts a
    #: runner clock for a security decision; it is recorded for forensics.
    recorded_at: str
    #: The previous record's ``record_hash`` (:data:`GENESIS_PREV_HASH` for
    #: ``seq == 0``).
    prev_hash: str
    #: ``sha256(prev_hash + canonical_record_body(...))``.
    record_hash: str


def canonical_record_body(
    *,
    runner_id: str,
    seq: int,
    phase: EffectPhase | str,
    command_id: str,
    op_id: str,
    params_hash: str,
    signature: str | None,
    target_scope: str,
    outcome: str | None,
    recorded_at: str,
) -> str:
    """Deterministic JSON serialisation of the hashed body (record_hash excluded).

    ``sort_keys`` + compact separators make the encoding stable across Python
    versions and dict-insertion order, so the runner and the centre derive an
    identical string for an identical record.
    """
    body: dict[str, Any] = {
        "runner_id": runner_id,
        "seq": seq,
        "phase": str(phase),
        "command_id": command_id,
        "op_id": op_id,
        "params_hash": params_hash,
        "signature": signature,
        "target_scope": target_scope,
        "outcome": outcome,
        "recorded_at": recorded_at,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def compute_record_hash(prev_hash: str, canonical_body: str) -> str:
    """The chain hash: ``sha256(prev_hash + canonical_body)`` as hex.

    Folding ``prev_hash`` into every record's hash is what chains them: a change
    to any earlier record changes its ``record_hash``, which must equal the next
    record's ``prev_hash``, which is folded into *its* hash — so a single edit
    invalidates every downstream link.
    """
    return hashlib.sha256((prev_hash + canonical_body).encode("utf-8")).hexdigest()


class EffectAuditChain:
    """A runner's on-disk hash-chained effect audit log, drained on forward.

    Two persisted concerns in one directory:

    * ``head.json`` — ``{"last_seq": N, "last_hash": "..."}``, the chain head.
      Persisted **separately** from the forwardable records so draining
      forwarded records never rewinds the head. Atomic (tmp + :func:`os.replace`,
      the :class:`~meho_backplane.runner.spool.ResultSpool` mould).
    * ``<seq:012d>.json`` — one un-forwarded :class:`EffectAuditRecord` per file,
      lexically sorted == seq order. Deleted once forwarded.

    Not thread-safe: the runner tick loop is single-threaded, and a write item is
    executed to completion (intent → mutation → outcome) before the next, so the
    append sequence is naturally serial.
    """

    _HEAD_NAME = "head.json"

    def __init__(self, chain_dir: str | os.PathLike[str], *, runner_id: str) -> None:
        self._dir = Path(chain_dir)
        self._runner_id = runner_id

    # -- head persistence --------------------------------------------------

    def _head_path(self) -> Path:
        return self._dir / self._HEAD_NAME

    def _read_head(self) -> tuple[int, str]:
        """Return ``(last_seq, last_hash)``; the genesis head for a fresh chain."""
        try:
            raw = self._head_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return (-1, GENESIS_PREV_HASH)
        try:
            head = json.loads(raw)
            return (int(head["last_seq"]), str(head["last_hash"]))
        except (ValueError, KeyError, TypeError) as exc:
            # A corrupt head is unrecoverable — refusing to guess is safer than
            # silently rewinding the chain (which reads as a gap at the centre).
            raise EffectChainStateError(f"corrupt effect-audit head: {exc}") from exc

    def _write_head(self, last_seq: int, last_hash: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        final = self._head_path()
        tmp = final.with_suffix(final.suffix + _TMP_SUFFIX)
        tmp.write_text(
            json.dumps({"last_seq": last_seq, "last_hash": last_hash}),
            encoding="utf-8",
        )
        os.replace(tmp, final)

    # -- append -----------------------------------------------------------

    def _append(
        self,
        *,
        phase: EffectPhase,
        command_id: str,
        op_id: str,
        params_hash: str,
        signature: str | None,
        target_scope: str,
        outcome: str | None,
    ) -> EffectAuditRecord:
        last_seq, last_hash = self._read_head()
        seq = last_seq + 1
        recorded_at = datetime.now(UTC).isoformat()
        canonical = canonical_record_body(
            runner_id=self._runner_id,
            seq=seq,
            phase=phase,
            command_id=command_id,
            op_id=op_id,
            params_hash=params_hash,
            signature=signature,
            target_scope=target_scope,
            outcome=outcome,
            recorded_at=recorded_at,
        )
        record_hash = compute_record_hash(last_hash, canonical)
        record = EffectAuditRecord(
            runner_id=self._runner_id,
            seq=seq,
            phase=phase,
            command_id=command_id,
            op_id=op_id,
            params_hash=params_hash,
            signature=signature,
            target_scope=target_scope,
            outcome=outcome,
            recorded_at=recorded_at,
            prev_hash=last_hash,
            record_hash=record_hash,
        )
        # Persist the record first, then advance the head: a crash between the
        # two re-derives the same seq/hash on the next append (idempotent), never
        # a gap. The reverse order could advance the head past a record that was
        # never written — an unrecoverable hole.
        self._write_record(record)
        self._write_head(seq, record_hash)
        return record

    def record_intent(
        self,
        *,
        command_id: str,
        op_id: str,
        params_hash: str,
        signature: str | None,
        target_scope: str,
    ) -> EffectAuditRecord:
        """Append the pre-mutation :attr:`EffectPhase.INTENT` record."""
        return self._append(
            phase=EffectPhase.INTENT,
            command_id=command_id,
            op_id=op_id,
            params_hash=params_hash,
            signature=signature,
            target_scope=target_scope,
            outcome=None,
        )

    def record_outcome(
        self,
        *,
        command_id: str,
        op_id: str,
        params_hash: str,
        signature: str | None,
        target_scope: str,
        outcome: str,
    ) -> EffectAuditRecord:
        """Append the post-mutation :attr:`EffectPhase.OUTCOME` record."""
        return self._append(
            phase=EffectPhase.OUTCOME,
            command_id=command_id,
            op_id=op_id,
            params_hash=params_hash,
            signature=signature,
            target_scope=target_scope,
            outcome=outcome,
        )

    # -- forward ----------------------------------------------------------

    def unforwarded(self) -> list[EffectAuditRecord]:
        """Return the un-forwarded records, seq-ascending (oldest first)."""
        out: list[EffectAuditRecord] = []
        for path in self._record_files():
            try:
                out.append(EffectAuditRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                _log.warning("runner_effect_chain_unreadable", path=str(path), error=str(exc))
        return out

    def mark_forwarded(self, up_to_seq: int) -> None:
        """Delete forwarded record files with ``seq <= up_to_seq``.

        The head is untouched: forwarding drains the spool but the chain must
        keep climbing from where it left off, or a later record would re-use a
        seq the centre already ingested.
        """
        for path in self._record_files():
            if self._seq_of(path) <= up_to_seq:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    # -- internals --------------------------------------------------------

    def _record_path(self, seq: int) -> Path:
        return self._dir / f"{seq:012d}.json"

    def _write_record(self, record: EffectAuditRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        final = self._record_path(record.seq)
        tmp = final.with_suffix(final.suffix + _TMP_SUFFIX)
        tmp.write_text(record.model_dump_json(), encoding="utf-8")
        os.replace(tmp, final)

    def _record_files(self) -> list[Path]:
        if not self._dir.exists():
            return []
        return sorted(
            p
            for p in self._dir.glob("*.json")
            if p.name != self._HEAD_NAME and not p.name.endswith(_TMP_SUFFIX)
        )

    @staticmethod
    def _seq_of(path: Path) -> int:
        return int(path.stem)
