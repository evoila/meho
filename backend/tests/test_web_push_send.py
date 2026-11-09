# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Push messages about requests that wait for approval (web_push/send.py).

A fake push service (an ``httpx.MockTransport``) takes every message. The
tests hold the browsers' private keys, so they decrypt each message and read
what the browser would show.

* A new request goes to the people who may approve it, in its tenant only.
* A decision sends a follow-up with the same tag.
* A push service that answers 404 or 410 removes the subscription.
* A person gets at most one message per window, then one summary.
* The message holds no operation, parameter or target.
* The message goes out only after the audit row committed; a failed commit
  sends nothing.
* The encryption matches the example of RFC 8291, and the signed token
  matches RFC 8292.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from structlog.testing import capture_logs

import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog, Tenant
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, register_preview_builder
from meho_backplane.operations.approval_queue import record_approval_step_event
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from meho_backplane.web_push import send as send_module
from meho_backplane.web_push.encryption import encrypt, vapid_authorization
from meho_backplane.web_push.keys import (
    b64url_decode,
    b64url_encode,
    new_private_key,
    public_key_for,
    reset_key_cache_for_testing,
)
from meho_backplane.web_push.models import PushSubscription
from meho_backplane.web_push.recipients import approve_scope_for
from tests.test_destructive_preview_binding import (
    _CONNECTOR_ID,
    _OP_ID,
    _PARAMS,
    _TENANT,
    _blast_radius_builder,
    _insert_destructive_descriptor,
    _register_recording_connector,
    _seed_target,
)

_OTHER_TENANT = uuid.UUID("99999999-9999-9999-9999-999999999999")
_PRIVATE_KEY = new_private_key()
_TENANT_NAME = "Tenant push-test"


# ---------------------------------------------------------------------------
# Fixtures: settings, a fake push service, browsers with known keys
# ---------------------------------------------------------------------------


@dataclass
class _Browser:
    endpoint: str
    private_key: ec.EllipticCurvePrivateKey
    auth: bytes

    @property
    def p256dh(self) -> str:
        point = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return b64url_encode(point)


def _browser(name: str, host: str = "fcm.googleapis.com") -> _Browser:
    return _Browser(
        endpoint=f"https://{host}/fcm/send/{name}",
        private_key=ec.generate_private_key(ec.SECP256R1()),
        auth=os.urandom(16),
    )


