# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the passkey touch challenge of an approval (#6214).

* The binding hash is stable for the same input and changes when any one
  field changes.
* A row is used once, ends after its valid time, and only its own person
  in its own tenant can use it.
* A request that changed after the row was made is refused.
* Two approves that race for one row: exactly one wins.
* The audit payload of an approve without a touch is as before.
* The daily clean-up deletes only old rows that were never used.

The real PostgreSQL race is in
``tests/integration/test_approval_presence_race_pg.py``.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.approval_sign_in import ApprovalAuth, TouchBinding, TouchPresence
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalPresenceChallenge,
    ApprovalRequest,
    ApprovalRequestStatus,
    Tenant,
)
from meho_backplane.loop_schedule import FIRST_RUN_DELAY_SECONDS
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_presence import (
    BINDING_ALGORITHM,
    CLEANUP_INTERVAL_SECONDS,
    MATCH_CODE_ALPHABET,
    TouchChallengeError,
    _binding_still_holds,
    _cleanup_loop,
    approval_card_text,
    compute_binding_hash,
    compute_card_hash,
    delete_unused_challenges,
    match_code,
    start_touch,
    touch_binding,
    use_touch,
)
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    create_pending_request,
)
from meho_backplane.settings import get_settings

_MODULE = "meho_backplane.operations.approval_presence"

_PARAMS: dict[str, Any] = {"name": "vm-01"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _person(tenant_id: uuid.UUID, sub: str = "person-approver") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _agent(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="agent-requester",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _seed_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    slug = f"touch-{tenant_id.hex[:8]}"
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _park(tenant_id: uuid.UUID) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_agent(tenant_id),
            connector_id="vmware-rest-9.0",
            op_id="vm.delete",
            target=None,
            params=dict(_PARAMS),
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"safety_level": "destructive", "params_echo": dict(_PARAMS)},
        )
        await session.commit()
        return request.id


async def _start(
    tenant_id: uuid.UUID, request_ids: list[uuid.UUID], **kwargs: Any
) -> ApprovalPresenceChallenge:
    async with get_sessionmaker()() as session:
        challenge = await start_touch(
            session,
            operator=_person(tenant_id),
            request_ids=request_ids,
            channel="web",
            session_kind="console",
            **kwargs,
        )
        await session.commit()
        return challenge


async def _use(
    challenge: ApprovalPresenceChallenge,
    operator: Operator,
    *,
    nonce: str | None = None,
    now: datetime | None = None,
) -> ApprovalPresenceChallenge:
    async with get_sessionmaker()() as session:
        used = await use_touch(
            session,
            challenge.id,
            operator=operator,
            nonce=challenge.binding_hash if nonce is None else nonce,
            now=now,
        )
        await session.commit()
        return used


async def _used_at(challenge_id: uuid.UUID) -> datetime | None:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalPresenceChallenge, challenge_id)
        assert row is not None
        return row.used_at


# ---------------------------------------------------------------------------
# The binding hash, the card hash and the match code
# ---------------------------------------------------------------------------


def _request(rng: random.Random) -> ApprovalRequest:
    def text() -> str:
        return f"{rng.getrandbits(64):016x}"

    return ApprovalRequest(
        id=uuid.UUID(int=rng.getrandbits(128)),
        connector_id=text(),
        op_id=text(),
        target_id=uuid.UUID(int=rng.getrandbits(128)),
        params_hash=text(),
        preview_hash=text(),
        target_hash=text(),
        release_id=text(),
        proposed_effect={"safety_level": "dangerous", "params_echo": {"k": text()}},
    )


_REQUEST_FIELDS = ("id", "params_hash", "preview_hash", "target_hash", "release_id")
_CARD_FIELDS = ("connector_id", "op_id", "target_id", "proposed_effect")


