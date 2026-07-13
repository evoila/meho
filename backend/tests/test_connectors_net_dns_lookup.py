# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.dns_lookup`` — #2409 (Initiative #2405 T4).

Full ``dig`` parity on the T1 ``net.*`` mold:

* typed forward records (A/AAAA/CNAME/MX/TXT/SRV/NS/SOA) and reverse PTR,
  dispatched targetless on a fresh boot;
* a chosen ``resolver`` IP queries that nameserver — proven against a
  second fixture resolver returning a distinct answer (split-horizon);
* the **return-failures contract**: NXDOMAIN / no-answer / SERVFAIL /
  timeout / a refused-by-allowlist lookup return ``{resolved: false,
  reason}`` with dispatch ``status="ok"`` — never a ``connector_*`` error;
* the queried ``name`` + ``type`` + ``resolver`` land in the durable audit
  row's ``raw_payload``; the ``name`` and any custom ``resolver`` IP are
  probe-allowlist-gated.

The DNS queries run against an **in-process UDP fixture resolver** (real
dnspython wire path, no outbound network): each ``dns.asyncresolver``
the handler builds is redirected to the fixture's ephemeral port, and its
default ("system") nameserver is pointed at the fixture. The split-horizon
test binds a second fixture on ``127.0.0.2`` sharing the same port so a
custom ``resolver`` IP routes to a distinct responder.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import dns.asyncresolver
import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import ops as net_ops
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.ops import net_dns_lookup, register_net_typed_operations
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.dns_lookup"

