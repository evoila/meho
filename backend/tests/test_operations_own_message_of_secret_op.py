# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An operation that sends a secret still shows the text MEHO wrote itself.

The dispatcher hides every vendor text of such an operation. A sentence that
MEHO or a connector wrote itself holds no vendor text, so an error type that
has the ``ConnectorOwnMessage`` mark keeps its ``exception_message``. A
plain error type and every vendor field stay hidden as before.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.connectors.linux.ops_write import (
    LinuxWriteError,
    LinuxWriteOwnMessageError,
    _resolve_sudo_password,
)
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD, withhold_vendor_text

_SECRET_TAGS = ["license", "license-key"]
_OP = "vmware.license.list"
_OWN_TEXT = "the MEHO account may not read licences; check its rights on the target"
# Made up, and not secret-shaped, so only the rule under test can keep it out.
_VENDOR_TEXT = "the value quokka-" + "7781-zebra-pass is not valid"


class _OwnMessageError(ConnectorOwnMessage, RuntimeError):
    pass


def test_own_message_of_a_secret_op_is_shown() -> None:
    built = result_connector_error(_OP, _OwnMessageError(_OWN_TEXT), 1.0)

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == _OWN_TEXT
    assert out.extras["exception_class"] == "_OwnMessageError"
    assert VENDOR_TEXT_WITHHELD not in out.model_dump_json()


def test_vendor_message_of_a_secret_op_stays_hidden() -> None:
    built = result_connector_error(_OP, RuntimeError(_VENDOR_TEXT), 1.0)
    assert "quokka" in built.model_dump_json()  # control: the builder copies it

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    assert "quokka" not in out.model_dump_json()


def test_vendor_fields_stay_hidden_next_to_an_own_message() -> None:
    first = result_connector_error(_OP, _OwnMessageError(_OWN_TEXT), 1.0)
    built = first.model_copy(update={"extras": {**first.extras, "upstream_message": _VENDOR_TEXT}})

    out = withhold_vendor_text(built, op_id=_OP, tags=_SECRET_TAGS)

    assert out.extras["exception_message"] == _OWN_TEXT
    assert out.extras["upstream_message"] == VENDOR_TEXT_WITHHELD


def test_own_message_of_a_plain_op_is_unchanged() -> None:
    built = result_connector_error("op", _OwnMessageError(_OWN_TEXT), 1.0)
    assert withhold_vendor_text(built, op_id="op", tags=["accounts"]) is built


@pytest.mark.asyncio
async def test_linux_sudo_refusal_shows_meho_reason_on_a_secret_op() -> None:
    class _Connector:
        async def _resolve_secret(self, target: object, operator: object) -> dict[str, str]:
            return {"private_key": "x"}  # no sudo_password and no password

    with pytest.raises(LinuxWriteOwnMessageError) as raised:
        await _resolve_sudo_password(_Connector(), object(), None)  # type: ignore[arg-type]

    built = result_connector_error("linux.script.run", raised.value, 1.0)
    out = withhold_vendor_text(built, op_id="linux.script.run", tags=None)

    assert out.extras["exception_class"] == "LinuxWriteOwnMessageError"
    assert "sudo_password" in out.extras["exception_message"]
    assert VENDOR_TEXT_WITHHELD not in out.model_dump_json()


def test_other_linux_write_errors_stay_hidden() -> None:
    built = result_connector_error("linux.script.run", LinuxWriteError(_VENDOR_TEXT), 1.0)
    out = withhold_vendor_text(built, op_id="linux.script.run", tags=None)
    assert out.extras["exception_message"] == VENDOR_TEXT_WITHHELD
