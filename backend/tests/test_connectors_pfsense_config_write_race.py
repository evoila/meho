# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the pfSense config-write lock and full read-back (#3544).

Every pfSense op that edits ``config.xml`` runs a ``pfSsh.php playback``
fragment. The fragment loads the whole config, edits it, and saves the
whole config again with ``write_config()``. Two such ops that overlap on one
appliance can save an old copy and undo each other's change.

These tests use a small in-memory pfSense. Its playback copies the config
when it starts, waits, and then saves the edited copy -- the same
"save an old copy" behaviour that loses an update. They check that:

* two ops started together on one connector and one target both persist
  (the per-appliance lock runs them one after the other);
* without the lock the same fake really does lose one change, so the first
  test proves the lock, not the fake;
* the full read-back raises when the op's own change was saved but another
  part of ``config.xml`` changed or was lost.

All config.xml fixtures are synthetic (RFC 5737 / RFC 1918 addresses).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.pfsense.ops_write import assert_only_own_object_changed
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str


_TARGET = _StubTarget(name="pfsense-test", host="pfsense.test.invalid")

_ROUTE = "<route><network>10.10.0.0/24</network><gateway>LAB_GW</gateway></route>"
_RETIRING_GW = (
    "<gateway_item><name>RETIRING_GW</name><interface>opt2</interface>"
    "<gateway>192.0.2.2</gateway></gateway_item>"
)
_ALIAS_Y = "<alias><name>ALIAS_Y</name><type>host</type><address>192.0.2.50</address></alias>"
_LAB_GW = (
    "<gateway_item><name>LAB_GW</name><interface>opt1</interface>"
    "<gateway>192.0.2.1</gateway></gateway_item>"
)
# pfSense writes a flag that is on as an empty element.
_DISABLED_USER = "<user><name>audit</name><disabled></disabled></user>"
_CONFIG = (
    "<pfsense>"
    "<revision><time>1</time><description>start</description></revision>"
    f"<system>{_DISABLED_USER}</system>"
    "<staticroutes>"
    f"{_ROUTE}"
    "<route><network>10.20.0.0/24</network><gateway>LAB_GW</gateway></route>"
    "</staticroutes>"
    "<gateways>"
    f"{_LAB_GW}"
    f"{_RETIRING_GW}"
    "</gateways>"
    "<aliases></aliases>"
    "</pfsense>"
)


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _save(config: str, *, drop: str, revision: int) -> str:
    """Return *config* with *drop* removed and a new ``<revision>``, like write_config()."""
    assert drop in config
    edited = config.replace(drop, "")
    start = edited.index("<revision>")
    end = edited.index("</revision>") + len("</revision>")
    return f"{edited[:start]}<revision><time>{revision}</time></revision>{edited[end:]}"


class _FakePfSense:
    """In-memory ``config.xml`` whose playback saves the copy it loaded at start."""

    def __init__(self, config: str, edits: dict[str, str]) -> None:
        self.config = config
        self.edits = edits  # playback script name -> XML snippet the fragment removes
        self.saves = 0
        self.playbacks_in_flight = 0
        self.max_playbacks_in_flight = 0
        # When set, playback loads this old copy instead of the live file.
        self.stale_snapshot: str | None = None

    async def run(self, target: Any, command: str, operator: Any = None) -> Any:
        del target, operator
        await asyncio.sleep(0)
        if command == "cat /cf/conf/config.xml":
            return _proc(self.config)
        if command.startswith("pfSsh.php playback "):
            script = command.removeprefix("pfSsh.php playback ")
            snapshot = self.stale_snapshot if self.stale_snapshot is not None else self.config
            self.playbacks_in_flight += 1
            self.max_playbacks_in_flight = max(
                self.max_playbacks_in_flight, self.playbacks_in_flight
            )
            await asyncio.sleep(0.01)  # the PHP process runs; another save may land now
            self.saves += 1
            self.config = _save(snapshot, drop=self.edits[script], revision=100 + self.saves)
            self.playbacks_in_flight -= 1
            return _proc()
        return _proc()  # stage / rm of the playback script


def _fake_for_route_and_gateway() -> _FakePfSense:
    return _FakePfSense(
        _CONFIG,
        {
            "meho_route_delete_10_10_0_0_24": _ROUTE,
            "meho_gateway_delete_RETIRING_GW": _RETIRING_GW,
        },
    )


async def _delete_route_and_gateway_together(
    connector: PfSenseConnector,
) -> list[Any]:
    return await asyncio.gather(
        connector.route_static_delete(_TARGET, {"network": "10.10.0.0/24"}),
        connector.gateway_delete(_TARGET, {"name": "RETIRING_GW"}),
        return_exceptions=True,
    )


# ---------------------------------------------------------------------------
# Two ops at the same time against one target
# ---------------------------------------------------------------------------


async def test_two_config_edits_on_one_target_both_persist() -> None:
    fake = _fake_for_route_and_gateway()
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", side_effect=fake.run):
        results = await _delete_route_and_gateway_together(connector)

    assert [r["status"] for r in results] == ["deleted", "deleted"]
    assert fake.max_playbacks_in_flight == 1
    # Both changes are in the saved file: neither op undid the other.
    assert _ROUTE not in fake.config
    assert _RETIRING_GW not in fake.config