#: Canonical forward answers the fixture serves, keyed by record type. The
#: values are valid rdata for their type (rrset.from_text parses them).
_FORWARD_ANSWERS: dict[str, tuple[str, ...]] = {
    "A": ("93.184.216.34",),
    "AAAA": ("2606:2800:220:1:248:1893:25c8:1946",),
    "CNAME": ("canonical.probe.example.",),
    "MX": ("10 mail.probe.example.",),
    "TXT": ('"v=spf1 -all"',),
    "SRV": ("10 60 5060 sip.probe.example.",),
    "NS": ("ns1.probe.example.",),
    "SOA": ("ns1.probe.example. hostmaster.probe.example. 1 3600 600 86400 60",),
}


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the minimal Settings env + reset dispatcher caches per test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_net_ops(stub_embedding_service: Any) -> AsyncIterator[None]:
    """Upsert the ``net.*`` descriptor rows for dispatch-driving tests."""
    await register_net_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_lookup(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``net.dns_lookup`` through the real targetless path."""
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# In-process UDP fixture resolver
# ---------------------------------------------------------------------------


class _DnsFixture:
    """Controls how the fixture UDP resolver answers a query.

    Mutate :attr:`mode` (``answer`` | ``nxdomain`` | ``servfail`` |
    ``noanswer``) to steer failure paths, or :attr:`a_value` for the
    split-horizon distinct answer.
    """

    def __init__(self, a_value: str = "93.184.216.34") -> None:
        self.mode = "answer"
        self.a_value = a_value
        self.ptr_value = "resolved.probe.example."
        self.authoritative = True

    def build_response(self, query: dns.message.Message) -> dns.message.Message:
        response = dns.message.make_response(query)
        if self.authoritative:
            response.flags |= dns.flags.AA
        if self.mode == "nxdomain":
            response.set_rcode(dns.rcode.NXDOMAIN)
            return response
        if self.mode == "servfail":
            response.set_rcode(dns.rcode.SERVFAIL)
            return response
        if self.mode == "noanswer":
            return response  # NOERROR, empty answer section
        question = query.question[0]
        qtype = dns.rdatatype.to_text(question.rdtype)
        if qtype == "PTR":
            response.answer.append(
                dns.rrset.from_text(question.name, 30, "IN", "PTR", self.ptr_value)
            )
        elif qtype == "A":
            response.answer.append(dns.rrset.from_text(question.name, 42, "IN", "A", self.a_value))
        elif qtype in _FORWARD_ANSWERS:
            response.answer.append(
                dns.rrset.from_text(question.name, 42, "IN", qtype, *_FORWARD_ANSWERS[qtype])
            )
        else:
            response.set_rcode(dns.rcode.NXDOMAIN)
        return response


class _DnsFixtureProtocol(asyncio.DatagramProtocol):
    def __init__(self, controller: _DnsFixture) -> None:
        self._controller = controller

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: Any) -> None:
        query = dns.message.from_wire(data)
        response = self._controller.build_response(query)
        self._transport.sendto(response.to_wire(), addr)  # type: ignore[attr-defined]


def _redirect_resolvers_to_port(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """Point every resolver the handler builds at the fixture's port.

    The factory pins ``.port`` (so a high-numbered fixture port is used
    instead of 53) and defaults the system nameserver to ``127.0.0.1``.
    The handler still overrides ``.nameservers`` for a custom ``resolver``
    IP — only the port is redirected, so the real dnspython wire path runs
    against the fixture.
    """
    real_cls = dns.asyncresolver.Resolver

    def _factory(*_args: object, **_kwargs: object) -> dns.asyncresolver.Resolver:
        resolver = real_cls(configure=False)
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = port
        return resolver

    monkeypatch.setattr(net_ops.dns.asyncresolver, "Resolver", _factory)


@pytest.fixture
async def dns_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_DnsFixture]:
    """Bind a fixture resolver on 127.0.0.1 and redirect handler resolvers to it."""
    controller = _DnsFixture()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DnsFixtureProtocol(controller), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("socket").getsockname()[1]
    _redirect_resolvers_to_port(monkeypatch, port)
    try:
        yield controller
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Forward typed records + reverse PTR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", list(_FORWARD_ANSWERS))
async def test_forward_lookup_returns_typed_records(
    monkeypatch: pytest.MonkeyPatch,
    dns_fixture: _DnsFixture,
    _registered_net_ops: None,
    record_type: str,
) -> None:
    """Each record type resolves to its records — targetless, fresh boot."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "probe.example, 127.0.0.0/8")

    result = await _dispatch_lookup({"name": "probe.example", "type": record_type})

    assert result.status == "ok", result.error
    body = result.result
    assert body["resolved"] is True
    assert body["reason"] is None
    assert body["type"] == record_type
    assert body["resolver"] == "system"
    values = [record["value"] for record in body["records"]]
    assert values == list(_FORWARD_ANSWERS[record_type])
    assert all(record["type"] == record_type for record in body["records"])
    assert all(record["ttl"] == 42 for record in body["records"])


async def test_reverse_lookup_returns_ptr(
    monkeypatch: pytest.MonkeyPatch,
    dns_fixture: _DnsFixture,
    _registered_net_ops: None,
) -> None:
    """An IP literal name performs a reverse PTR lookup (dig -x parity)."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")

    result = await _dispatch_lookup({"name": "10.0.0.5"})

    assert result.status == "ok", result.error
    body = result.result
    assert body["resolved"] is True
    assert body["type"] == "PTR"
    assert body["name"] == "10.0.0.5"
    assert [record["value"] for record in body["records"]] == ["resolved.probe.example."]
    assert body["records"][0]["type"] == "PTR"
    assert body["authoritative"] is True


# ---------------------------------------------------------------------------
# Chosen resolver — split-horizon proof
# ---------------------------------------------------------------------------


async def test_chosen_resolver_queries_that_nameserver(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_ops: None,
) -> None:
    """A ``resolver=<ip>`` routes to that nameserver — distinct answer.

    Two fixtures share one port on different loopback IPs: the default
    ("system") resolver 127.0.0.1 answers ``10.0.0.1``; the chosen
    resolver 127.0.0.2 answers ``10.0.0.2``. The same name yields
    different records depending on which resolver is asked — the
    split-horizon case the op exists to diagnose.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "split.example, 127.0.0.0/8")
    loop = asyncio.get_running_loop()

    system = _DnsFixture(a_value="10.0.0.1")
    chosen = _DnsFixture(a_value="10.0.0.2")
    transport_sys, _ = await loop.create_datagram_endpoint(
        lambda: _DnsFixtureProtocol(system), local_addr=("127.0.0.1", 0)
    )
    port = transport_sys.get_extra_info("socket").getsockname()[1]
    transport_chosen, _ = await loop.create_datagram_endpoint(
        lambda: _DnsFixtureProtocol(chosen), local_addr=("127.0.0.2", port)
    )
    _redirect_resolvers_to_port(monkeypatch, port)
    try:
        via_system = await _dispatch_lookup({"name": "split.example", "type": "A"})
        via_chosen = await _dispatch_lookup(
            {"name": "split.example", "type": "A", "resolver": "127.0.0.2"}
        )
    finally:
        transport_sys.close()
        transport_chosen.close()

    assert via_system.status == "ok" and via_chosen.status == "ok"
    assert via_system.result["resolver"] == "system"
    assert via_system.result["records"][0]["value"] == "10.0.0.1"
    assert via_chosen.result["resolver"] == "127.0.0.2"
    assert via_chosen.result["records"][0]["value"] == "10.0.0.2"


