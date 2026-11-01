# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host advanced settings, firewall, lockdown, SNMP and core dump, against a fake vCenter.

Checks the two reads and the six changes: the request bodies, the read-back,
``unchanged`` (nothing sent), the refusals before a call, a vendor fault, a
change that waits for approval, the approval previews, and that no secret
(SNMP community, a setting that names a password) reaches an answer.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import (
    _host_access_writes,
    _host_security_writes,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._host_access_writes import (
    host_coredump_set_composite,
    host_lockdown_exceptions_set_composite,
    host_lockdown_set_composite,
    host_snmp_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_schemas import (
    ADVANCED_SETTINGS_SET,
)
from meho_backplane.connectors.vmware_rest.composites._host_security_writes import (
    host_advanced_settings_set_composite,
    host_firewall_ruleset_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.host_security import is_secret_key
from meho_backplane.connectors.vmware_rest.typed_ops_host_security import (
    advanced_settings_show_impl,
    firewall_show_impl,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import SecretValueError
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._secret_values import plain_fields_of
from meho_backplane.operations._validate import validate_params
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from tests._vmware_host_security_fake import (
    HOST,
    OLD_COMMUNITY,
    SECRET_PASSWORD,
    HostSecurityVim,
)
from tests._vmware_teardown_fake import GateRecorder

NEW_COMMUNITY = "new-community-value"
NEW_PASSWORD = "new-password-value"
SECRET_KEY = "UserVars.ExamplePassword"
_CHANGES = (
    "vmware.composite.host.advanced_settings.set",
    "vmware.composite.host.firewall.ruleset.set",
    "vmware.composite.host.lockdown.set",
    "vmware.composite.host.lockdown.exceptions.set",
    "vmware.composite.host.snmp.set",
    "vmware.composite.host.coredump.set",
)


def _operator() -> Operator:
    return Operator(
        sub="op-host-security",
        name="Host Security Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004352"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _Target:
    name = "vc-test"
    host = "vc.test.invalid"
    fingerprint: Any = None


class _EsxiTarget(_Target):
    def __init__(self) -> None:
        self.fingerprint = {"product": "esxi", "reachable": True}


def _fault(fault_type: str, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.0.0.0/x")
    response = httpx.Response(
        500, json={"_typeName": fault_type, "faultstring": text}, request=request
    )
    return httpx.HTTPStatusError("500 Server Error", request=request, response=response)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-in for the Vault read of the SNMP community."""
    seen: list[dict[str, Any]] = []

    async def read(operator: Any, target: Any, params: Any, *, prefix: str) -> str:
        seen.append({"prefix": prefix, **params})
        return NEW_COMMUNITY

    monkeypatch.setattr(_host_access_writes, "read_secret_param", read)
    return seen


@pytest.fixture
def settings_vault(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-in for the Vault read of a secret advanced setting."""
    seen: list[dict[str, Any]] = []

    async def read(operator: Any, target: Any, params: Any, *, prefix: str) -> str:
        seen.append({"prefix": prefix, **params})
        return NEW_PASSWORD

    monkeypatch.setattr(_host_security_writes, "read_secret_param", read)
    return seen


async def _call(handler: Any, vim: HostSecurityVim, params: dict[str, Any]) -> Any:
    return await handler(operator=_operator(), target=_Target(), params=params, connector=vim)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_advanced_settings_show_marks_changed_values_and_hides_secrets() -> None:
    vim = HostSecurityVim()
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok", out
    rows = {row["key"]: row for row in out["settings"]}
    assert list(rows) == sorted(rows)
    assert rows["Net.BlockGuestBPDU"]["is_default"] is False
    assert rows["Net.BlockGuestBPDU"]["value_type"] == "int"
    assert rows["UserVars.SuppressShellWarning"]["is_default"] is True
    assert rows["UserVars.SuppressShellWarning"]["value_type"] == "long"
    assert rows["Misc.ReadOnlyThing"]["read_only"] is True
    assert rows["UserVars.ExamplePassword"]["value"] == "<hidden>"
    assert rows["Security.PasswordHistory"]["value"] == 0
    assert rows["Security.PasswordQualityControl"]["value"] == "retry=3 min=disabled,disabled,7"
    assert SECRET_PASSWORD not in json.dumps(out)
    assert vim.body_of("QueryOptions") == {}


@pytest.mark.parametrize(
    ("key", "definition", "secret"),
    [
        ("UserVars.ExamplePassword", None, True),
        ("UserVars.ExamplePassword", {"value_type": "string", "choices": None}, True),
        ("Config.HostAgent.plugins.vimsvc.authToken", None, True),
        ("Snmp.Community", None, True),
        # A password policy setting names a password but holds a rule about it.
        ("Security.PasswordHistory", None, False),
        ("Security.PasswordMaxDays", None, False),
        ("Security.PasswordQualityControl", None, False),
        # A number, a switch or a choice never holds a secret.
        ("UserVars.TokenTimeout", {"value_type": "int", "choices": None}, False),
        ("UserVars.TokenCheck", {"value_type": "boolean", "choices": None}, False),
        ("UserVars.TokenMode", {"value_type": "string", "choices": ["on", "off"]}, False),
        ("UserVars.TokenTimeout", None, True),
        ("Net.BlockGuestBPDU", None, False),
    ],
)
def test_is_secret_key_tells_a_secret_from_a_policy_or_a_number(
    key: str, definition: dict[str, Any] | None, secret: bool
) -> None:
    assert is_secret_key(key, definition) is secret


async def test_advanced_settings_show_filters_by_prefix_and_changed_only() -> None:
    vim = HostSecurityVim()
    out = await advanced_settings_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "prefix": "Net.", "changed_only": True},
    )
    assert [row["key"] for row in out["settings"]] == ["Net.BlockGuestBPDU"]
    assert vim.body_of("QueryOptions") == {"name": "Net."}


async def test_advanced_settings_show_unknown_prefix() -> None:
    vim = HostSecurityVim()
    vim.fail["QueryOptions"] = _fault("InvalidName", "no such option")
    out = await advanced_settings_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "prefix": "Nope."},
    )
    assert out["status"] == "no_such_setting"
    assert out["settings"] == []


async def test_advanced_settings_show_without_defaults_still_lists_values() -> None:
    vim = HostSecurityVim()
    vim.definitions = None  # type: ignore[assignment]

    def broken(mo_type: str, prop: str) -> Any:
        raise RuntimeError("defaults unreadable")

    vim._value = broken  # type: ignore[method-assign]
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok"
    assert out["read_notes"] and "defaults" in out["read_notes"][0]
    assert all(row["is_default"] is None for row in out["settings"])


async def test_firewall_show_lists_rulesets_and_filters() -> None:
    vim = HostSecurityVim()
    out = await firewall_show_impl(vim, _operator(), _Target(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "ok"
    assert out["default_policy"] == {"incoming_blocked": True, "outgoing_blocked": True}
    ssh = out["rulesets"][0]
    assert ssh["key"] == "sshServer"
    assert ssh["allowed"] == {"all_ip": True, "ip_addresses": [], "networks": []}
    assert ssh["rules"] == [
        {
            "direction": "inbound",
            "protocol": "tcp",
            "port": 22,
            "end_port": None,
            "port_type": "dst",
        }
    ]
    enabled = await firewall_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "enabled_only": True},
    )
    assert [r["key"] for r in enabled["rulesets"]] == ["vpxHeartbeats"]
    missing = await firewall_show_impl(
        vim,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"host": "esxi-a", "ruleset": "nope"},
    )
    assert missing["status"] == "ruleset_not_found"


async def test_reads_refuse_a_single_esxi_target_and_an_unknown_host() -> None:
    vim = HostSecurityVim()
    out = await firewall_show_impl(vim, _operator(), _EsxiTarget(), {"host": "esxi-a"})  # type: ignore[arg-type]
    assert out["status"] == "needs_vcenter"
    out = await advanced_settings_show_impl(vim, _operator(), _Target(), {"host": "nope"})  # type: ignore[arg-type]
    assert out["status"] == "host_not_found"
    assert vim.calls == []


# ---------------------------------------------------------------------------
# host.advanced_settings.set
# ---------------------------------------------------------------------------


async def test_advanced_settings_set_sends_the_typed_value_and_reads_back(
    gate: GateRecorder,
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {
            "host": "esxi-a",
            "settings": [
                {"key": "UserVars.SuppressShellWarning", "value": 1},
                {"key": "Net.BlockGuestBPDU", "value": 1},
            ],
        },
    )
    assert out["status"] == "applied", out
    assert vim.writes() == ["UpdateOptions"]
    assert vim.body_of("UpdateOptions") == {
        "changedValue": [
            {
                "_typeName": "OptionValue",
                "key": "UserVars.SuppressShellWarning",
                "value": {"_typeName": "long", "_value": 1},
            }
        ]
    }
    assert out["before"]["UserVars.SuppressShellWarning"] == 0
    assert out["after"]["UserVars.SuppressShellWarning"] == 1
    assert gate.calls[0]["op_id"] == "POST:/OptionManager/{moId}/UpdateOptions"


@pytest.mark.parametrize(
    ("key", "value", "value_type"),
    [
        ("Security.PasswordHistory", 3, "int"),
        ("Security.PasswordQualityControl", "retry=5 min=disabled,disabled,8", "string"),
    ],
)
async def test_advanced_settings_set_takes_a_plain_value_for_a_password_policy_setting(
    gate: GateRecorder, settings_vault: list[dict[str, Any]], key: str, value: Any, value_type: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": key, "value": value}]},
    )
    assert out["status"] == "applied", out
    assert vim.writes() == ["UpdateOptions"]
    assert vim.body_of("UpdateOptions")["changedValue"][0]["value"] == {
        "_typeName": value_type,
        "_value": value,
    }
    # The policy holds no secret, so the answer shows it.
    assert out["requested"]["settings"] == {key: value}
    assert out["after"][key] == value
    assert "<hidden>" not in json.dumps(out)
    assert settings_vault == []


def test_advanced_settings_set_shows_the_password_policy_values_in_the_answer() -> None:
    """The default answer view hides a password-named key; these hold no secret."""
    plain = plain_fields_of("vmware.composite.host.advanced_settings.set")
    assert plain == {
        "Security.PasswordHistory",
        "Security.PasswordMaxDays",
        "Security.PasswordQualityControl",
    }
    assert all(not is_secret_key(key) for key in plain)


async def test_advanced_settings_set_checks_a_password_policy_value(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": "Security.PasswordHistory", "value": 9}]},
    )
    assert out["status"] == "invalid_value", out
    assert out["keys"] == ["Security.PasswordHistory"]
    assert "between 0 and 5" in out["guidance"]
    assert vim.writes() == []


async def test_advanced_settings_set_unchanged_sends_nothing(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": "Net.BlockGuestBPDU", "value": 1}]},
    )
    assert out["status"] == "unchanged"
    assert vim.writes() == []
    assert gate.calls == []


@pytest.mark.parametrize(
    ("setting", "status"),
    [
        ({"key": "No.Such.Key", "value": 1}, "unknown_setting"),
        ({"key": "Misc.ReadOnlyThing", "value": "x"}, "read_only_setting"),
        ({"key": "Config.HostAgent.plugins.solo.enableMob", "value": 1}, "invalid_value"),
        ({"key": "UserVars.SuppressShellWarning", "value": 5}, "invalid_value"),
        ({"key": "UserVars.SuppressShellWarning", "value": True}, "invalid_value"),
    ],
)
async def test_advanced_settings_set_checks_before_sending(
    gate: GateRecorder, setting: dict[str, Any], status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite, vim, {"host": "esxi-a", "settings": [setting]}
    )
    assert out["status"] == status, out
    assert out["keys"] == [setting["key"]]
    assert vim.writes() == []


async def test_advanced_settings_set_refuses_a_key_given_twice(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    key = "UserVars.SuppressShellWarning"
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": key, "value": 0}, {"key": key, "value": 1}]},
    )
    assert out["status"] == "invalid_value", out
    assert out["keys"] == [key]
    assert vim.writes() == []


async def test_advanced_settings_set_refuses_a_plain_secret_value(
    gate: GateRecorder, settings_vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value": "new-pw"}]},
    )
    assert out["status"] == "secret_value_required", out
    assert out["keys"] == [SECRET_KEY]
    assert "value_secret_ref" in out["guidance"]
    assert vim.writes() == []
    assert settings_vault == []
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert "new-pw" not in text
    assert SECRET_PASSWORD not in text


@pytest.mark.parametrize(
    ("setting", "problem"),
    [
        ({"key": "Security.PasswordHistory", "value": 3}, None),
        ({"key": SECRET_KEY, "value_secret_ref": "esxi/example"}, None),
        ({"key": SECRET_KEY, "value_secret_ref": "esxi/example", "value_secret_key": "pw"}, None),
        ({"key": SECRET_KEY, "value": "x", "value_secret_ref": "esxi/example"}, "oneOf"),
        ({"key": SECRET_KEY}, "oneOf"),
        ({"key": SECRET_KEY, "value_secret_mount": "kv"}, "oneOf"),
    ],
)
def test_advanced_settings_set_schema_takes_value_or_secret_ref_one_of_them(
    setting: dict[str, Any], problem: str | None
) -> None:
    """The schema says it with ``oneOf``: exactly one of value and value_secret_ref."""
    errors = validate_params(
        ADVANCED_SETTINGS_SET["parameter_schema"], {"host": "esxi-a", "settings": [setting]}
    )
    if problem is None:
        assert errors == []
    else:
        assert [e["validator"] for e in errors] == [problem], errors
        assert errors[0]["path"] == "$.settings[0]"


async def test_advanced_settings_set_reads_a_secret_from_vault_and_never_shows_it(
    gate: GateRecorder, settings_vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {
            "host": "esxi-a",
            "settings": [
                {"key": SECRET_KEY, "value_secret_ref": "esxi/example"},
                {"key": "Net.BlockGuestBPDU", "value": 0},
            ],
        },
    )
    assert out["status"] == "applied", out
    assert settings_vault == [
        {
            "prefix": "value",
            "key": SECRET_KEY,
            "value_secret_ref": "esxi/example",
            "value_secret_key": "value",
        }
    ]
    body = vim.body_of("UpdateOptions")
    assert body["changedValue"][0] == {
        "_typeName": "OptionValue",
        "key": SECRET_KEY,
        "value": {"_typeName": "string", "_value": NEW_PASSWORD},
    }
    assert vim.options[SECRET_KEY] == ("string", NEW_PASSWORD)
    assert out["requested"]["settings"] == {SECRET_KEY: "<hidden>", "Net.BlockGuestBPDU": 0}
    assert out["before"] == {SECRET_KEY: "<hidden>", "Net.BlockGuestBPDU": 1}
    assert out["after"] == {SECRET_KEY: "<hidden>", "Net.BlockGuestBPDU": 0}
    assert gate.calls[0]["params"]["settings"] == {SECRET_KEY: "<hidden>", "Net.BlockGuestBPDU": 0}
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert NEW_PASSWORD not in text
    assert SECRET_PASSWORD not in text


async def test_advanced_settings_set_unchanged_secret_sends_nothing(
    gate: GateRecorder, settings_vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.options[SECRET_KEY] = ("string", NEW_PASSWORD)
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value_secret_ref": "esxi/example"}]},
    )
    assert out["status"] == "unchanged", out
    assert out["after"] == {SECRET_KEY: "<hidden>"}
    assert vim.writes() == []
    assert NEW_PASSWORD not in json.dumps(out)


@pytest.mark.parametrize(
    ("setting", "status"),
    [
        ({"key": "Net.BlockGuestBPDU", "value_secret_ref": "esxi/example"}, "invalid_value"),
        ({"key": SECRET_KEY, "value": "x", "value_secret_ref": "esxi/example"}, "invalid_value"),
        ({"key": SECRET_KEY}, "invalid_value"),
        ({"key": "No.Such.Key", "value_secret_ref": "esxi/example"}, "unknown_setting"),
    ],
)
async def test_advanced_settings_set_checks_a_secret_ref_before_vault(
    gate: GateRecorder,
    settings_vault: list[dict[str, Any]],
    setting: dict[str, Any],
    status: str,
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite, vim, {"host": "esxi-a", "settings": [setting]}
    )
    assert out["status"] == status, out
    assert out["keys"] == [setting["key"]]
    assert vim.writes() == []
    assert settings_vault == []


async def test_advanced_settings_set_unusable_secret_sends_nothing(
    gate: GateRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def read(operator: Any, target: Any, params: Any, *, prefix: str) -> str:
        raise SecretValueError(
            "the Vault secret at value_secret_ref='esxi/example' (mount='secret') has no text "
            "under the key 'value'. Store the value under that key, or set value_secret_key. "
            "Nothing was sent."
        )

    monkeypatch.setattr(_host_security_writes, "read_secret_param", read)
    vim = HostSecurityVim()
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value_secret_ref": "esxi/example"}]},
    )
    assert out["status"] == "value_secret_unusable", out
    assert out["keys"] == [SECRET_KEY]
    assert out["guidance"].startswith(f"{SECRET_KEY}: the Vault secret")
    assert out["guidance"].endswith("Nothing was sent.")
    assert vim.methods() == ["RetrievePropertiesEx", "RetrievePropertiesEx"]


async def test_advanced_settings_set_hides_the_secret_in_a_fault(
    gate: GateRecorder, settings_vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.fail["UpdateOptions"] = _fault("InvalidArgument", f"bad value {NEW_PASSWORD}")
    out = await _call(
        host_advanced_settings_set_composite,
        vim,
        {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value_secret_ref": "esxi/example"}]},
    )
    assert out["status"] == "failed", out
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert NEW_PASSWORD not in text
    assert "InvalidArgument" not in out["fault"] or NEW_PASSWORD not in out["fault"]


def test_advanced_settings_set_is_a_credential_write() -> None:
    """The pin keeps the params off the broadcast and the bodies out of the trace."""
    op_id = "vmware.composite.host.advanced_settings.set"
    params = {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value_secret_ref": "x"}]}
    assert classify_op(op_id) == "credential_write"
    assert redact_payload("credential_write", params, "ok") == {
        "op_class": "credential_write",
        "result_status": "ok",
    }
    assert classify_body_exclusion(op_id).excluded is True


async def test_advanced_settings_set_fault_and_not_verified(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    vim.fail["UpdateOptions"] = _fault("InvalidArgument", "A specified parameter was not correct")
    params = {"host": "esxi-a", "settings": [{"key": "Net.BlockGuestBPDU", "value": 0}]}
    out = await _call(host_advanced_settings_set_composite, vim, params)
    assert out["status"] == "failed"
    assert "InvalidArgument" in out["fault"]
    assert out["issues"][0]["severity"] == "error"
    vim = HostSecurityVim()
    vim.apply = False
    out = await _call(host_advanced_settings_set_composite, vim, params)
    assert out["status"] == "not_verified"


async def test_a_parked_change_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(status="awaiting_approval", op_id="x", result=None, duration_ms=1.0)
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(parked))
    vim = HostSecurityVim()
    out = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "normal"})
    assert out is parked
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# host.firewall.ruleset.set
# ---------------------------------------------------------------------------


async def test_firewall_ruleset_set_limits_addresses_then_turns_on(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_firewall_ruleset_set_composite,
        vim,
        {
            "host": "esxi-a",
            "ruleset": "sshServer",
            "enabled": True,
            "allowed": {"all_ip": False, "networks": ["192.0.2.0/24"], "ip_addresses": []},
        },
    )
    assert out["status"] == "applied", out
    assert vim.writes() == ["UpdateRuleset", "EnableRuleset"]
    assert vim.body_of("UpdateRuleset") == {
        "id": "sshServer",
        "spec": {
            "_typeName": "HostFirewallRulesetRulesetSpec",
            "allowedHosts": {
                "_typeName": "HostFirewallRulesetIpList",
                "allIp": False,
                "ipAddress": [],
                "ipNetwork": [
                    {
                        "_typeName": "HostFirewallRulesetIpNetwork",
                        "network": "192.0.2.0",
                        "prefixLength": 24,
                    }
                ],
            },
        },
    }
    assert out["after"]["enabled"] is True
    assert out["after"]["allowed"]["networks"] == ["192.0.2.0/24"]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"ruleset": "vpxHeartbeats", "enabled": False}, "ruleset_required"),
        ({"ruleset": "nope", "enabled": True}, "ruleset_not_found"),
        ({"ruleset": "sshServer", "allowed": {"all_ip": False}}, "invalid_value"),
        (
            {"ruleset": "sshServer", "allowed": {"all_ip": True, "networks": ["192.0.2.0/24"]}},
            "invalid_value",
        ),
        (
            {"ruleset": "sshServer", "allowed": {"all_ip": False, "networks": ["x/99"]}},
            "invalid_value",
        ),
        ({"ruleset": "sshServer", "enabled": False}, "unchanged"),
    ],
)
async def test_firewall_ruleset_set_refusals(
    gate: GateRecorder, params: dict[str, Any], status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(host_firewall_ruleset_set_composite, vim, {"host": "esxi-a", **params})
    assert out["status"] == status, out
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# Lockdown mode and its exception users
# ---------------------------------------------------------------------------


async def test_lockdown_set_and_unchanged(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "strict"})
    assert out["status"] == "applied"
    assert vim.body_of("ChangeLockdownMode") == {"mode": "lockdownStrict"}
    assert out["before"] == {"mode": "disabled"}
    assert out["after"] == {"mode": "strict"}
    again = await _call(host_lockdown_set_composite, vim, {"host": "esxi-a", "mode": "strict"})
    assert again["status"] == "unchanged"
    assert vim.writes() == ["ChangeLockdownMode"]


