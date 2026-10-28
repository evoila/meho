# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the checks every FortiGate change runs, before and after it is sent.

A respx-mocked FortiGate answers from made-up recorded answers. The object
is an address on a VDOM target, as the later address operations use it.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors.fortios import FortiOSConnector
from meho_backplane.connectors.fortios._errors import FortiOSApiError
from meho_backplane.connectors.fortios.redaction import REDACTED
from meho_backplane.connectors.fortios.write_checks import (
    HA_ATTEMPTS,
    CmdbObject,
    FortiOSWriteRefusedError,
    WritePreviewSpec,
    check_before_write,
    register_write_preview,
    send_write,
    verify_after_write,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from tests import _fortios_fixtures as fx
from tests._fortios_support import (
    FortiTarget,
    make_operator,
    no_retry_wait,
    settings_env,
    token_loader,
)

_OBJ = CmdbObject("firewall/address", "host-a")
_OBJECT_PATH = "cmdb/firewall/address/host-a"
_USAGE_PATH = "monitor/system/object/usage"
_REFS = ("firewall/addrgrp:group-a", "firewall/policy:3")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from settings_env(monkeypatch)


def _answers(**overrides: Any) -> dict[str, Any]:
    answers = {
        **fx.DEVICE_ANSWERS,
        _OBJECT_PATH: [fx.ADDRESS],
        _USAGE_PATH: fx.USAGE_OF_ADDRESS,
    }
    answers.update(overrides)
    return answers


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def _run(fake: fx.FakeFortiGate, call: Any) -> Any:
    connector = FortiOSConnector(token_loader=token_loader)
    target = FortiTarget(scope="vdom", vdom="vdom-a")
    try:
        async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
            mock.route().mock(side_effect=fake)
            return await call(connector, target)
    finally:
        await connector.aclose()


async def _before(fake: fx.FakeFortiGate, expected: Any = None, refs: Any = _REFS) -> Any:
    expected = fx.ADDRESS if expected is None else expected

    async def call(connector: FortiOSConnector, target: Any) -> Any:
        return await check_before_write(
            connector,
            make_operator(),
            target,
            obj=_OBJ,
            expected_before=expected,
            expected_references=refs,
        )

    return await _run(fake, call)


# ---------------------------------------------------------------------------
# Before the write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_write_passes_when_everything_matches() -> None:
    fake = fx.FakeFortiGate(_answers())
    before = await _before(fake)
    assert before.version == "7.4.4"
    assert before.live == fx.ADDRESS
    assert before.references == _REFS
    usage = next(c for c in fake.calls if c.url.path.endswith("object/usage"))
    assert dict(usage.url.params) == {
        "q_path": "firewall",
        "q_name": "address",
        "mkey": "host-a",
        "vdom": "vdom-a",
    }


@pytest.mark.asyncio
async def test_a_changed_object_is_refused() -> None:
    changed = {**fx.ADDRESS, "comment": "changed by someone"}
    with pytest.raises(FortiOSWriteRefusedError, match="not what the approver saw") as info:
        await _before(fx.FakeFortiGate(_answers(**{_OBJECT_PATH: [changed]})))
    assert info.value.code == "object_changed"


@pytest.mark.asyncio
async def test_a_create_is_refused_when_the_object_already_exists() -> None:
    async def call(connector: FortiOSConnector, target: Any) -> Any:
        return await check_before_write(
            connector,
            make_operator(),
            target,
            obj=_OBJ,
            expected_before=None,
            expected_references=None,
        )

    with pytest.raises(FortiOSWriteRefusedError) as info:
        await _run(fx.FakeFortiGate(_answers()), call)
    assert info.value.code == "object_changed"
    answers = _answers()
    del answers[_OBJECT_PATH]
    before = await _run(fx.FakeFortiGate(answers), call)
    assert before.live is None


@pytest.mark.asyncio
async def test_a_changed_list_of_users_is_refused() -> None:
    with pytest.raises(FortiOSWriteRefusedError, match="policies and groups") as info:
        await _before(fx.FakeFortiGate(_answers()), refs=["firewall/policy:3"])
    assert info.value.code == "references_changed"


@pytest.mark.asyncio
async def test_an_unreadable_list_of_users_is_refused() -> None:
    with pytest.raises(FortiOSWriteRefusedError) as info:
        await _before(fx.FakeFortiGate(_answers(**{_USAGE_PATH: {"made_up": []}})))
    assert info.value.code == "references_unknown"


@pytest.mark.asyncio
async def test_a_change_without_the_list_of_users_is_refused() -> None:
    fake = fx.FakeFortiGate(_answers())
    with pytest.raises(FortiOSWriteRefusedError, match="Preview the change again") as info:
        await _before(fake, refs=None)
    assert info.value.code == "references_unknown"
    assert all(c.method == "GET" for c in fake.calls)
    assert not any(c.url.path.endswith("object/usage") for c in fake.calls)


@pytest.mark.asyncio
async def test_a_create_makes_no_usage_call() -> None:
    answers = _answers()
    del answers[_OBJECT_PATH]
    fake = fx.FakeFortiGate(answers)

    async def call(connector: FortiOSConnector, target: Any) -> Any:
        return await check_before_write(
            connector,
            make_operator(),
            target,
            obj=_OBJ,
            expected_before=None,
            expected_references=[],
        )

    before = await _run(fake, call)
    assert before.live is None
    assert before.references == ()
    assert not any(c.url.path.endswith("object/usage") for c in fake.calls)


@pytest.mark.asyncio
async def test_fortimanager_control_is_refused_with_change_it_in_fortimanager() -> None:
    fake = fx.FakeFortiGate(
        _answers(**{"monitor/system/central-management/status": fx.FMG_CONTROLS})
    )
    with pytest.raises(FortiOSWriteRefusedError, match="change it in FortiManager") as info:
        await _before(fake)
    assert info.value.code == "fortimanager_controls"
    assert _OBJECT_PATH not in fake.paths()


@pytest.mark.parametrize(
    "state",
    [{}, {"mode": "normal"}, {"mode": "made-up", "server": "fmg.test.invalid"}, {"server": ""}],
)
@pytest.mark.asyncio
async def test_an_unknown_fortimanager_state_is_refused(state: dict[str, Any]) -> None:
    fake = fx.FakeFortiGate(_answers(**{"monitor/system/central-management/status": state}))
    with pytest.raises(FortiOSWriteRefusedError, match="could not tell") as info:
        await _before(fake)
    assert info.value.code == "fortimanager_unknown"
    assert _OBJECT_PATH not in fake.paths()


@pytest.mark.asyncio
async def test_backup_mode_allows_a_direct_change() -> None:
    backup = {**fx.FMG_CONTROLS, "mode": "backup"}
    before = await _before(
        fx.FakeFortiGate(_answers(**{"monitor/system/central-management/status": backup}))
    )
    assert before.live == fx.ADDRESS


@pytest.mark.asyncio
async def test_an_untested_version_is_refused() -> None:
    with pytest.raises(FortiOSWriteRefusedError, match="not on the tested list") as info:
        await _before(fx.FakeFortiGate(_answers(), version="v7.8.0"))
    assert info.value.code == "version_untested"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def _send(route_effect: Any) -> tuple[Any, respx.Route]:
    connector = FortiOSConnector(token_loader=token_loader)
    target = FortiTarget(scope="vdom", vdom="vdom-a")
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        route = mock.put(f"/api/v2/{_OBJECT_PATH}").mock(side_effect=route_effect)
        try:
            result = await send_write(
                connector,
                make_operator(),
                target,
                method="PUT",
                obj=_OBJ,
                body={"comment": "new"},
            )
        finally:
            await connector.aclose()
    return result, route


@pytest.mark.asyncio
async def test_a_lost_answer_is_unknown_and_never_sent_again() -> None:
    result, route = await _send(httpx.ReadTimeout("made-up timeout"))
    assert result.outcome == "unknown"
    assert "does not send it again" in str(result.reason)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_an_answered_write_returns_its_results() -> None:
    answer = httpx.Response(200, json=fx.wrap({"mkey": "host-a"}, path="firewall", name="address"))
    result, route = await _send(answer)
    assert (result.outcome, result.results) == ("answered", {"mkey": "host-a"})
    assert route.calls[0].request.url.params["vdom"] == "vdom-a"


@pytest.mark.asyncio
async def test_a_refused_write_is_an_error_sent_once() -> None:
    refused = httpx.Response(500, json=fx.error_answer(500, error=-5, cli_error="made-up"))
    with pytest.raises(FortiOSApiError, match="HTTP 500"):
        await _send(refused)


@pytest.mark.asyncio
async def test_a_connection_that_never_opened_is_raised() -> None:
    with pytest.raises(httpx.ConnectError):
        await _send(httpx.ConnectError("made-up refused"))


# ---------------------------------------------------------------------------
# After the write
# ---------------------------------------------------------------------------


async def _verify(fake: fx.FakeFortiGate, sent: Any, sleeps: _Sleeps | None = None) -> Any:
    async def call(connector: FortiOSConnector, target: Any) -> Any:
        return await verify_after_write(
            connector, make_operator(), target, obj=_OBJ, sent=sent, sleep=sleeps or _Sleeps()
        )

    return await _run(fake, call)


@pytest.mark.asyncio
async def test_a_matching_read_back_and_ha_in_sync_is_verified() -> None:
    result = await _verify(fx.FakeFortiGate(_answers()), {"comment": "made-up host"})
    assert result.outcome == "verified"
    assert result.ha_in_sync is True


@pytest.mark.asyncio
async def test_a_read_back_that_does_not_match_is_not_verified() -> None:
    result = await _verify(fx.FakeFortiGate(_answers()), {"comment": "something else"})
    assert result.outcome == "not_verified"
    assert result.reasons == ("read-back differs in: comment",)


@pytest.mark.asyncio
async def test_a_secret_field_is_not_compared() -> None:
    result = await _verify(fx.FakeFortiGate(_answers()), {"password": "new-made-up"})
    assert result.outcome == "verified"


@pytest.mark.asyncio
async def test_ha_out_of_sync_after_every_try_is_not_verified() -> None:
    sleeps = _Sleeps()
    fake = fx.FakeFortiGate(_answers(**{"monitor/system/ha-checksums": fx.CHECKSUMS_DIFFER}))
    result = await _verify(fake, {"comment": "made-up host"}, sleeps)
    assert result.outcome == "not_verified"
    assert result.ha_in_sync is False
    assert len(sleeps.calls) == HA_ATTEMPTS - 1
    assert fake.paths().count("monitor/system/ha-checksums") == HA_ATTEMPTS


@pytest.mark.asyncio
async def test_ha_that_catches_up_is_verified() -> None:
    fake = fx.FakeFortiGate(_answers())
    states = [fx.CHECKSUMS_DIFFER, fx.CHECKSUMS_SAME]
    original = fake.__call__

    def handler(request: Any) -> Any:
        if request.url.path.endswith("ha-checksums"):
            fake.answers["monitor/system/ha-checksums"] = (
                states.pop(0) if states else fx.CHECKSUMS_SAME
            )
        return original(request)

    connector = FortiOSConnector(token_loader=token_loader)
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=handler)
        result = await verify_after_write(
            connector,
            make_operator(),
            FortiTarget(scope="vdom", vdom="vdom-a"),
            obj=_OBJ,
            sent={"comment": "made-up host"},
            sleep=_Sleeps(),
        )
    await connector.aclose()
    assert (result.outcome, result.ha_in_sync) == ("verified", True)