async def test_without_the_lock_the_fake_loses_one_change() -> None:
    # Control: hand out a new lock on every call, so nothing is serialized.
    # The fake then shows the lost update, and the op whose change was undone
    # fails loud instead of reporting success.
    fake = _fake_for_route_and_gateway()
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", side_effect=fake.run),
        patch.object(connector, "config_write_lock", side_effect=lambda _t: asyncio.Lock()),
    ):
        results = await _delete_route_and_gateway_together(connector)

    assert fake.max_playbacks_in_flight == 2
    route_result, gateway_result = results
    assert isinstance(route_result, RuntimeError)
    assert "verification failed" in str(route_result)
    assert gateway_result["status"] == "deleted"
    assert _ROUTE in fake.config  # the route delete was undone by the gateway save


def test_lock_is_shared_per_appliance_host() -> None:
    connector = PfSenseConnector()
    same_host = _StubTarget(name="other-name", host="PFSENSE.test.invalid")
    other_host = _StubTarget(name="pfsense-test", host="other.test.invalid")
    assert connector.config_write_lock(_TARGET) is connector.config_write_lock(same_host)
    assert connector.config_write_lock(_TARGET) is not connector.config_write_lock(other_host)


# ---------------------------------------------------------------------------
# Full read-back: own change saved, but something else changed or was lost
# ---------------------------------------------------------------------------


async def test_full_readback_flags_a_change_lost_by_a_stale_save() -> None:
    # Another writer added ALIAS_Y before this op started, so the op's first
    # read sees it. The playback then saves an old copy without ALIAS_Y. The
    # route delete itself persisted, so only the full read-back can tell.
    fake = _fake_for_route_and_gateway()
    fake.stale_snapshot = fake.config
    fake.config = fake.config.replace("<aliases></aliases>", f"<aliases>{_ALIAS_Y}</aliases>")
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", side_effect=fake.run),
        pytest.raises(RuntimeError) as excinfo,
    ):
        await connector.route_static_delete(_TARGET, {"network": "10.10.0.0/24"})

    message = str(excinfo.value)
    assert "route.static.delete: the change was saved" in message
    assert "(aliases)" in message
    assert _ROUTE not in fake.config
    assert _ALIAS_Y not in fake.config


async def test_full_readback_flags_another_save_that_landed_during_the_op() -> None:
    fake = _fake_for_route_and_gateway()
    original_run = fake.run

    async def run_with_outside_save(target: Any, command: str, operator: Any = None) -> Any:
        result = await original_run(target, command, operator)
        if command.startswith("pfSsh.php playback "):
            # Someone else saves right after this op's save, before its read-back.
            fake.config = fake.config.replace(_RETIRING_GW, "")
        return result

    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", side_effect=run_with_outside_save),
        pytest.raises(RuntimeError, match=r"route\.static\.delete: the change was saved.*gateways"),
    ):
        await connector.route_static_delete(_TARGET, {"network": "10.10.0.0/24"})


def _check(before: str, after: str) -> None:
    assert_only_own_object_changed(
        before,
        after,
        op="gateway.delete",
        section="gateways",
        item_tag="gateway_item",
        key_tag="name",
        key="RETIRING_GW",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Only the edited object and the revision changed.
        (_CONFIG, _save(_CONFIG, drop=_RETIRING_GW, revision=7)),
        # Same content, different whitespace.
        (_CONFIG, _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace("><", ">\n  <")),
        # Deleting the last item leaves the edited section as an empty element.
        (
            _CONFIG.replace(_LAB_GW, ""),
            _save(_CONFIG.replace(_LAB_GW, ""), drop=_RETIRING_GW, revision=7),
        ),
        # Deleting the last item may also drop the edited section itself.
        (
            _CONFIG.replace(_LAB_GW, ""),
            _save(_CONFIG.replace(_LAB_GW, ""), drop=_RETIRING_GW, revision=7).replace(
                "<gateways></gateways>", ""
            ),
        ),
        # Adding the first item creates the edited section.
        (
            _save(_CONFIG, drop=f"<gateways>{_LAB_GW}{_RETIRING_GW}</gateways>", revision=1),
            _save(_CONFIG, drop=_LAB_GW, revision=7),
        ),
    ],
)
def test_full_readback_accepts_only_own_change(before: str, after: str) -> None:
    _check(before, after)


@pytest.mark.parametrize(
    ("after", "changed"),
    [
        (_save(_CONFIG, drop=_ROUTE, revision=7), "staticroutes"),
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace("192.0.2.1", "192.0.2.9"),
            "gateways",
        ),
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace(
                "<aliases></aliases>", f"<aliases>{_ALIAS_Y}</aliases>"
            ),
            "aliases",
        ),
        # A flag on an unrelated object was lost: the user is enabled again.
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace("<disabled></disabled>", ""),
            "system",
        ),
        # A flag was added to another object in the edited section.
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace(
                "<name>LAB_GW</name>", "<name>LAB_GW</name><disabled></disabled>"
            ),
            "gateways",
        ),
        # An unrelated empty section appeared.
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace(
                "</pfsense>", "<widgets></widgets></pfsense>"
            ),
            "widgets",
        ),
        # An unrelated empty section went away.
        (
            _save(_CONFIG, drop=_RETIRING_GW, revision=7).replace("<aliases></aliases>", ""),
            "aliases",
        ),
    ],
)
def test_full_readback_rejects_any_other_change(after: str, changed: str) -> None:
    with pytest.raises(RuntimeError, match=rf"the change was saved.*\({changed}\)"):
        _check(_CONFIG, after)


def test_full_readback_fails_closed_on_unparseable_config() -> None:
    with pytest.raises(RuntimeError, match="could not be parsed"):
        _check(_CONFIG, "<pfsense")
