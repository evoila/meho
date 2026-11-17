# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved ``secret.move`` holds to what its approval card said about the sink.

* The card said the sink secret does not exist: the run writes with
  ``cas=0``, so Vault refuses the write if the secret exists by then.
* With ``merge``, the card said the secret or the field does not exist:
  the run refuses when it finds the secret or the field.
* Each refusal is ``SecretSinkChangedError`` and writes nothing, so the
  fields of a sink secret that exists by then stay as they are.
* The card said "not known": the run writes as an ordinary move does.

The sink path is a small in-memory KV-v2 secret with check-and-set; the
Vault client is faked at the single ``_build_client`` seam. No real HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import hvac
import pytest

from ._vault_fakes import install_fake_client
from .test_secret_broker_vault_target import (  # noqa: F401  (fixtures)
    _SECRET_VALUE,
    _env,
    _move,
    _settings_env,
    stub_embedding_service,
)
from .test_secret_move_approved_targets import _approve_and_run, _ask, _card, _message

pytestmark = pytest.mark.usefixtures("_env")

_SRC = "vault-target:vault-src/secret/a#password"
_DST = "vault-target:vault-dst/secret/b#password"
_SINK_PATH = "secret/b"
_CHANGED = "connector_error: SecretSinkChangedError"


@dataclass
class _Sink:
    """The sink secret: ``None`` when it does not exist. Writes check ``cas`` like Vault."""

    data: dict[str, Any] | None = None
    version: int = 0
    forbid_reads: bool = False
    writes: list[dict[str, Any]] = field(default_factory=list)

    def read(self) -> dict[str, Any]:
        if self.forbid_reads:
            raise hvac.exceptions.Forbidden("denied")
        if self.data is None:
            raise hvac.exceptions.InvalidPath("missing")
        return {"data": {"data": dict(self.data), "metadata": {"version": self.version}}}

    def write(self, secret: dict[str, Any], cas: int | None) -> dict[str, Any]:
        current = self.version if self.data is not None else 0
        if cas is not None and cas != current:
            raise hvac.exceptions.InvalidRequest("check-and-set parameter did not match")
        self.writes.append({"secret": dict(secret), "cas": cas})
        self.data, self.version = dict(secret), current + 1
        return {"data": {"version": self.version}}


def _install(monkeypatch: pytest.MonkeyPatch, sink: _Sink) -> Any:
    """Fake Vault whose ``secret/b`` path is *sink*; the source holds the made-up value."""
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    kv = fake.secrets.kv.v2
    real_read, real_write = kv.read_secret_version, kv.create_or_update_secret

    def read(path: str, **kwargs: Any) -> dict[str, Any]:
        return sink.read() if path == _SINK_PATH else real_read(path, **kwargs)

    def write(path: str, secret: dict[str, Any], cas: int | None = None, **kwargs: Any) -> Any:
        if path == _SINK_PATH:
            return sink.write(secret, cas)
        return real_write(path, secret, cas=cas, **kwargs)

    monkeypatch.setattr(kv, "read_secret_version", read)
    monkeypatch.setattr(kv, "create_or_update_secret", write)
    return fake


@pytest.mark.parametrize("merge", [False, True], ids=["replace", "merge"])
async def test_a_sink_secret_made_after_the_approval_refuses_the_run(
    merge: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _Sink()
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST, "merge": merge})
    assert _card(row)["sink"]["secret_exists"] == "no"
    sink.data, sink.version = {"username": "demo", "token": "other"}, 1

    result = await _approve_and_run(row.id)

    assert result.status == "error", result
    assert result.error == _CHANGED
    assert "the sink secret exists now" in _message(result)
    assert "Nothing was written" in _message(result)
    assert sink.writes == []
    assert sink.data == {"username": "demo", "token": "other"}  # no field lost


@pytest.mark.parametrize("merge", [False, True], ids=["replace", "merge"])
async def test_a_sink_still_missing_at_the_run_is_written_with_cas_zero(
    merge: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _Sink()
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST, "merge": merge})

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert sink.writes == [{"secret": {"password": _SECRET_VALUE}, "cas": 0}]


async def test_a_sink_secret_made_between_the_read_and_the_merge_write_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check-and-set covers the time between the run's read and its write."""
    sink = _Sink()
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST, "merge": True})
    real_write = sink.write

    def write_after_another(secret: dict[str, Any], cas: int | None) -> dict[str, Any]:
        if sink.data is None:
            sink.data, sink.version = {"username": "demo"}, 1  # another writer
        return real_write(secret, cas)

    monkeypatch.setattr(sink, "write", write_after_another)

    result = await _approve_and_run(row.id)

    assert result.error == _CHANGED, result
    assert sink.writes == []
    assert sink.data == {"username": "demo"}


async def test_a_sink_field_made_after_the_approval_refuses_the_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Sink(data={"username": "demo"}, version=3)
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST, "merge": True})
    assert _card(row)["sink"]["field_exists"] == "no"
    sink.data, sink.version = {"username": "demo", "password": "set-by-another"}, 4

    result = await _approve_and_run(row.id)

    assert result.error == _CHANGED, result
    assert "the field 'password' of the sink secret exists now" in _message(result)
    assert sink.writes == []
    assert sink.data == {"username": "demo", "password": "set-by-another"}


async def test_a_sink_field_still_missing_at_the_run_is_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Sink(data={"username": "demo"}, version=3)
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST, "merge": True})

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert sink.writes == [{"secret": {"username": "demo", "password": _SECRET_VALUE}, "cas": 3}]


async def test_a_card_that_said_not_known_writes_as_an_ordinary_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Sink(forbid_reads=True)
    _install(monkeypatch, sink)
    row = await _ask({"from": _SRC, "to": _DST})
    assert _card(row)["sink"]["secret_exists"] == "not known"
    sink.forbid_reads, sink.data, sink.version = False, {"username": "demo"}, 2

    result = await _approve_and_run(row.id)

    assert result.status == "ok", result
    assert sink.writes == [{"secret": {"password": _SECRET_VALUE}, "cas": None}]


async def test_an_ordinary_move_writes_without_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the approved run holds to a card; an ordinary move has none."""
    sink = _Sink(data={"username": "demo"}, version=2)
    _install(monkeypatch, sink)

    result = await _move({"from": _SRC, "to": _DST})

    assert result.status == "ok", result
    assert sink.writes == [{"secret": {"password": _SECRET_VALUE}, "cas": None}]