# ---------------------------------------------------------------------------
# Return-failures contract — status=ok, never connector_*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_reason",
    [
        ("nxdomain", "nxdomain"),
        ("noanswer", "no_answer"),
        ("servfail", "servfail"),
    ],
    ids=["nxdomain", "no_answer", "servfail"],
)
async def test_lookup_failure_is_ok_status_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    dns_fixture: _DnsFixture,
    _registered_net_ops: None,
    mode: str,
    expected_reason: str,
) -> None:
    """A resolver-level failure returns resolved=false with a reason, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "probe.example, 127.0.0.0/8")
    dns_fixture.mode = mode

    result = await _dispatch_lookup({"name": "probe.example", "type": "A"})

    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    assert result.result["resolved"] is False
    assert result.result["reason"] == expected_reason
    assert result.result["records"] == []
    assert result.result["authoritative"] is None


async def test_timeout_maps_to_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query deadline elapsing returns reason='timeout' (handler-direct)."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "slow.example")

    async def _raise_timeout(*_a: object, **_kw: object) -> object:
        raise dns.exception.Timeout()

    monkeypatch.setattr(net_ops.dns.asyncresolver.Resolver, "resolve", _raise_timeout)

    result = await net_dns_lookup(_make_operator(), None, {"name": "slow.example", "type": "A"})

    assert result["resolved"] is False
    assert result["reason"] == "timeout"


# ---------------------------------------------------------------------------
# Allowlist gating — name and custom resolver
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_query(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_ops: None,
) -> None:
    """Empty allowlist ⇒ structured refusal, no resolver ever constructed."""

    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no resolver may be built when the lookup is refused")

    monkeypatch.setattr(net_ops.dns.asyncresolver, "Resolver", _boom)

    result = await _dispatch_lookup({"name": "internal.example", "type": "A"})

    assert result.status == "ok", result.error
    assert result.result == {
        "resolved": False,
        "name": "internal.example",
        "type": "A",
        "resolver": "system",
        "records": [],
        "authoritative": None,
        "authenticated_data": None,
        "reason": "not_in_probe_allowlist",
    }


async def test_unlisted_custom_resolver_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_ops: None,
) -> None:
    """A custom resolver IP outside the allowlist is refused (the name alone is not enough)."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "probe.example")  # name listed, resolver IP is not

    result = await _dispatch_lookup({"name": "probe.example", "type": "A", "resolver": "8.8.8.8"})

    assert result.status == "ok", result.error
    assert result.result["resolved"] is False
    assert result.result["reason"] == "not_in_probe_allowlist"
    assert result.result["resolver"] == "8.8.8.8"


async def test_non_ip_custom_resolver_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname (non-IP) ``resolver`` returns reason='bad_resolver' (handler-direct)."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "probe.example, dns.internal")

    result = await net_dns_lookup(
        _make_operator(),
        None,
        {"name": "probe.example", "type": "A", "resolver": "dns.internal"},
    )

    assert result["resolved"] is False
    assert result["reason"] == "bad_resolver"


# ---------------------------------------------------------------------------
# Audit row records name + type + resolver
# ---------------------------------------------------------------------------


async def test_audit_row_records_name_type_and_resolver(
    monkeypatch: pytest.MonkeyPatch,
    dns_fixture: _DnsFixture,
    _registered_net_ops: None,
) -> None:
    """The durable audit row's raw_payload carries the queried name/type/resolver."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "probe.example, 127.0.0.0/8")

    result = await _dispatch_lookup({"name": "probe.example", "type": "MX"})
    assert result.status == "ok", result.error

    rows = await _fetch_audit_rows()
    lookup_rows = [row for row in rows if row.path == _OP_ID]
    assert len(lookup_rows) == 1
    raw = lookup_rows[0].raw_payload
    assert raw is not None
    assert raw["name"] == "probe.example"
    assert raw["type"] == "MX"
    assert raw["resolver"] == "system"