def _changed(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return uuid.uuid4()
    if isinstance(value, dict):
        return {**value, "params_echo": {"k": "other"}}
    return f"{value}-changed"


@pytest.mark.parametrize("seed", range(25))
def test_binding_hash_is_stable_and_changes_with_each_field(seed: int) -> None:
    rng = random.Random(seed)
    tenant_id = uuid.UUID(int=rng.getrandbits(128))
    requests = [_request(rng) for _ in range(rng.randint(1, 3))]
    server_random = f"{rng.getrandbits(256):064x}"
    card = compute_card_hash(requests)

    def binding(**overrides: Any) -> str:
        args: dict[str, Any] = {
            "tenant_id": tenant_id,
            "requests": requests,
            "card_hash": card,
            "server_random": server_random,
        }
        args.update(overrides)
        return compute_binding_hash(
            args["tenant_id"],
            args["requests"],
            card_hash=args["card_hash"],
            server_random=args["server_random"],
        )

    base = binding()
    assert base == binding()
    assert len(base) == 64
    assert binding(tenant_id=uuid.uuid4()) != base
    assert binding(card_hash=card[::-1]) != base
    assert binding(server_random=f"{rng.getrandbits(256):064x}") != base
    if len(requests) > 1:
        assert binding(requests=list(reversed(requests))) != base

    index = rng.randrange(len(requests))
    for field in _REQUEST_FIELDS:
        original = getattr(requests[index], field)
        setattr(requests[index], field, _changed(original))
        assert binding() != base, field
        setattr(requests[index], field, original)
    for field in _CARD_FIELDS:
        original = getattr(requests[index], field)
        setattr(requests[index], field, _changed(original))
        assert compute_card_hash(requests) != card, field
        setattr(requests[index], field, original)
    assert binding() == base


def test_a_missing_hash_and_an_empty_one_differ() -> None:
    rng = random.Random(7)
    request = _request(rng)
    tenant_id = uuid.uuid4()
    request.preview_hash = None
    without = compute_binding_hash(tenant_id, [request], card_hash="c", server_random="r")
    request.preview_hash = ""
    assert compute_binding_hash(tenant_id, [request], card_hash="c", server_random="r") != without


def test_card_text_names_operation_target_risk_and_effect() -> None:
    request = _request(random.Random(1))
    text = approval_card_text([request])
    assert request.op_id in text
    assert request.connector_id in text
    assert str(request.target_id) in text
    assert '"risk_level":"dangerous"' in text
    assert '"params_echo"' in text


@pytest.mark.parametrize("seed", range(20))
def test_match_code_has_six_characters_without_look_alikes(seed: int) -> None:
    binding = f"{random.Random(seed).getrandbits(256):064x}"
    code = match_code(binding)
    assert code == match_code(binding)
    assert len(code) == 6
    assert set(code) <= set(MATCH_CODE_ALPHABET)
    assert not set(code) & set("01IO")


def test_match_code_alphabet_has_32_distinct_characters() -> None:
    assert len(set(MATCH_CODE_ALPHABET)) == len(MATCH_CODE_ALPHABET) == 32


# ---------------------------------------------------------------------------
# start_touch
# ---------------------------------------------------------------------------


async def test_start_touch_makes_one_row_that_names_the_request() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    now = datetime.now(UTC)

    challenge = await _start(tenant_id, [request_id], now=now)

    assert challenge.tenant_id == tenant_id
    assert challenge.approver_sub == "person-approver"
    assert challenge.request_ids == [str(request_id)]
    assert challenge.channel == "web"
    assert challenge.session_kind == "console"
    assert challenge.used_at is None
    assert challenge.expires_at == now + timedelta(seconds=120)
    assert len(challenge.server_random) == 64
    assert challenge.match_code == match_code(challenge.binding_hash)
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        assert challenge.card_hash == compute_card_hash([request])
        assert challenge.binding_hash == compute_binding_hash(
            tenant_id,
            [request],
            card_hash=challenge.card_hash,
            server_random=challenge.server_random,
        )


async def test_two_touches_for_one_request_get_different_hashes() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    first = await _start(tenant_id, [request_id])
    second = await _start(tenant_id, [request_id])
    assert first.card_hash == second.card_hash
    assert first.binding_hash != second.binding_hash


async def test_valid_time_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPROVAL_PRESENCE_TOUCH_VALID_SECONDS", "300")
    get_settings.cache_clear()
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    now = datetime.now(UTC)
    challenge = await _start(tenant_id, [request_id], now=now)
    assert challenge.expires_at == now + timedelta(seconds=300)


async def test_start_touch_refuses_a_request_of_another_tenant() -> None:
    tenant_id = await _seed_tenant()
    other_tenant = await _seed_tenant()
    request_id = await _park(other_tenant)
    with pytest.raises(ApprovalNotFoundError):
        await _start(tenant_id, [request_id])


async def test_start_touch_refuses_a_decided_request() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(status=ApprovalRequestStatus.REJECTED.value)
        )
        await session.commit()
    with pytest.raises(ApprovalRequestAlreadyDecidedError):
        await _start(tenant_id, [request_id])