@pytest.mark.asyncio
async def test_a_single_unit_is_verified() -> None:
    fake = fx.FakeFortiGate(_answers(**{"monitor/system/ha-checksums": fx.CHECKSUMS_SINGLE}))
    result = await _verify(fake, {"comment": "made-up host"})
    assert (result.outcome, result.ha_in_sync, result.reasons) == ("verified", None, ())


@pytest.mark.parametrize(
    "results",
    [
        None,
        {},
        [],
        [{"serial_no": fx.CHECKSUMS_SINGLE[0]["serial_no"]}],
        [{"checksum": fx.CHECKSUMS_SINGLE[0]["checksum"]}],
        [fx.CHECKSUMS_SINGLE[0], {"serial_no": "FGT-made-up-2"}],
        [fx.CHECKSUMS_SINGLE[0], None],
        [fx.CHECKSUMS_SINGLE[0], "made-up text"],
    ],
    ids=[
        "null",
        "empty-object",
        "empty-list",
        "no-checksums",
        "no-serial",
        "one-row-incomplete",
        "one-member-null",
        "one-member-text",
    ],
)
@pytest.mark.asyncio
async def test_an_incomplete_ha_answer_gives_unknown_not_a_single_unit(results: Any) -> None:
    fake = fx.FakeFortiGate(_answers(**{"monitor/system/ha-checksums": results}))
    result = await _verify(fake, {"comment": "made-up host"})
    assert (result.outcome, result.ha_in_sync) == ("unknown", None)
    assert result.read_back == fx.ADDRESS
    assert result.reasons[0].startswith("the HA state is incomplete")
    assert fake.paths().count("monitor/system/ha-checksums") == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(403, json=fx.error_answer(403)), httpx.ReadTimeout("made-up timeout")],
    ids=["forbidden", "timeout"],
)
@pytest.mark.asyncio
async def test_ha_state_that_cannot_be_read_gives_unknown(
    failure: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fx.FakeFortiGate(_answers(**{"monitor/system/ha-checksums": failure}))
    connector = FortiOSConnector(token_loader=token_loader)
    no_retry_wait(connector, monkeypatch)
    try:
        async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
            mock.route().mock(side_effect=fake)
            result = await verify_after_write(
                connector,
                make_operator(),
                FortiTarget(scope="vdom", vdom="vdom-a"),
                obj=_OBJ,
                sent={"comment": "made-up host"},
                sleep=_Sleeps(),
            )
    finally:
        await connector.aclose()
    assert (result.outcome, result.ha_in_sync) == ("unknown", None)
    assert result.read_back == fx.ADDRESS
    assert result.reasons[0].startswith("the HA state could not be read")


@pytest.mark.asyncio
async def test_a_read_back_mismatch_stays_not_verified_when_the_ha_state_is_unreadable() -> None:
    fake = fx.FakeFortiGate(
        _answers(**{"monitor/system/ha-checksums": httpx.Response(403, json=fx.error_answer(403))})
    )
    result = await _verify(fake, {"comment": "something else"})
    assert result.outcome == "not_verified"
    assert result.reasons[0] == "read-back differs in: comment"
    assert result.reasons[1].startswith("the HA state could not be read")


@pytest.mark.asyncio
async def test_a_lost_read_back_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fx.FakeFortiGate(_answers(**{_OBJECT_PATH: httpx.ReadTimeout("made-up")}))
    connector = FortiOSConnector(token_loader=token_loader)
    no_retry_wait(connector, monkeypatch)
    try:
        async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
            mock.route().mock(side_effect=fake)
            result = await verify_after_write(
                connector,
                make_operator(),
                FortiTarget(scope="vdom", vdom="vdom-a"),
                obj=_OBJ,
                sent={"comment": "x"},
                sleep=_Sleeps(),
            )
    finally:
        await connector.aclose()
    assert result.outcome == "unknown"
    assert result.reasons[0].startswith("read-back failed")


@pytest.mark.asyncio
async def test_a_delete_is_verified_when_the_object_is_gone() -> None:
    answers = _answers()
    del answers[_OBJECT_PATH]
    assert (await _verify(fx.FakeFortiGate(answers), None)).outcome == "verified"
    still_there = await _verify(fx.FakeFortiGate(_answers()), None)
    assert still_there.outcome == "not_verified"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_preview_shows_now_after_users_vdom_ha_and_fortimanager() -> None:
    op_id = "fortios.example.address.change"
    register_write_preview(
        op_id,
        WritePreviewSpec(
            action="change", path="firewall/address", mkey_param="name", body_param="fields"
        ),
    )
    builder = _PREVIEW_BUILDERS.pop(op_id)
    connector = FortiOSConnector(token_loader=token_loader)
    target = FortiTarget(scope="vdom", vdom="vdom-a")
    params = {"name": "host-a", "fields": {"comment": "new", "password": "made-up-new-secret"}}
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=connector,
        operator=make_operator(),
        target=target,
        params=copy.deepcopy(params),
    )
    async with respx.mock(base_url=fx.BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fx.FakeFortiGate(_answers()))
        preview = await builder(ctx)
    await connector.aclose()
    assert preview["now"] == fx.ADDRESS
    assert preview["after"]["comment"] == "new"
    assert preview["after"]["password"] == REDACTED
    assert preview["references"] == list(_REFS)
    assert (preview["vdom"], preview["target_scope"]) == ("vdom-a", "vdom")
    assert preview["ha_in_sync"] is True
    assert preview["fortimanager_controls"] is False
    assert preview["expected_before"] == fx.ADDRESS
    assert preview["expected_references"] == list(_REFS)
    assert "made-up-new-secret" not in str(preview)