async def test_lockdown_exceptions_set(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_lockdown_exceptions_set_composite,
        vim,
        {"host": "esxi-a", "users": ["svc-b", "svc-a"]},
    )
    assert out["status"] == "applied"
    assert vim.body_of("UpdateLockdownExceptions") == {"users": ["svc-a", "svc-b"]}
    assert out["after"] == {"users": ["svc-a", "svc-b"]}
    again = await _call(
        host_lockdown_exceptions_set_composite,
        vim,
        {"host": "esxi-a", "users": ["svc-a", "svc-b"]},
    )
    assert again["status"] == "unchanged"


# ---------------------------------------------------------------------------
# host.snmp.set
# ---------------------------------------------------------------------------


async def test_snmp_set_reads_the_community_from_vault_and_never_shows_it(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_snmp_set_composite,
        vim,
        {
            "host": "esxi-a",
            "enabled": True,
            "community_secret_ref": "snmp/example",
            "trap_targets": [{"host_name": "trap-new.example.invalid"}],
        },
    )
    assert out["status"] == "applied", out
    spec = vim.body_of("ReconfigureSnmpAgent")["spec"]
    assert spec["readOnlyCommunities"] == [NEW_COMMUNITY]
    assert spec["trapTargets"] == [
        {
            "_typeName": "HostSnmpDestination",
            "hostName": "trap-new.example.invalid",
            "port": 162,
            "community": NEW_COMMUNITY,
        }
    ]
    assert spec["option"] == [{"_typeName": "KeyValue", "key": "syscontact", "value": "ops"}]
    assert vault[0]["community_secret_key"] == "community"
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert NEW_COMMUNITY not in text
    assert OLD_COMMUNITY not in text
    assert out["after"]["read_only_community_count"] == 1
    assert out["after"]["trap_targets"] == [{"host_name": "trap-new.example.invalid", "port": 162}]