@pytest.mark.parametrize(
    ("ids", "channel"), [("empty", "web"), ("repeat", "web"), ("one", "phone")]
)
async def test_start_touch_refuses_bad_input(ids: str, channel: str) -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    request_ids = {"empty": [], "repeat": [request_id, request_id], "one": [request_id]}[ids]
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError):
            await start_touch(
                session,
                operator=_person(tenant_id),
                request_ids=request_ids,
                channel=channel,
                session_kind="console",
            )


# ---------------------------------------------------------------------------
# use_touch
# ---------------------------------------------------------------------------


async def test_use_touch_marks_the_row_used_once() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])

    used = await _use(challenge, _person(tenant_id))
    assert used.used_at is not None
    assert await _used_at(challenge.id) is not None

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id))
    assert refused.value.reason == "used"
    assert str(refused.value) == (
        "This passkey touch was already used. Click Approve again to start a new one."
    )


async def test_use_touch_refuses_an_expired_row() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    now = datetime.now(UTC)
    challenge = await _start(tenant_id, [request_id], now=now)

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id), now=now + timedelta(seconds=120))
    assert refused.value.reason == "expired"
    assert await _used_at(challenge.id) is None

    used = await _use(challenge, _person(tenant_id), now=now + timedelta(seconds=119))
    assert used.used_at is not None


async def test_use_touch_refuses_another_person() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id, sub="someone-else"))
    assert refused.value.reason == "not_found"
    assert await _used_at(challenge.id) is None


async def test_use_touch_refuses_another_tenant() -> None:
    tenant_id = await _seed_tenant()
    other_tenant = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(other_tenant))
    assert refused.value.reason == "not_found"
    assert await _used_at(challenge.id) is None


async def test_use_touch_refuses_an_unknown_row() -> None:
    tenant_id = await _seed_tenant()
    async with get_sessionmaker()() as session:
        with pytest.raises(TouchChallengeError) as refused:
            await use_touch(session, uuid.uuid4(), operator=_person(tenant_id), nonce="x")
    assert refused.value.reason == "not_found"


async def test_use_touch_refuses_a_nonce_of_another_touch() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])
    other = await _start(tenant_id, [request_id])

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id), nonce=other.binding_hash)
    assert refused.value.reason == "nonce_mismatch"
    assert await _used_at(challenge.id) is None


@pytest.mark.parametrize(
    "change",
    [
        {"params_hash": "0" * 64},
        {"preview_hash": "1" * 64},
        {"target_hash": "2" * 64},
        {"release_id": "another-release"},
        {"proposed_effect": {"safety_level": "destructive", "params_echo": {"name": "vm-02"}}},
    ],
)
async def test_use_touch_refuses_a_request_that_changed(change: dict[str, Any]) -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest).where(ApprovalRequest.id == request_id).values(**change)
        )
        await session.commit()

    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id))
    assert refused.value.reason == "request_changed"
    assert str(refused.value) == (
        "The request changed after you started the passkey touch. "
        "Click Approve again to see it as it is now."
    )
    assert await _used_at(challenge.id) is None


async def test_use_touch_covers_every_request_of_a_list() -> None:
    tenant_id = await _seed_tenant()
    first = await _park(tenant_id)
    second = await _park(tenant_id)
    challenge = await _start(tenant_id, [first, second])
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ApprovalRequest).where(ApprovalRequest.id == second).values(params_hash="f" * 64)
        )
        await session.commit()
    with pytest.raises(TouchChallengeError) as refused:
        await _use(challenge, _person(tenant_id))
    assert refused.value.reason == "request_changed"


async def test_use_touch_takes_the_approval_lock_on_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])
    calls: list[tuple[str, str]] = []

    async def _spy(session: AsyncSession, scope: str, key: str) -> None:
        calls.append((scope, key))

    monkeypatch.setattr(f"{_MODULE}.take_approval_lock", _spy)
    await _use(challenge, _person(tenant_id))
    assert calls == [("approval-presence-touch", str(challenge.id))]


async def test_two_racing_uses_of_one_row_have_exactly_one_winner() -> None:
    """Both approves pass every check; the mark lets only one through."""
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])
    both_checked = asyncio.Barrier(2)
    real_check = _binding_still_holds

    async def _check_then_wait(session: AsyncSession, row: ApprovalPresenceChallenge) -> bool:
        holds = await real_check(session, row)
        await both_checked.wait()
        return holds

    async def _attempt(session: AsyncSession) -> str:
        try:
            await use_touch(
                session, challenge.id, operator=_person(tenant_id), nonce=challenge.binding_hash
            )
        except TouchChallengeError as refused:
            await session.rollback()
            return refused.reason
        await session.commit()
        return "ok"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(f"{_MODULE}._binding_still_holds", _check_then_wait)
        async with get_sessionmaker()() as first, get_sessionmaker()() as second:
            outcomes = await asyncio.gather(_attempt(first), _attempt(second))

    assert sorted(outcomes) == ["ok", "used"]
    assert await _used_at(challenge.id) is not None


