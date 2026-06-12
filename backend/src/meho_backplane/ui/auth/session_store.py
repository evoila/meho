# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF session-store -- encrypted token custody + refresh rotation.

Initiative #337 (G10.0 Frontend chassis), Task #864 (T3). The
operator-console is locked to the Backend-for-Frontend (BFF) custody
shape per decision #11 (``docs/planning/v0.2-decisions.md``): the
browser holds an opaque session-cookie value, the real OAuth access +
refresh tokens live encrypted in the server-side ``web_session`` row.
This module owns the encryption / load / revoke / rotation surface
that the login-flow (Task #865) calls into.

The four entry points
---------------------

* :func:`create_session` -- write a row at login time. The caller
  (Task #865's ``/ui/auth/callback`` handler) passes the operator's
  ``sub``, tenant id, and the freshly-issued OAuth access + refresh
  tokens; this module Fernet-encrypts both tokens and inserts the
  row. The returned :class:`DecryptedSession` carries the row's
  ``id`` -- the value the BFF middleware sets as the
  ``meho_session`` cookie.

* :func:`load_session` -- hot-path lookup on every ``/ui/*`` request.
  Reads the row by PK, applies the revocation + expiry filter, and
  Fernet-decrypts both tokens. Returns ``None`` when the session is
  missing / revoked / past ``expires_at``. Updates ``last_seen_at``
  on a successful hit; the column is server-side-controlled (never
  client-supplied) so refreshing it from the request hot path is
  safe.

* :func:`load_session_for_update` -- side-effect-free variant of
  :func:`load_session` that row-locks the session via
  ``SELECT ... FOR UPDATE`` for the duration of the caller's
  transaction. The refresh orchestrator (G0.25 #1694,
  :mod:`meho_backplane.ui.auth.refresh`) uses it to serialise
  concurrent refresh attempts on the same session: the second
  request blocks on the lock, then observes the first request's
  rotated tokens and skips its own token-endpoint round-trip.

* :func:`revoke_session` -- soft-delete on logout (and on any
  operator-initiated revoke surface). Sets ``revoked_at = now()``;
  keeps the row visible-but-marked for audit-trail back-reference.
  Idempotent on already-revoked rows.

* :func:`rotate_refresh` -- the RFC 9700 § 4.14 contract. Called by
  the inline refresh orchestrator
  (:mod:`meho_backplane.ui.auth.refresh`, G0.25 #1694) when the
  access token has expired -- or is about to expire -- but the
  operator's session has not. Verifies the
  presented refresh token matches the stored one; on match the
  row's tokens are swapped to the new pair (one-time-use semantics
  -- the previous refresh value never decrypts to a valid
  comparand again). On mismatch *or* on a presented-refresh-after-
  revoked row, the session is revoked AND an ``audit_log`` row is
  written ``path='ui.session.refresh_replay'`` so the security
  surface (G2.4 / G8.1 replay-detection views) sees the event.

Encryption discipline
---------------------

Every token write passes through one
:class:`cryptography.fernet.Fernet` instance. The instance is
constructed lazily from :attr:`Settings.ui_session_encryption_key`
(a URL-safe base64-encoded 32-byte key resolved from a Vault-rendered
env var in production) and cached on the module so the same key
yields the same instance. Cache invalidation is keyed on the key
string -- :func:`get_settings.cache_clear` followed by a key swap
under test naturally produces a different cached instance.

Why Fernet (not raw AES-GCM)
----------------------------

Fernet is the
`cryptography-library-blessed authenticated-encryption envelope
<https://cryptography.io/en/latest/fernet/>`_ for the
"encrypt a blob at rest" use case. The envelope pins AES-128-CBC
+ HMAC-SHA256, includes a timestamp + random IV per token, and
ships a single ``Fernet`` API that is hard to misuse. Raw AES-GCM
is faster but the chassis pays one Fernet encrypt per login and
one Fernet decrypt per ``/ui/*`` request -- not a hot path -- and
the misuse surface (nonce reuse on raw GCM is catastrophic) is the
sort of footgun the chassis explicitly avoids by reaching for the
high-level primitive.

The stored ``bytes`` are the Fernet token in its native bytes form
(the same value :meth:`Fernet.encrypt` returns). Storing the bytes
(not the URL-safe base64 string it represents) keeps text-search
tooling (``psql \\d``, future grep-the-audit-export flows) from
surfacing what looks like an OAuth token in stable storage.

References
----------

* RFC 9700 § 4.14 -- OAuth 2.0 Security Best Current Practice on
  refresh token rotation:
  https://datatracker.ietf.org/doc/rfc9700/
* ``cryptography`` Fernet API:
  https://cryptography.io/en/latest/fernet/
* Chassis precedent (audit-row write):
  ``meho_backplane.topology.annotate._build_audit_row``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, WebSession
from meho_backplane.settings import get_settings

__all__ = [
    "DecryptedSession",
    "EncryptionKeyMissingError",
    "RefreshReplayError",
    "SessionStoreError",
    "create_session",
    "load_session",
    "load_session_for_update",
    "reset_fernet_cache_for_testing",
    "revoke_session",
    "rotate_refresh",
]


#: ``audit_log.path`` value used to record a refresh-token replay event.
#: The path is treated as a synthetic op-id so the audit-replay UI
#: surfaces this alongside other denied / replayed surfaces. Stable
#: identifier -- referenced by tests and by any future replay-detection
#: dashboard.
REFRESH_REPLAY_AUDIT_PATH: Final[str] = "ui.session.refresh_replay"

#: ``audit_log.method`` token for the replay event. Mirrors the
#: chassis pattern in :mod:`meho_backplane.topology.annotate` (which
#: writes a verb-style token for non-HTTP audit rows).
REFRESH_REPLAY_AUDIT_METHOD: Final[str] = "ROTATE"


class SessionStoreError(Exception):
    """Base class for backplane-side session-store failures.

    Catch this when a single error response shape ("could not
    handle the session-cookie path") is enough; subclasses carry the
    specific intent.
    """


class EncryptionKeyMissingError(SessionStoreError):
    """``UI_SESSION_ENCRYPTION_KEY`` is unset.

    Raised by :func:`_get_fernet` on the first session-store call
    when :attr:`Settings.ui_session_encryption_key` is empty.
    Production deploys render the key from a Vault secret into the
    pod's environment; dev/test pin a per-run key via the autouse
    conftest fixture. Surfacing this as an explicit exception (vs
    a cryptic ``ValueError`` from inside Fernet's constructor)
    gives the operator-facing error message a concrete remediation
    pointer.
    """


@dataclass(frozen=True)
class RefreshReplayError(SessionStoreError):
    """A refresh token was reused; the session has been revoked.

    Raised by :func:`rotate_refresh` when the presented refresh
    value does not match the stored one (or the session is already
    revoked). The session row is set to ``revoked_at = now()`` and
    an audit row is written before this exception propagates so
    the caller (Task #865's refresh handler) can map it to a 401 +
    cookie-clear without re-deriving any of the side effects.

    Attributes
    ----------
    session_id
        The revoked session's PK -- echo into the response so the
        operator's session-id can be correlated against the audit
        row (audit row's ``payload`` carries the same value).
    audit_id
        The ``audit_log.id`` of the freshly-written replay row.
        Lets the caller surface a clickable link to the audit
        viewer in the operator-facing error response.
    """

    session_id: uuid.UUID
    audit_id: uuid.UUID

    def __str__(self) -> str:
        return f"refresh-token replay on session {self.session_id}; audit_id={self.audit_id}"


@dataclass(frozen=True)
class DecryptedSession:
    """Plaintext view of a ``web_session`` row.

    The session-store module is the only consumer that ever sees
    plaintext tokens; this dataclass is the cross-call return shape.
    Frozen so callers cannot accidentally mutate a token in-place
    and write it back through the ORM (which would skip the
    Fernet encrypt path).

    The cookie value the browser holds is ``str(self.id)``.
    """

    id: uuid.UUID
    operator_sub: str
    tenant_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    access_token: str
    refresh_token: str
    last_seen_at: datetime


# ---------------------------------------------------------------------------
# Fernet plumbing
# ---------------------------------------------------------------------------


#: Module-level Fernet cache keyed on the configured key string. The
#: chassis-wide :func:`get_settings` cache + this one-tuple cache
#: jointly ensure the same key never reconstructs Fernet twice.
#: Tests that swap the key call :func:`reset_fernet_cache_for_testing`
#: so a per-test key materialises a fresh Fernet without restarting
#: the process.
_FERNET_CACHE: tuple[str, Fernet] | None = None


def _get_fernet() -> Fernet:
    """Return the process-wide Fernet bound to ``UI_SESSION_ENCRYPTION_KEY``.

    Lazy + cached: constructing :class:`Fernet` parses + validates
    the key (URL-safe base64 of exactly 32 bytes), so the first call
    pays the parse cost; every subsequent call returns the cached
    instance. The cache key is the raw key string -- if a test swaps
    the env var and clears :func:`get_settings.cache_clear`, the
    next call here observes a different key and rebuilds.

    Raises
    ------
    EncryptionKeyMissingError
        ``settings.ui_session_encryption_key`` is empty. Production
        misconfiguration; the message names the env var explicitly
        so the remediation does not require reading code.
    cryptography.fernet.InvalidToken
        The key is set but the Fernet constructor rejected it (not
        URL-safe-base64, wrong length). Fernet's own
        :class:`ValueError` propagates verbatim -- the chassis does
        not wrap it because the error message Fernet ships is
        already operator-actionable.
    """
    global _FERNET_CACHE
    key = get_settings().ui_session_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "UI_SESSION_ENCRYPTION_KEY is not set; the operator-console "
            "session store cannot encrypt tokens. Generate a key with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and surface it as "
            "the UI_SESSION_ENCRYPTION_KEY env var."
        )
    if _FERNET_CACHE is None or _FERNET_CACHE[0] != key:
        _FERNET_CACHE = (key, Fernet(key.encode("ascii")))
    return _FERNET_CACHE[1]


def reset_fernet_cache_for_testing() -> None:
    """Drop the Fernet cache. Test-only.

    Production never mutates the key under a running process. Tests
    that swap the key under :func:`pytest.MonkeyPatch.setenv` call
    this between cases so a subsequent
    :func:`get_settings.cache_clear` materialises a fresh Fernet
    rather than reusing the previous key's instance.
    """
    global _FERNET_CACHE
    _FERNET_CACHE = None


def _encrypt(value: str) -> bytes:
    """Fernet-encrypt *value* and return the ciphertext bytes."""
    return _get_fernet().encrypt(value.encode("utf-8"))


def _decrypt(ciphertext: bytes) -> str:
    """Fernet-decrypt *ciphertext* and return the plaintext string.

    Raises :class:`cryptography.fernet.InvalidToken` on any
    ciphertext that fails the HMAC check (tampered ciphertext,
    wrong key, encoded-in-a-different-context blob). Callers above
    the storage seam translate this into a generic
    :class:`SessionStoreError`; the storage seam itself does not
    paper over it -- corrupted ciphertext is a programmer error or
    a key-rotation bug and should not be swallowed silently.
    """
    return _get_fernet().decrypt(ciphertext).decode("utf-8")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    access_token: str,
    refresh_token: str,
    lifetime: timedelta,
) -> DecryptedSession:
    """Insert a fresh ``web_session`` row and return its decrypted view.

    Called by the BFF login-flow's ``/ui/auth/callback`` handler
    (Task #865) at the end of a successful OAuth code exchange.
    The caller passes the freshly-issued OAuth access + refresh
    tokens; this module is responsible for Fernet-encrypting them
    before insert.

    Parameters
    ----------
    session
        An open :class:`AsyncSession`. The caller controls the
        transaction shape; this function flushes (so ``id`` /
        ``created_at`` / ``last_seen_at`` are populated by the DB)
        but does **not** commit -- the caller's outer
        ``async with session.begin():`` block does.
    operator_sub
        Keycloak ``sub`` claim of the logged-in operator. Stored on
        the row verbatim (no transformation); ``AuditLog`` already
        treats this as the stable operator handle end-to-end.
    tenant_id
        The operator's active tenant at session-creation time
        (sourced from the JWT ``tenant_id`` claim by Task #865).
    access_token
        Plaintext OAuth access token from Keycloak's token
        endpoint. Encrypted before insert; this is the last point
        in the request lifecycle where the access token appears in
        plaintext on the DB side.
    refresh_token
        Plaintext OAuth refresh token. Encrypted before insert.
    lifetime
        How long this session is valid for. ``expires_at`` is set
        to ``datetime.now(UTC) + lifetime``. The caller computes
        the lifetime from the access-token's ``exp`` claim minus
        clock-skew margin so the session expires no later than the
        token it represents.

    Returns
    -------
    DecryptedSession
        Plaintext view of the inserted row -- the caller uses
        ``str(result.id)`` as the ``meho_session`` cookie value.
    """
    now = datetime.now(UTC)
    expires_at = now + lifetime
    row = WebSession(
        id=uuid.uuid4(),
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        created_at=now,
        expires_at=expires_at,
        access_token=_encrypt(access_token),
        refresh_token=_encrypt(refresh_token),
        last_seen_at=now,
        revoked_at=None,
    )
    session.add(row)
    await session.flush()
    return DecryptedSession(
        id=row.id,
        operator_sub=row.operator_sub,
        tenant_id=row.tenant_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        access_token=access_token,
        refresh_token=refresh_token,
        last_seen_at=row.last_seen_at,
    )


async def load_session(
    session: AsyncSession,
    cookie_id: uuid.UUID,
) -> DecryptedSession | None:
    """Load and decrypt a session by cookie value, or return ``None``.

    Hot-path lookup for the BFF session-middleware (Task #865).
    Returns ``None`` (not an exception) for any "no usable session"
    outcome so the middleware can redirect to ``/ui/auth/login``
    without distinguishing between "no such cookie", "revoked",
    "expired" at the policy layer. The audit / log instrumentation
    Task #865 layers on top can disambiguate by inspecting the row
    state directly if needed.

    Parameters
    ----------
    session
        Open :class:`AsyncSession`. The function flushes its
        ``last_seen_at`` update but does not commit; the caller's
        outer transaction owns the commit.
    cookie_id
        Parsed UUID from the ``meho_session`` cookie. Malformed
        cookie values are the caller's problem -- parse them with
        :func:`uuid.UUID` and catch :class:`ValueError` upstream.

    Returns
    -------
    DecryptedSession | None
        Decrypted plaintext view when the row is present, active
        (``revoked_at IS NULL``), and not past ``expires_at``.
        ``None`` otherwise.
    """
    row = await session.get(WebSession, cookie_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    # Naive-vs-aware-datetime portability: SQLite returns naive UTC
    # datetimes from a ``timestamptz`` column under aiosqlite; PG
    # returns aware. Coerce naive values to UTC-aware before
    # comparing so the inequality is well-defined on both dialects.
    expires_at = _coerce_utc(row.expires_at)
    if expires_at <= now:
        return None
    access_plaintext = _decrypt(row.access_token)
    refresh_plaintext = _decrypt(row.refresh_token)
    # Hot-path side effect: refresh ``last_seen_at`` so the future
    # idle-revocation sweep can see this session is alive. The
    # update is server-side-controlled (never a client-supplied
    # value), so this is safe to run on every successful load.
    row.last_seen_at = now
    # Sliding-session extension (G10.1-T3 #869): keep an actively-used
    # session alive past its login-time ``expires_at`` so a long display
    # (the broadcast wall-monitor) does not log out mid-stream. Bounded
    # by an absolute cap from ``created_at`` so a permanent display
    # cannot create an immortal session. Returns the (possibly extended)
    # ``expires_at`` and mutates the row in place.
    expires_at = _maybe_extend_expiry(row, created_at=_coerce_utc(row.created_at), now=now)
    await session.flush()
    return DecryptedSession(
        id=row.id,
        operator_sub=row.operator_sub,
        tenant_id=row.tenant_id,
        created_at=_coerce_utc(row.created_at),
        expires_at=expires_at,
        access_token=access_plaintext,
        refresh_token=refresh_plaintext,
        last_seen_at=row.last_seen_at,
    )


async def load_session_for_update(
    session: AsyncSession,
    cookie_id: uuid.UUID,
) -> DecryptedSession | None:
    """Load + decrypt a session under a ``SELECT ... FOR UPDATE`` row lock.

    The serialisation primitive for the inline token-refresh path
    (G0.25 #1694). Unlike :func:`load_session` this variant is
    **side-effect-free** -- no ``last_seen_at`` bump, no sliding
    extension -- because the caller is about to mutate the row through
    :func:`rotate_refresh` anyway and the refresh decision must be
    made against stable, lock-protected values.

    The lock holds for the remainder of the caller's transaction, so
    two concurrent refresh attempts on the same session serialise
    here: the second blocks until the first commits its rotation, then
    re-reads and finds the already-rotated tokens. SQLite caveat as in
    :func:`rotate_refresh` -- aiosqlite serialises at the connection /
    database level rather than per row, but the behavioural contract
    (exactly one winner; the loser observes the winner's write) holds
    on both engines.

    Returns
    -------
    DecryptedSession | None
        Decrypted plaintext view when the row is present, active
        (``revoked_at IS NULL``), and not past ``expires_at``.
        ``None`` otherwise -- same collapse contract as
        :func:`load_session`.
    """
    result = await session.execute(
        select(WebSession).where(WebSession.id == cookie_id).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    expires_at = _coerce_utc(row.expires_at)
    if expires_at <= now:
        return None
    return DecryptedSession(
        id=row.id,
        operator_sub=row.operator_sub,
        tenant_id=row.tenant_id,
        created_at=_coerce_utc(row.created_at),
        expires_at=expires_at,
        access_token=_decrypt(row.access_token),
        refresh_token=_decrypt(row.refresh_token),
        last_seen_at=row.last_seen_at,
    )


async def revoke_session(
    session: AsyncSession,
    cookie_id: uuid.UUID,
) -> None:
    """Soft-delete a session row (idempotent).

    Sets ``revoked_at = now()`` on the row. Idempotent: calling
    twice on the same row leaves ``revoked_at`` pinned to the
    first-call timestamp (no error, no overwrite). A missing row
    is a no-op -- the caller (logout handler in Task #865) does
    not need to distinguish "already-logged-out" from "never-
    logged-in" for the response shape.

    Parameters
    ----------
    session
        Open :class:`AsyncSession`. Flushes; the caller's outer
        transaction owns the commit.
    cookie_id
        The ``meho_session`` cookie UUID.
    """
    row = await session.get(WebSession, cookie_id)
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = datetime.now(UTC)
    await session.flush()


async def rotate_refresh(
    session: AsyncSession,
    cookie_id: uuid.UUID,
    *,
    presented_refresh: str,
    new_access_token: str,
    new_refresh_token: str,
    new_lifetime: timedelta | None = None,
) -> DecryptedSession:
    """Swap the row's tokens for a new pair after a successful refresh.

    The RFC 9700 § 4.14 "refresh-token rotation" contract:

    * On success (presented refresh matches stored refresh AND the
      session is still active), both columns are overwritten with
      the fresh ciphertext. The previous refresh value becomes
      un-presentable (it never decrypts to the new stored value).

    * On replay (presented refresh does not match, OR the session
      is already revoked, OR the session is past
      ``expires_at``), the session row is revoked AND an
      :class:`AuditLog` row is written
      ``path='ui.session.refresh_replay'`` so the security surface
      (G2.4 audit-export / G8.1 replay-detection) sees the event.
      A :class:`RefreshReplayError` carrying the freshly-allocated
      audit_id propagates to the caller (Task #865's refresh
      handler) which maps it to a 401 + cookie-clear.

    The replay branch also fires when the row is *missing*
    altogether -- the "no row at all" shape is treated as "the cookie
    was never valid, or the session has been hard-deleted by an
    out-of-band ops action" -- either way a 401 + cookie-clear is the
    right response and the audit row documents the attempt.

    Parameters
    ----------
    session
        Open :class:`AsyncSession` for the **happy path** (caller's
        outer transaction owns the commit; this function flushes).
        The replay path never writes through it -- the revoke +
        audit-row side effects commit on a dedicated session (see
        :func:`_commit_replay_side_effects`) so they survive the
        caller's rollback on the propagating
        :class:`RefreshReplayError`.
    cookie_id
        The session's UUID PK (the ``meho_session`` cookie value).
    presented_refresh
        The refresh token being presented back to this rotation
        call. Compared bytes-for-bytes against the decrypted stored
        value.
    new_access_token
        Fresh access token returned by Keycloak's token endpoint.
        Encrypted before write.
    new_refresh_token
        Fresh refresh token returned alongside ``new_access_token``.
        Encrypted before write.
    new_lifetime
        Optional session-lifetime extension derived from the fresh
        access token's ``expires_in`` minus the login-time clock-skew
        margin (G0.25 #1694). When provided, ``expires_at`` is pushed
        to ``now + new_lifetime``, clamped to the absolute ceiling
        ``created_at + ui_session_absolute_lifetime_seconds`` and
        never moved backwards -- the same monotonic + capped
        discipline :func:`_maybe_extend_expiry` applies to the
        sliding extension, so a refresh alone can never mint an
        immortal session. ``None`` (the default) leaves
        ``expires_at`` untouched, preserving the pre-#1694 contract
        for existing callers.

    Returns
    -------
    DecryptedSession
        Plaintext view of the rotated row -- callers do not need
        to re-decrypt.

    Raises
    ------
    RefreshReplayError
        ``presented_refresh`` does not match the stored value, or
        the session is already revoked / past expiry / missing.
        The session is revoked + an audit row is written
        **independently of the caller's transaction** before the
        exception propagates.
    """
    now = datetime.now(UTC)
    row = await _locked_row_or_replay(
        session,
        cookie_id,
        presented_refresh=presented_refresh,
        now=now,
    )
    return await _apply_rotation(
        session,
        row,
        new_access_token=new_access_token,
        new_refresh_token=new_refresh_token,
        new_lifetime=new_lifetime,
        now=now,
    )


async def _locked_row_or_replay(
    session: AsyncSession,
    cookie_id: uuid.UUID,
    *,
    presented_refresh: str,
    now: datetime,
) -> WebSession:
    """Row-lock the session and gate it through the replay defences.

    RFC 9700 § 4.14 demands single-use refresh tokens, which means
    two concurrent requests presenting the same valid refresh value
    must not BOTH pass the mismatch / revoked / expired gate and
    both successfully rotate. A naive ``session.get`` is a
    non-locking read; two transactions could each load the row,
    each check ``refresh_token == stored``, and each write a new
    ciphertext on top -- the second-writer wins but the
    second-presenter still received a "rotation OK" response,
    which is exactly the one-time-use breach the RFC closes.

    ``SELECT ... FOR UPDATE`` row-locks the session row for the
    duration of the caller's transaction. The second concurrent
    rotation blocks until the first commits; when it unblocks and
    re-reads, the stored refresh value has already rotated, so
    ``presented_refresh != stored_refresh`` and the replay branch
    fires -- exactly the one-time-use property the RFC mandates.

    SQLite caveat: aiosqlite's locking is database-level rather
    than row-level, so on the dev/test path the serialization
    comes from the connection-level write lock rather than a true
    row lock. The behavioural contract (exactly one rotation
    wins; the other surfaces as replay) holds on both engines.
    """
    result = await session.execute(
        select(WebSession).where(WebSession.id == cookie_id).with_for_update()
    )
    row = result.scalar_one_or_none()

    if row is None:
        # Cannot replay-revoke a row that does not exist, but the
        # event is still worth auditing -- a presented cookie with
        # no row could be a stolen cookie that we already
        # hard-deleted, or a fishing attempt against a randomly-
        # generated UUID. The audit row commits on its own session
        # so it survives the caller's exception-path rollback.
        audit_id = await _commit_replay_side_effects(
            session_id=cookie_id,
            operator_sub="<unknown>",
            tenant_id=None,
            reason="missing_session",
            now=now,
        )
        raise RefreshReplayError(session_id=cookie_id, audit_id=audit_id)

    stored_refresh = _decrypt(row.refresh_token)
    is_revoked = row.revoked_at is not None
    is_expired = _coerce_utc(row.expires_at) <= now
    is_mismatch = presented_refresh != stored_refresh

    if is_revoked or is_expired or is_mismatch:
        reason = (
            "already_revoked" if is_revoked else ("expired" if is_expired else "value_mismatch")
        )
        # Operator_sub / tenant_id captured here so the audit row
        # gets the correct attribution even though the dedicated
        # session below re-loads the row by id.
        audit_id = await _commit_replay_side_effects(
            session_id=cookie_id,
            operator_sub=row.operator_sub,
            tenant_id=row.tenant_id,
            reason=reason,
            now=now,
        )
        raise RefreshReplayError(session_id=cookie_id, audit_id=audit_id)

    return row


async def _apply_rotation(
    session: AsyncSession,
    row: WebSession,
    *,
    new_access_token: str,
    new_refresh_token: str,
    new_lifetime: timedelta | None,
    now: datetime,
) -> DecryptedSession:
    """Happy-path rotation: swap ciphertext, optionally extend expiry.

    Both columns are overwritten with the fresh ciphertext; the
    previous refresh value becomes un-presentable (one-time-use).
    ``new_lifetime`` -- when the caller is the inline refresh path
    (G0.25 #1694) -- pushes ``expires_at`` forward through
    :func:`_refresh_extended_expiry`'s monotonic + absolute-cap
    discipline.
    """
    row.access_token = _encrypt(new_access_token)
    row.refresh_token = _encrypt(new_refresh_token)
    row.last_seen_at = now
    effective_expires_at = _coerce_utc(row.expires_at)
    if new_lifetime is not None:
        extended = _refresh_extended_expiry(
            current=effective_expires_at,
            created_at=_coerce_utc(row.created_at),
            now=now,
            new_lifetime=new_lifetime,
        )
        if extended > effective_expires_at:
            row.expires_at = extended
            effective_expires_at = extended
    await session.flush()
    return DecryptedSession(
        id=row.id,
        operator_sub=row.operator_sub,
        tenant_id=row.tenant_id,
        created_at=_coerce_utc(row.created_at),
        expires_at=effective_expires_at,
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        last_seen_at=row.last_seen_at,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_extend_expiry(
    row: WebSession,
    *,
    created_at: datetime,
    now: datetime,
) -> datetime:
    """Slide a near-expiry session forward, bounded by an absolute cap.

    The long-display refresh mechanism for the broadcast wall-monitor
    (G10.1-T3 #869). The BFF session row's ``expires_at`` is set at
    login to roughly the access-token TTL (minutes to ~an hour), so a
    wall display left running for hours would cross ``expires_at``
    mid-display; the next SSE reconnect to ``/ui/broadcast/stream``
    would be 302-redirected to login, and the browser ``EventSource``
    permanently fails on the non-200 -- the feed dies silently.

    On every active ``/ui/*`` load (this function runs inside
    :func:`load_session`, after the still-valid check), when the row is
    within ``ui_session_sliding_extension_seconds`` of expiry, push
    ``expires_at`` out to ``now + sliding_window`` -- but never past the
    absolute ceiling ``created_at + ui_session_absolute_lifetime_seconds``.
    The pairing is the standard idle-vs-absolute session-timeout shape
    (OWASP ASVS v4 §3.3): the sliding window keeps an in-use session
    alive; the absolute cap guarantees a daily re-auth even for a
    permanently-displayed monitor.

    A ``sliding`` value of ``0`` disables the extension entirely (the
    session expires strictly at its login-time ``expires_at``). The
    mutation is server-side-controlled (clock + config only, never a
    client-supplied value), so running it on every successful load is
    safe.

    Returns the effective ``expires_at`` (extended or unchanged) and
    mutates ``row.expires_at`` in place when an extension applies. The
    caller's :func:`AsyncSession.flush` persists it.
    """
    settings = get_settings()
    sliding = settings.ui_session_sliding_extension_seconds
    if sliding <= 0:
        return _coerce_utc(row.expires_at)

    current = _coerce_utc(row.expires_at)
    # Absolute ceiling from session birth -- the extension can never
    # push expiry past this, so a permanent display still re-auths.
    ceiling = created_at + timedelta(seconds=settings.ui_session_absolute_lifetime_seconds)
    if current >= ceiling:
        # Already at (or past) the absolute cap; no further sliding.
        return current

    # Only slide when the session is within the sliding window of
    # expiry -- avoids a write on every single request for a freshly
    # logged-in session whose expiry is still far off.
    threshold = current - timedelta(seconds=sliding)
    if now < threshold:
        return current

    extended = min(now + timedelta(seconds=sliding), ceiling)
    # Monotonic guard: never move expiry backwards (a tiny sliding
    # window vs a long login TTL could otherwise shrink it).
    if extended <= current:
        return current
    row.expires_at = extended
    return extended


def _refresh_extended_expiry(
    *,
    current: datetime,
    created_at: datetime,
    now: datetime,
    new_lifetime: timedelta,
) -> datetime:
    """Compute the post-refresh ``expires_at``, capped and monotonic.

    The refresh-driven sibling of :func:`_maybe_extend_expiry`
    (G0.25 #1694): a successful token refresh extends the session to
    ``now + new_lifetime`` (the fresh token's ``expires_in`` minus the
    login-time margin), but

    * never past the absolute ceiling ``created_at +
      ui_session_absolute_lifetime_seconds`` -- chained refreshes
      cannot mint an immortal session (OWASP ASVS v4 § 3.3 absolute
      timeout), and
    * never backwards -- when the sliding extension (#869) already
      pushed ``expires_at`` beyond the fresh token's own lifetime,
      shrinking it would log out a wall display the sliding window
      deliberately keeps alive. The access token inside the row simply
      refreshes again on its next expiry.

    Returns the effective ``expires_at``; the caller decides whether
    to write it back.
    """
    settings = get_settings()
    ceiling = created_at + timedelta(seconds=settings.ui_session_absolute_lifetime_seconds)
    candidate = min(now + new_lifetime, ceiling)
    return candidate if candidate > current else current


def _coerce_utc(value: datetime) -> datetime:
    """Return *value* as a UTC-aware ``datetime``.

    SQLite (the dev/test path via aiosqlite) returns naive UTC
    values from ``timestamptz`` columns; PostgreSQL returns
    timezone-aware values. The chassis-wide convention is "naive
    means UTC" (every write goes through ``datetime.now(UTC)``);
    this helper aligns the read-side to that contract so
    inequality comparisons against :func:`datetime.now` (always
    aware in this module) are dialect-portable.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _build_replay_audit_row(
    *,
    session_id: uuid.UUID,
    operator_sub: str,
    tenant_id: uuid.UUID | None,
    reason: str,
    now: datetime,
) -> AuditLog:
    """Construct one ``AuditLog`` row for a refresh-token replay event.

    Mirrors the column shape :mod:`meho_backplane.topology.annotate`
    uses (``method`` as verb token, ``path`` as op-id).
    Pre-allocates ``audit_id`` so :class:`RefreshReplayError` can
    surface the row id without re-querying.

    ``status_code=401`` rather than 200 because the audit row
    captures a refused-credential event -- the audit-replay
    surface filters by status to surface the security-relevant
    rows.
    """
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=now,
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        method=REFRESH_REPLAY_AUDIT_METHOD,
        path=REFRESH_REPLAY_AUDIT_PATH,
        status_code=401,
        request_id=None,
        duration_ms=Decimal("0.00"),
        payload={
            "session_id": str(session_id),
            "reason": reason,
        },
    )


async def _commit_replay_side_effects(
    *,
    session_id: uuid.UUID,
    operator_sub: str,
    tenant_id: uuid.UUID | None,
    reason: str,
    now: datetime,
) -> uuid.UUID:
    """Revoke the session + write the audit row on a dedicated session.

    The replay branch in :func:`rotate_refresh` must persist its
    side effects (revoke + audit row) **independently** of the
    caller's transaction. The caller has typically wrapped the
    ``rotate_refresh`` call in ``async with session.begin():`` --
    if we wrote through the caller's session, the propagating
    :class:`RefreshReplayError` would unwind that ``begin()`` and
    roll the writes back, breaking the AC's "replay revokes the
    session AND writes an audit row" contract.

    We open a fresh session from the chassis-wide
    :func:`get_sessionmaker`, do the revoke + insert, commit, and
    return the new audit row's id. The caller's session is
    untouched (no flush / no commit / no rollback into it). When
    the session is genuinely missing (``operator_sub == "<unknown>"``
    sentinel), the function skips the revoke step (no row to
    revoke) and writes the audit row only.
    """
    sessionmaker = get_sessionmaker()
    audit_row = _build_replay_audit_row(
        session_id=session_id,
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        reason=reason,
        now=now,
    )
    async with sessionmaker() as dedicated, dedicated.begin():
        # Revoke step: a missing-row reason has nothing to revoke,
        # but the other three reasons all reference a row that
        # exists in the session-store and may need its
        # ``revoked_at`` flipped to ``now``. We re-fetch by id
        # under the dedicated session so the row is attached to
        # the transaction we are about to commit.
        if reason != "missing_session":
            existing = await dedicated.get(WebSession, session_id)
            if existing is not None and existing.revoked_at is None:
                existing.revoked_at = now
        dedicated.add(audit_row)
    return audit_row.id