async def test_snmp_set_is_not_verified_when_the_host_keeps_the_old_community(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.keep_communities = True
    params = {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"}
    out = await _call(host_snmp_set_composite, vim, params)
    assert out["status"] == "not_verified", out
    assert vim.writes() == ["ReconfigureSnmpAgent"]
    assert out["after"]["enabled"] is True
    assert out["after"]["read_only_community_count"] == 1
    assert out["issues"][0]["severity"] == "error"
    text = json.dumps(out) + json.dumps(gate.calls, default=str)
    assert NEW_COMMUNITY not in text
    assert OLD_COMMUNITY not in text


async def test_snmp_set_keeps_fields_left_out(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(host_snmp_set_composite, vim, {"host": "esxi-a", "enabled": True})
    assert out["status"] == "applied"
    spec = vim.body_of("ReconfigureSnmpAgent")["spec"]
    assert spec["readOnlyCommunities"] == [OLD_COMMUNITY]
    assert spec["trapTargets"][0]["hostName"] == "trap-old.example.invalid"
    assert spec["port"] == 161
    again = await _call(host_snmp_set_composite, vim, {"host": "esxi-a", "enabled": True})
    assert again["status"] == "unchanged"


async def test_snmp_set_needs_a_community_for_traps(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    vim.snmp = {"_typeName": "HostSnmpConfigSpec", "enabled": False, "port": 161}
    out = await _call(
        host_snmp_set_composite,
        vim,
        {
            "host": "esxi-a",
            "enabled": True,
            "trap_targets": [{"host_name": "trap.example.invalid"}],
        },
    )
    assert out["status"] == "community_required"
    assert vim.writes() == []


async def test_snmp_set_hides_the_community_in_a_fault(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.fail["ReconfigureSnmpAgent"] = _fault("InvalidArgument", f"bad community {NEW_COMMUNITY}")
    out = await _call(
        host_snmp_set_composite,
        vim,
        {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"},
    )
    assert out["status"] == "failed"
    assert NEW_COMMUNITY not in json.dumps(out)


@pytest.mark.parametrize("echoed", [OLD_COMMUNITY, "trap-only-community"])
async def test_snmp_set_hides_a_kept_community_in_a_fault(gate: GateRecorder, echoed: str) -> None:
    vim = HostSecurityVim()
    vim.snmp["trapTargets"][0]["community"] = "trap-only-community"
    vim.fail["ReconfigureSnmpAgent"] = _fault("InvalidArgument", f"bad community {echoed}")
    out = await _call(host_snmp_set_composite, vim, {"host": "esxi-a", "enabled": True})
    assert out["status"] == "failed"
    assert echoed not in json.dumps(out)


async def test_snmp_set_puts_a_new_community_on_kept_trap_targets(
    gate: GateRecorder, vault: list[dict[str, Any]]
) -> None:
    vim = HostSecurityVim()
    vim.snmp["enabled"] = True
    params = {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"}
    out = await _call(host_snmp_set_composite, vim, params)
    assert out["status"] == "applied", out
    spec = vim.body_of("ReconfigureSnmpAgent")["spec"]
    assert spec["readOnlyCommunities"] == [NEW_COMMUNITY]
    assert spec["trapTargets"][0]["hostName"] == "trap-old.example.invalid"
    assert spec["trapTargets"][0]["community"] == NEW_COMMUNITY
    again = await _call(host_snmp_set_composite, vim, params)
    assert again["status"] == "unchanged"


# ---------------------------------------------------------------------------
# host.coredump.set
# ---------------------------------------------------------------------------


async def test_coredump_set_picks_a_free_partition(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_coredump_set_composite, vim, {"host": "esxi-a", "disk_name": "disk-b", "partition": 9}
    )
    assert out["status"] == "applied", out
    assert vim.body_of("SelectActivePartition") == {
        "partition": {"_typeName": "HostScsiDiskPartition", "diskName": "disk-b", "partition": 9}
    }
    assert out["after"]["active"]["disk_name"] == "disk-b"


@pytest.mark.parametrize(
    ("disk", "number", "status"),
    [("disk-a", 7, "unchanged"), ("disk-c", 1, "partition_not_available")],
)
async def test_coredump_set_refusals(
    gate: GateRecorder, disk: str, number: int, status: str
) -> None:
    vim = HostSecurityVim()
    out = await _call(
        host_coredump_set_composite, vim, {"host": "esxi-a", "disk_name": disk, "partition": number}
    )
    assert out["status"] == status
    assert len(out["before"]["available"]) == 2
    assert vim.writes() == []


# ---------------------------------------------------------------------------
# Every change: host refusal, grant set, preview
# ---------------------------------------------------------------------------


async def test_a_change_on_a_single_esxi_target_needs_vcenter(gate: GateRecorder) -> None:
    vim = HostSecurityVim()
    out = await host_lockdown_set_composite(
        operator=_operator(), target=_EsxiTarget(), params={"mode": "normal"}, connector=vim
    )
    assert out["status"] == "needs_vcenter"
    assert vim.calls == []


def test_every_change_lists_its_vim_calls() -> None:
    for op_id in _CHANGES:
        calls = _GOVERNED_SUBOP_MANIFEST[op_id]
        assert calls[0] == "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
        assert len(calls) >= 2


def _preview_ctx(vim: HostSecurityVim, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=vim,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


@pytest.mark.parametrize(
    ("op_id", "params", "current"),
    [
        ("vmware.composite.host.lockdown.set", {"mode": "normal"}, {"mode": "disabled"}),
        ("vmware.composite.host.lockdown.exceptions.set", {"users": []}, {"users": ["svc-old"]}),
        (
            "vmware.composite.host.advanced_settings.set",
            {"settings": [{"key": SECRET_KEY, "value_secret_ref": "esxi/example"}]},
            {SECRET_KEY: "<hidden>"},
        ),
        (
            "vmware.composite.host.advanced_settings.set",
            {"settings": [{"key": "Syslog.global.logDir", "value_secret_ref": "esxi/example"}]},
            {"Syslog.global.logDir": "<hidden>"},
        ),
    ],
)
async def test_previews_show_the_current_setting(
    op_id: str, params: dict[str, Any], current: Any
) -> None:
    vim = HostSecurityVim()
    preview = await _PREVIEW_BUILDERS[op_id](_preview_ctx(vim, {"host": "esxi-a", **params}))
    assert preview is not None
    assert preview["host"] == HOST
    assert preview["current"] == current
    assert vim.writes() == []


async def test_the_advanced_settings_preview_shows_no_secret() -> None:
    vim = HostSecurityVim()
    params = {"host": "esxi-a", "settings": [{"key": SECRET_KEY, "value_secret_ref": "esxi/x"}]}
    builder = _PREVIEW_BUILDERS["vmware.composite.host.advanced_settings.set"]
    preview = await builder(_preview_ctx(vim, params))
    assert preview is not None
    assert preview["requested"] == {"settings": {SECRET_KEY: "<hidden>"}}
    assert SECRET_PASSWORD not in json.dumps(preview)


async def test_the_snmp_preview_shows_no_community() -> None:
    vim = HostSecurityVim()
    params = {"host": "esxi-a", "enabled": True, "community_secret_ref": "snmp/example"}
    preview = await _PREVIEW_BUILDERS["vmware.composite.host.snmp.set"](_preview_ctx(vim, params))
    assert preview is not None
    assert preview["requested"]["community_secret_ref"] == "snmp/example"
    assert preview["current"]["read_only_community_count"] == 1
    assert OLD_COMMUNITY not in json.dumps(preview)