def _decrypt(body: bytes, browser: _Browser) -> dict[str, Any]:
    """Read a message as the browser does (RFC 8291 section 3, receiver side)."""
    salt, key_len = body[:16], body[20]
    as_public = body[21 : 21 + key_len]
    ciphertext = body[21 + key_len :]
    server_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh = browser.private_key.exchange(ec.ECDH(), server_key)
    ua_public = b64url_decode(browser.p256dh)

    def mac(key: bytes, data: bytes) -> bytes:
        return hmac.new(key, data, hashlib.sha256).digest()

    ikm = mac(mac(browser.auth, ecdh), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = mac(salt, ikm)
    cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")
    data = json.loads(plain[:-1])
    assert isinstance(data, dict)
    return data


class _FakePushService:
    """Takes every message; answers with a status per endpoint (201 by default)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}
        self.fail = False
        self.on_request: Any = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.on_request is not None:
            await self.on_request(request)
        if self.fail:
            raise httpx.ConnectError("push service down", request=request)
        self.requests.append(request)
        return httpx.Response(self.status.get(str(request.url), 201))

    def to(self, browser: _Browser) -> list[dict[str, Any]]:
        return [
            _decrypt(r.content, browser) for r in self.requests if str(r.url) == browser.endpoint
        ]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "true")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", _PRIVATE_KEY)
    get_settings.cache_clear()
    reset_key_cache_for_testing()
    send_module.reset_for_testing()
    builders = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    send_module.set_transport_for_testing(None)
    send_module.reset_for_testing()
    get_settings.cache_clear()
    reset_key_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders)


@pytest.fixture
async def push_service() -> AsyncIterator[_FakePushService]:
    service = _FakePushService()
    send_module.set_transport_for_testing(httpx.MockTransport(service.handle))
    async with get_sessionmaker()() as s, s.begin():
        s.add(Tenant(id=_TENANT, slug="push-test", name=_TENANT_NAME))
        s.add(Tenant(id=_OTHER_TENANT, slug="push-other", name="Tenant push-other"))
    yield service
    await send_module.wait_for_sends_for_testing()


async def _subscribe(
    browser: _Browser, *, sub: str, scope: str | None, tenant_id: uuid.UUID = _TENANT
) -> uuid.UUID:
    row = PushSubscription(
        operator_sub=sub,
        tenant_id=tenant_id,
        endpoint=browser.endpoint,
        p256dh=browser.p256dh,
        auth=b64url_encode(browser.auth),
        created_at=datetime.now(UTC),
        approve_scope=scope,
    )
    async with get_sessionmaker()() as s, s.begin():
        s.add(row)
    return row.id


def _request(
    *,
    requester: str = "op-requester",
    level: str = "caution",
    params: dict[str, Any] | None = None,
    status: str = ApprovalRequestStatus.PENDING.value,
) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        run_id=None,
        principal_sub=requester,
        op_id="POST:/vms/{vm}/power",
        connector_id="gh-rest-3",
        target_id=None,
        params_hash="0" * 64,
        params=params or {"vm": "vm-made-up-name"},
        proposed_effect={"op_id": "POST:/vms/{vm}/power", "safety_level": level},
        status=status,
    )


async def _step(request: ApprovalRequest, decision: str = "pending") -> None:
    await record_approval_step_event(
        tenant_id=_TENANT, request=request, decision=decision, audit_id=uuid.uuid4()
    )
    await send_module.wait_for_sends_for_testing()


async def _rows() -> list[PushSubscription]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(PushSubscription))).scalars())


# ---------------------------------------------------------------------------
# Who gets a message
# ---------------------------------------------------------------------------


async def test_a_new_request_goes_to_the_people_who_may_approve_it(
    push_service: _FakePushService,
) -> None:
    admin, approver, operator, elsewhere = (
        _browser("admin"),
        _browser("approver", host="web.push.apple.com"),
        _browser("operator"),
        _browser("elsewhere"),
    )
    await _subscribe(admin, sub="admin", scope="all")
    await _subscribe(approver, sub="approver", scope="lower_risk")
    await _subscribe(operator, sub="operator", scope=None)
    await _subscribe(elsewhere, sub="admin-b", scope="all", tenant_id=_OTHER_TENANT)

    request = _request(level="caution")
    await _step(request)

    assert [m["title"] for m in push_service.to(admin)] == ["An approval waits"]
    assert [m["title"] for m in push_service.to(approver)] == ["An approval waits"]
    assert push_service.to(operator) == []
    assert push_service.to(elsewhere) == []
    message = push_service.to(admin)[0]
    assert message == {
        "title": "An approval waits",
        "body": f"Tenant {_TENANT_NAME}. Request {request.id}.",
        "tag": f"approval-{request.id}",
        "url": "/ui/approvals",
    }


async def test_a_high_risk_request_goes_only_to_admins_and_sa_operators(
    push_service: _FakePushService,
) -> None:
    admin, approver = _browser("admin"), _browser("approver")
    await _subscribe(admin, sub="admin", scope="all")
    await _subscribe(approver, sub="approver", scope="lower_risk")

    for level in ("dangerous", "destructive"):
        send_module.reset_for_testing()
        await _step(_request(level=level))

    assert len(push_service.to(admin)) == 2
    assert push_service.to(approver) == []


async def test_own_request_reaches_only_a_person_who_may_approve_their_own(
    push_service: _FakePushService,
) -> None:
    sa_operator, approver = _browser("sa"), _browser("approver")
    await _subscribe(sa_operator, sub="sa-op", scope="all")
    await _subscribe(approver, sub="approver", scope="lower_risk")

    by_sa_operator = _request(requester="sa-op")
    by_approver = _request(requester="approver")
    await _step(by_sa_operator)
    send_module.reset_for_testing()
    await _step(by_approver)

    # The SA-Operator gets both, also their own; the approver gets only the
    # other person's request, never their own.
    assert len(push_service.to(sa_operator)) == 2
    assert [m["tag"] for m in push_service.to(approver)] == [f"approval-{by_sa_operator.id}"]


async def test_nothing_is_sent_with_the_setting_off(
    push_service: _FakePushService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _subscribe(_browser("admin"), sub="admin", scope="all")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
    get_settings.cache_clear()
    reset_key_cache_for_testing()

    await _step(_request())

    assert push_service.requests == []


# ---------------------------------------------------------------------------
# Decisions, gone browsers, the rate limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "title"),
    [
        ("approved", "Approval approved"),
        ("rejected", "Approval rejected"),
        ("expired", "Approval expired"),
        ("withdrawn", "Approval withdrawn"),
    ],
)
async def test_a_decision_replaces_the_waiting_message(
    push_service: _FakePushService, decision: str, title: str
) -> None:
    admin = _browser("admin")
    await _subscribe(admin, sub="admin", scope="all")
    request = _request()

    await _step(request)
    send_module.reset_for_testing()
    request.status = decision
    await _step(request, decision)

    first, second = push_service.to(admin)
    assert first["title"] == "An approval waits"
    assert second["title"] == title
    assert second["tag"] == first["tag"] == f"approval-{request.id}"
    # The push service replaces an undelivered first message too.
    topics = {r.headers["topic"] for r in push_service.requests}
    assert topics == {request.id.hex}


@pytest.mark.parametrize("status", [404, 410])
async def test_a_gone_browser_is_removed(push_service: _FakePushService, status: int) -> None:
    gone, alive = _browser("gone"), _browser("alive")
    await _subscribe(gone, sub="admin", scope="all")
    await _subscribe(alive, sub="admin-2", scope="all")
    push_service.status[gone.endpoint] = status

    with capture_logs() as logs:
        await _step(_request())

    assert [row.endpoint for row in await _rows()] == [alive.endpoint]
    assert any(entry["event"] == "web_push_subscription_gone" for entry in logs)
    assert gone.endpoint not in repr(logs)


async def test_another_error_keeps_the_browser(push_service: _FakePushService) -> None:
    browser = _browser("busy")
    await _subscribe(browser, sub="admin", scope="all")
    push_service.status[browser.endpoint] = 429

    await _step(_request())

    assert len(await _rows()) == 1


async def test_the_rate_limit_holds_and_ends_with_one_summary(
    push_service: _FakePushService,
) -> None:
    admin = _browser("admin")
    await _subscribe(admin, sub="admin", scope="all")
    send_module.reset_for_testing(window=0.3)

    started = time.monotonic()
    for _ in range(4):
        await record_approval_step_event(
            tenant_id=_TENANT, request=_request(), decision="pending", audit_id=uuid.uuid4()
        )
    await send_module.wait_for_sends_for_testing()

    titles = [m["title"] for m in push_service.to(admin)]
    assert titles == ["An approval waits", "Approvals changed"]
    summary = push_service.to(admin)[1]
    assert summary["body"] == "3 more approval messages. Open the approvals page."
    assert summary["tag"] == "approval-summary"
    assert time.monotonic() - started >= 0.3


def test_the_default_window_is_thirty_seconds() -> None:
    assert send_module.RATE_LIMIT_SECONDS == 30.0


# ---------------------------------------------------------------------------
# What leaves MEHO
# ---------------------------------------------------------------------------


async def test_the_message_holds_no_request_content(push_service: _FakePushService) -> None:
    admin = _browser("admin")
    await _subscribe(admin, sub="admin", scope="all")
    secret_value = "made-up-password-7f3a"
    request = _request(params={"vm": "vm-made-up-name", "password": secret_value})

    await _step(request)

    (sent,) = push_service.requests
    text = json.dumps(_decrypt(sent.content, admin))
    for value in (secret_value, "vm-made-up-name", "password", request.op_id, "gh-rest-3"):
        assert value not in text
    # The body on the wire is encrypted: not even the tenant name is readable.
    assert _TENANT_NAME.encode() not in sent.content
    assert sent.headers["content-encoding"] == "aes128gcm"


async def test_an_endpoint_off_the_allowlist_gets_nothing(push_service: _FakePushService) -> None:
    inside = _Browser(
        endpoint="https://push.example/inside",
        private_key=ec.generate_private_key(ec.SECP256R1()),
        auth=os.urandom(16),
    )
    await _subscribe(inside, sub="admin", scope="all")

    with capture_logs() as logs:
        await _step(_request())

    assert push_service.requests == []
    assert any(entry["event"] == "web_push_endpoint_refused" for entry in logs)
    assert inside.endpoint not in repr(logs)


async def test_a_failed_send_logs_no_endpoint_and_no_key(push_service: _FakePushService) -> None:
    browser = _browser("down")
    await _subscribe(browser, sub="admin", scope="all")
    push_service.fail = True

    with capture_logs() as logs:
        await _step(_request())

    failed = [entry for entry in logs if entry["event"] == "web_push_send_failed"]
    assert failed and failed[0]["error"] == "ConnectError"
    text = repr(logs)
    for secret in (browser.endpoint, browser.p256dh, b64url_encode(browser.auth), _PRIVATE_KEY):
        assert secret not in text
    assert len(await _rows()) == 1


async def test_the_request_is_signed_with_the_push_key(push_service: _FakePushService) -> None:
    admin = _browser("admin", host="updates.push.services.mozilla.com")
    await _subscribe(admin, sub="admin", scope="all")

    await _step(_request())

    (sent,) = push_service.requests
    scheme, _, params = sent.headers["authorization"].partition(" ")
    assert scheme == "vapid"
    fields = dict(part.strip().split("=", 1) for part in params.split(","))
    assert fields["k"] == public_key_for(_PRIVATE_KEY)
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(fields["k"])
    )
    claims = jwt.decode(
        fields["t"],
        public,
        algorithms=["ES256"],
        audience="https://updates.push.services.mozilla.com",
    )
    assert claims["sub"] == "https://meho.test"
    assert claims["exp"] - time.time() <= 24 * 60 * 60
    assert sent.headers["ttl"]
    assert sent.headers["urgency"] == "high"


def test_vapid_token_has_no_subject_when_the_address_is_unknown() -> None:
    header = vapid_authorization(
        "https://fcm.googleapis.com/fcm/send/x", private_key=_PRIVATE_KEY, subject=None
    )
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert "sub" not in jwt.decode(token, options={"verify_signature": False})


def test_encryption_matches_the_example_of_rfc_8291() -> None:
    """RFC 8291, Appendix A: fixed keys and salt give the published bytes."""
    server_key = ec.derive_private_key(
        # The public example key of RFC 8291, Appendix A.
        int.from_bytes(
            b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"
        ),  # gitleaks:allow
        ec.SECP256R1(),
    )
    body = encrypt(
        b64url_decode("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        p256dh="BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        auth="BTBZMqHH6r4Tts7J_aSIgg",  # gitleaks:allow (public RFC 8291 example value)
        server_key=server_key,
        salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    header = b64url_decode(
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    ciphertext = b64url_decode(
        "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    )
    assert body == header + ciphertext
    assert base64.urlsafe_b64encode(body[:16]).rstrip(b"=") == b"DGv6ra1nlYgDCS1FRnbzlw"


# ---------------------------------------------------------------------------
# What the person may approve
# ---------------------------------------------------------------------------


def _operator(**update: Any) -> Operator:
    base = Operator(
        sub="person",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )
    return base.model_copy(update=update)


@pytest.mark.parametrize(
    ("update", "scope"),
    [
        ({"tenant_role": TenantRole.TENANT_ADMIN}, "all"),
        ({"self_approver": True}, "all"),
        ({"tenant_role": TenantRole.READ_ONLY, "approver": True}, "lower_risk"),
        ({}, None),
        ({"tenant_role": TenantRole.READ_ONLY}, None),
        ({"tenant_role": TenantRole.TENANT_ADMIN, "principal_kind": PrincipalKind.AGENT}, None),
        ({"approver": True, "principal_kind": PrincipalKind.SERVICE}, None),
    ],
)
def test_approve_scope_follows_the_login_groups(update: dict[str, Any], scope: str | None) -> None:
    assert approve_scope_for(_operator(**update)) == scope


def test_approve_scope_of_no_login_is_none() -> None:
    assert approve_scope_for(None) is None


# ---------------------------------------------------------------------------
# Only after the audit row committed
# ---------------------------------------------------------------------------


async def _park_destructive(embedding: list[float]) -> dict[str, Any]:
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)
    requester = Operator(
        sub="op-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    result = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    await send_module.wait_for_sends_for_testing()
    return result


async def test_the_message_goes_out_after_the_audit_row_committed(
    push_service: _FakePushService,
) -> None:
    admin = _browser("admin")
    await _subscribe(admin, sub="admin", scope="all")
    seen_audit_rows: list[int] = []

    async def _check_audit(_: httpx.Request) -> None:
        # A new session sees only committed rows.
        async with get_sessionmaker()() as s:
            rows = await s.execute(select(AuditLog).where(AuditLog.path == "approval.request"))
            seen_audit_rows.append(len(list(rows.scalars())))

    push_service.on_request = _check_audit

    result = await _park_destructive([0.1] * 384)

    assert result["status"] == "awaiting_approval", result
    request_id = UUID(result["extras"]["approval_request_id"])
    assert [m["body"] for m in push_service.to(admin)] == [
        f"Tenant {_TENANT_NAME}. Request {request_id}."
    ]
    assert seen_audit_rows == [1]


async def test_a_failed_commit_sends_nothing(
    push_service: _FakePushService, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _subscribe(_browser("admin"), sub="admin", scope="all")
    real_create = approval_queue_module.create_pending_request

    async def _create_then_fail_commit(session: Any, **kwargs: Any) -> ApprovalRequest:
        request = await real_create(session, **kwargs)

        async def _refuse() -> None:
            raise RuntimeError("database refused the commit")

        monkeypatch.setattr(session, "commit", _refuse)
        return request

    monkeypatch.setattr(approval_queue_module, "create_pending_request", _create_then_fail_commit)

    result = await _park_destructive([0.1] * 384)

    assert result["status"] != "awaiting_approval"
    assert push_service.requests == []
