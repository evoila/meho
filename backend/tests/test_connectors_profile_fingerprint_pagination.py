# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Profile-driven fingerprint / probe + named pagination (G0.28-T6 #1972).

Covers the three acceptance criteria of #1972:

* ``fingerprint`` / ``probe`` derive from the :class:`ExecutionProfile`; the
  named version-splitter enum covers harbor's ``-`` split + vRLI's 5-part
  dot split.
* One named pagination strategy (``cursor_token``) works end-to-end for an
  ingested list op through :func:`dispatch_ingested`.
* Response-field selection is a single literal top-level key; the
  "no dotted paths" constraint is rejected at the schema boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.profile import (
    VERSION_SPLITTERS,
    AuthSpec,
    CursorTokenPagination,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
    ProbeSpec,
    VersionSplitter,
    split_version,
)
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._branches import dispatch_ingested

# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str = "prof-a"
    host: str = "prof-a.test.invalid"
    port: int | None = 443
    auth_model: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: None = None
    version: str | None = None
    preferred_impl_id: None = None
    verify_tls: bool = True
    tls_ca_pin: str | None = None


def _make_operator() -> Operator:
    return Operator(
        sub="op",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


_AUTH = AuthSpec(scheme="basic", secret_fields=("username", "password"))


def _profile(
    *,
    fingerprint: FingerprintSpec | None = None,
    probe: str | ProbeSpec = "delegate",
    pagination: PaginationSpec | None = None,
) -> ExecutionProfile:
    return ExecutionProfile(
        product="harbor",
        version="2.x",
        auth=_AUTH,
        fingerprint=fingerprint
        or FingerprintSpec(
            path="/api/v2.0/systeminfo",
            version_key="harbor_version",
            version_splitter="dash",
        ),
        probe=probe,
        pagination=pagination or PaginationSpec(strategy="none", items_key="value"),
    )


def _profiled_connector(profile: ExecutionProfile) -> ProfiledRestConnector:
    """A profiled connector whose auth_headers returns ``{}`` (T4 #1970 is unmerged).

    Until the named-auth wiring lands, ``ProfiledRestConnector.auth_headers``
    raises; the fingerprint/probe/pagination paths under test here either run
    unauthenticated or only need a header dict, so a no-op override isolates
    T6's behaviour from T4's.
    """

    class _Conn(ProfiledRestConnector):
        product = "harbor"
        version = "2.x"
        impl_id = "harbor-rest"

        async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
            return {}

    conn = _Conn()
    conn.profile = profile
    return conn


# ---------------------------------------------------------------------------
# split_version — the named splitter catalog
# ---------------------------------------------------------------------------


def test_version_splitters_set_matches_literal() -> None:
    import typing

    assert frozenset(typing.get_args(VersionSplitter)) == VERSION_SPLITTERS
    assert {"none", "dash", "vrli_five_part"} == VERSION_SPLITTERS


@pytest.mark.parametrize(
    ("splitter", "raw", "expected"),
    [
        ("none", "9.0.0.0", ("9.0.0.0", None)),
        ("dash", "v2.11.0-abc1234", ("v2.11.0", "abc1234")),
        ("dash", "v2.11.0", ("v2.11.0", None)),
        ("vrli_five_part", "9.0.0.0.21761695", ("9.0.0", "21761695")),
        ("vrli_five_part", "9.0.0", ("9.0.0", None)),
    ],
)
def test_split_version_named_shapes(
    splitter: VersionSplitter, raw: str, expected: tuple[str | None, str | None]
) -> None:
    assert split_version(splitter, raw) == expected


@pytest.mark.parametrize("splitter", sorted(VERSION_SPLITTERS))
def test_split_version_tolerates_blank(splitter: VersionSplitter) -> None:
    assert split_version(splitter, None) == (None, None)
    assert split_version(splitter, "") == (None, None)


# ---------------------------------------------------------------------------
# Schema — literal-top-level-key constraint (#1177), closed enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_key", ["a.b", "items[0]", "data.*", "x[1].y"])
def test_fingerprint_version_key_rejects_dotted_path(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        FingerprintSpec(path="/v", version_key=bad_key)


@pytest.mark.parametrize("bad_key", ["a.b", "comp[0]", "x.*"])
def test_probe_ok_field_rejects_dotted_path(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        ProbeSpec(path="/h", ok_field=bad_key, ok_value="ok")


@pytest.mark.parametrize("bad_key", ["a.b", "rows[0]", "data.*"])
def test_pagination_items_key_rejects_dotted_path(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        PaginationSpec(strategy="none", items_key=bad_key)


def test_cursor_resp_field_rejects_dotted_path() -> None:
    with pytest.raises(ValidationError):
        CursorTokenPagination(req_param="pageToken", resp_field="page.next")


def test_pagination_cursor_token_requires_cursor() -> None:
    with pytest.raises(ValidationError):
        PaginationSpec(strategy="cursor_token", items_key="accounts")


def test_pagination_none_forbids_cursor() -> None:
    with pytest.raises(ValidationError):
        PaginationSpec(
            strategy="none",
            items_key="accounts",
            cursor=CursorTokenPagination(req_param="pageToken", resp_field="nextPageToken"),
        )


def test_fingerprint_splitter_literal_closed() -> None:
    with pytest.raises(ValidationError):
        FingerprintSpec(path="/v", version_key="version", version_splitter="regex")  # type: ignore[arg-type]


def test_specs_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        FingerprintSpec(path="/v", version_key="version", jsonpath="$.version")  # type: ignore[call-arg]


def test_profile_carries_new_specs() -> None:
    """The literal-key constraint also lives on the schema-shape: no DSL fields."""
    assert set(ExecutionProfile.model_fields) == {
        "product",
        "version",
        "auth",
        "fingerprint",
        "probe",
        "pagination",
    }


# ---------------------------------------------------------------------------
# ProfiledRestConnector.fingerprint — derived from the profile
# ---------------------------------------------------------------------------


@respx.mock
async def test_fingerprint_authenticated_harbor_dash_split() -> None:
    conn = _profiled_connector(_profile())
    respx.get("https://prof-a.test.invalid/api/v2.0/systeminfo").mock(
        return_value=httpx.Response(200, json={"harbor_version": "v2.11.0-abc1234"})
    )
    fp = await conn.fingerprint(_StubTarget(), _make_operator())
    assert fp.reachable is True
    assert fp.version == "v2.11.0"
    assert fp.build == "abc1234"
    assert fp.probe_method == "GET /api/v2.0/systeminfo"


@respx.mock
async def test_fingerprint_unauthenticated_vrli_five_part() -> None:
    profile = _profile(
        fingerprint=FingerprintSpec(
            path="/api/v2/version",
            authenticated=False,
            version_key="version",
            version_splitter="vrli_five_part",
        )
    )
    conn = _profiled_connector(profile)
    respx.get("https://prof-a.test.invalid/api/v2/version").mock(
        return_value=httpx.Response(200, json={"version": "9.0.0.0.21761695"})
    )
    fp = await conn.fingerprint(_StubTarget())  # no operator — unauthenticated
    assert fp.reachable is True
    assert fp.version == "9.0.0"
    assert fp.build == "21761695"


@respx.mock
async def test_fingerprint_transport_error_is_unreachable() -> None:
    conn = _profiled_connector(_profile())
    respx.get("https://prof-a.test.invalid/api/v2.0/systeminfo").mock(
        return_value=httpx.Response(503)
    )
    fp = await conn.fingerprint(_StubTarget(), _make_operator())
    assert fp.reachable is False
    assert "error" in fp.extras


def test_fingerprint_authenticated_without_operator_unreachable() -> None:
    """An authenticated recipe with no operator yields an unreachable result."""
    import asyncio

    conn = _profiled_connector(_profile())
    fp = asyncio.run(conn.fingerprint(_StubTarget()))
    assert fp.reachable is False


# ---------------------------------------------------------------------------
# ProfiledRestConnector.probe — delegate + dedicated health
# ---------------------------------------------------------------------------


@respx.mock
async def test_probe_delegate_reports_fingerprint_reachability() -> None:
    profile = _profile(
        fingerprint=FingerprintSpec(
            path="/api/v2/version",
            authenticated=False,
            version_key="version",
            version_splitter="none",
        ),
        probe="delegate",
    )
    conn = _profiled_connector(profile)
    respx.get("https://prof-a.test.invalid/api/v2/version").mock(
        return_value=httpx.Response(200, json={"version": "9.0.0"})
    )
    res = await conn.probe(_StubTarget())
    assert res.ok is True


@respx.mock
async def test_probe_dedicated_health_ok() -> None:
    profile = _profile(
        probe=ProbeSpec(path="/api/v2.0/health", ok_field="status", ok_value="healthy")
    )
    conn = _profiled_connector(profile)
    respx.get("https://prof-a.test.invalid/api/v2.0/health").mock(
        return_value=httpx.Response(200, json={"status": "healthy"})
    )
    res = await conn.probe(_StubTarget())
    assert res.ok is True


@respx.mock
async def test_probe_dedicated_health_unhealthy() -> None:
    profile = _profile(
        probe=ProbeSpec(path="/api/v2.0/health", ok_field="status", ok_value="healthy")
    )
    conn = _profiled_connector(profile)
    respx.get("https://prof-a.test.invalid/api/v2.0/health").mock(
        return_value=httpx.Response(200, json={"status": "unhealthy"})
    )
    res = await conn.probe(_StubTarget())
    assert res.ok is False
    assert "status" in (res.reason or "")


def test_fingerprint_without_profile_raises() -> None:
    import asyncio

    with pytest.raises(NotImplementedError):
        asyncio.run(ProfiledRestConnector().fingerprint(_StubTarget(), _make_operator()))


# ---------------------------------------------------------------------------
# dispatch_ingested — cursor_token pagination end-to-end
# ---------------------------------------------------------------------------


def _list_descriptor() -> EndpointDescriptor:
    return EndpointDescriptor(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.things.list",
        source_kind="ingested",
        method="GET",
        path="/api/v1/things",
        parameter_schema={"type": "object", "properties": {}},
    )


@respx.mock
async def test_cursor_token_pagination_assembles_all_pages() -> None:
    profile = _profile(
        pagination=PaginationSpec(
            strategy="cursor_token",
            items_key="things",
            cursor=CursorTokenPagination(req_param="pageToken", resp_field="nextPageToken"),
        )
    )
    conn = _profiled_connector(profile)

    route = respx.get("https://prof-a.test.invalid/api/v1/things")

    def _responder(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken")
        if token is None:
            return httpx.Response(
                200, json={"things": [{"id": 1}, {"id": 2}], "nextPageToken": "p2"}
            )
        if token == "p2":
            return httpx.Response(200, json={"things": [{"id": 3}], "nextPageToken": "p3"})
        return httpx.Response(200, json={"things": [{"id": 4}]})

    route.mock(side_effect=_responder)

    result = await dispatch_ingested(
        connector=conn,
        descriptor=_list_descriptor(),
        operator=_make_operator(),
        target=_StubTarget(),
        params={},
    )
    assert result == {"things": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}], "total": 4}
    assert route.call_count == 3


@respx.mock
async def test_strategy_none_makes_single_request() -> None:
    conn = _profiled_connector(
        _profile(pagination=PaginationSpec(strategy="none", items_key="things"))
    )
    route = respx.get("https://prof-a.test.invalid/api/v1/things").mock(
        return_value=httpx.Response(200, json={"things": [{"id": 1}], "nextPageToken": "ignored"})
    )
    result = await dispatch_ingested(
        connector=conn,
        descriptor=_list_descriptor(),
        operator=_make_operator(),
        target=_StubTarget(),
        params={},
    )
    # strategy='none' does not loop even when a cursor field is present.
    assert route.call_count == 1
    assert result["nextPageToken"] == "ignored"


@respx.mock
async def test_plain_connector_without_profile_is_single_request() -> None:
    """A connector with no profile falls through to the single-request path."""

    class _Plain(ProfiledRestConnector):
        product = "harbor"
        version = "2.x"
        impl_id = "harbor-rest"

        async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
            return {}

    conn = _Plain()  # profile stays None
    route = respx.get("https://prof-a.test.invalid/api/v1/things").mock(
        return_value=httpx.Response(200, json={"things": []})
    )
    result = await dispatch_ingested(
        connector=conn,
        descriptor=_list_descriptor(),
        operator=_make_operator(),
        target=_StubTarget(),
        params={},
    )
    assert route.call_count == 1
    assert result == {"things": []}