async def test_touch_binding_names_the_row() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    challenge = await _start(tenant_id, [request_id])
    binding = touch_binding(challenge)
    assert binding == TouchBinding(
        algorithm=BINDING_ALGORITHM,
        hash=challenge.binding_hash,
        request_ids=(str(request_id),),
        card_hash=challenge.card_hash,
        challenge_id=str(challenge.id),
    )


# ---------------------------------------------------------------------------
# The audit payload
# ---------------------------------------------------------------------------


def test_audit_payload_without_a_touch_is_as_before() -> None:
    auth = ApprovalAuth(channel="rest", method="fresh_sign_in", auth_time=1700, sid="s", acr="1")
    payload = auth.audit_payload("person", single_use=True)
    assert payload == {
        "channel": "rest",
        "method": "fresh_sign_in",
        "auth_time": 1700,
        "acr": "1",
        "sign_in_key": auth.sign_in_key("person"),
        "single_use": True,
    }
    assert list(payload) == [
        "channel",
        "method",
        "auth_time",
        "acr",
        "sign_in_key",
        "single_use",
    ]


def test_audit_payload_with_a_touch_has_presence_and_binding() -> None:
    binding = TouchBinding(
        algorithm=BINDING_ALGORITHM,
        hash="b" * 64,
        request_ids=("r1",),
        card_hash="c" * 64,
        challenge_id="ch1",
    )
    auth = ApprovalAuth(
        channel="web",
        method="fresh_sign_in",
        auth_time=1700,
        acr="1",
        presence=TouchPresence(acr="meho-touch", amr=("hwk",), auth_time=1750),
        binding=binding,
    )
    payload = auth.audit_payload("person")
    assert payload["presence"] == {"acr": "meho-touch", "amr": ["hwk"], "auth_time": 1750}
    assert payload["binding"] == {
        "algorithm": BINDING_ALGORITHM,
        "hash": "b" * 64,
        "request_ids": ["r1"],
        "card_hash": "c" * 64,
        "challenge_id": "ch1",
    }
    assert payload["sign_in_key"] == auth.sign_in_key("person")


# ---------------------------------------------------------------------------
# The daily clean-up
# ---------------------------------------------------------------------------


async def test_cleanup_deletes_only_old_unused_rows() -> None:
    tenant_id = await _seed_tenant()
    request_id = await _park(tenant_id)
    now = datetime.now(UTC)
    old = now - timedelta(days=15)
    old_unused = await _start(tenant_id, [request_id], now=old)
    old_used = await _start(tenant_id, [request_id], now=old)
    await _use(old_used, _person(tenant_id), now=old + timedelta(seconds=10))
    recent_unused = await _start(tenant_id, [request_id], now=now - timedelta(days=13))

    dropped = await delete_unused_challenges(now=now)

    assert dropped == 1
    async with get_sessionmaker()() as session:
        left = set((await session.execute(select(ApprovalPresenceChallenge.id))).scalars().all())
    assert old_unused.id not in left
    assert {old_used.id, recent_unused.id} <= left


async def test_cleanup_loop_first_run_does_not_wait_a_full_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first clean-up comes one minute after start, then once a day."""
    events: list[tuple[str, float]] = []

    async def _fake_sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        if len(events) > 3:
            raise asyncio.CancelledError

    async def _fake_cleanup() -> int:
        events.append(("cleanup", 0))
        return 0

    monkeypatch.setattr(f"{_MODULE}.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(f"{_MODULE}.delete_unused_challenges", _fake_cleanup)
    monkeypatch.setattr(f"{_MODULE}.note_loop_tick", lambda *_args, **_kwargs: None)

    with pytest.raises(asyncio.CancelledError):
        await _cleanup_loop()

    assert events == [
        ("sleep", FIRST_RUN_DELAY_SECONDS),
        ("cleanup", 0),
        ("sleep", CLEANUP_INTERVAL_SECONDS),
        ("cleanup", 0),
        ("sleep", CLEANUP_INTERVAL_SECONDS),
    ]
