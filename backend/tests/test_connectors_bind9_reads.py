# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Bind9 read op group (G3.4-T2 #588).

Coverage matrix (per Task #588 acceptance criteria):

* ``parse_named_checkconf_zones`` -- one row per top-level zone,
  including zones nested inside ``view`` blocks; ``file`` / ``type``
  extracted from inner directives.
* ``parse_zonefile`` -- one row per rrset member; absolute names,
  integer TTLs, canonical class / type / rdata strings; SOA, NS, MX,
  TXT, A, AAAA, CNAME all round-trip.
* ``parse_dig_answer`` -- accepts both ``+noall +answer +nocomments``
  bare-lines shape and the default ``;; ANSWER SECTION:`` shape;
  empty answer is empty list; multi-record rrsets yield multiple rows.
* ``ensure_path_under_root`` -- accepts absolute paths under the root,
  relative paths resolved under the root, and rejects every traversal
  / control-char / absolute-outside variant.
* ``Bind9Connector.bind9_zone_list`` / ``.bind9_zone_read`` /
  ``.bind9_record_get`` / ``.bind9_config_show`` -- bound-method shim
  invariants against mocked ``_run_command``: correct command,
  correct argument plumbing, structured-error envelopes from the
  dispatcher seam (path rejection, missing zone).
* ``BIND9_OPS`` registration shape -- all four new ops carry
  ``safety_level='safe'``, ``additionalProperties=False`` on the
  parameter schema, non-empty ``llm_instructions``, and bind9-namespace
  op_ids.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import dns.exception
import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.bind9 import BIND9_OPS, Bind9Connector
from meho_backplane.connectors.bind9.ops_config import (
    ConfigPathRejectedError,
    bind9_config_show,
    ensure_path_under_root,
)
from meho_backplane.connectors.bind9.ops_record import (
    bind9_record_get,
    parse_dig_answer,
)
from meho_backplane.connectors.bind9.ops_zone import (
    ZonefileReadError,
    bind9_zone_list,
    bind9_zone_read,
    parse_named_checkconf_zones,
    parse_zonefile,
)
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Env fixture -- mirrors test_connectors_bind9.py (KEYCLOAK / VAULT pins)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires.

    The DB fixture in :mod:`conftest` pins ``DATABASE_URL`` autouse;
    Keycloak + Vault settings are per-file (per the conftest's design
    note). Mirrors the fixture in :mod:`tests.test_connectors_bind9`.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stub target + completed-process helper -- mirrors test_connectors_bind9.py
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]


_TARGET = _StubTarget(
    name="bind9-test",
    host="bind9.test.invalid",
    port=22,
    secret_ref={"username": "root", "password": "irrelevant"},  # NOSONAR
)


def _completed_process(stdout: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's :class:`SSHCompletedProcess`."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


# ---------------------------------------------------------------------------
# parse_named_checkconf_zones
# ---------------------------------------------------------------------------


def test_parse_zones_master_zone_extracts_name_file_type() -> None:
    output = 'zone "evba.lab" {\n\ttype master;\n\tfile "/etc/bind/db.evba.lab";\n};\n'
    assert parse_named_checkconf_zones(output) == [
        {"name": "evba.lab", "file": "/etc/bind/db.evba.lab", "type": "master"}
    ]


def test_parse_zones_supports_multiple_zones_and_class_clause() -> None:
    output = (
        'zone "evba.lab" IN {\n'
        "\ttype master;\n"
        '\tfile "/etc/bind/db.evba.lab";\n'
        "};\n"
        'zone "10.5.50.in-addr.arpa" {\n'
        "\ttype master;\n"
        '\tfile "/etc/bind/db.10.5.50";\n'
        "};\n"
    )
    rows = parse_named_checkconf_zones(output)
    assert {row["name"] for row in rows} == {"evba.lab", "10.5.50.in-addr.arpa"}
    assert all(row["type"] == "master" for row in rows)


def test_parse_zones_walks_through_options_and_view_blocks() -> None:
    """Zones nested in ``view`` blocks are still emitted; outer ``options`` is ignored."""
    output = (
        "options {\n"
        '\tdirectory "/var/cache/bind";\n'
        "};\n"
        'zone "evba.lab" {\n'
        "\ttype master;\n"
        '\tfile "/etc/bind/db.evba.lab";\n'
        "};\n"
        'view "external" {\n'
        '\tzone "ext.example.com" {\n'
        "\t\ttype slave;\n"
        '\t\tfile "/etc/bind/views/ext.example.com";\n'
        "\t\tmasters { 10.0.0.1; };\n"
        "\t};\n"
        "};\n"
    )
    rows = parse_named_checkconf_zones(output)
    names = {row["name"] for row in rows}
    assert "evba.lab" in names
    assert "ext.example.com" in names
    ext_row = next(r for r in rows if r["name"] == "ext.example.com")
    assert ext_row["type"] == "slave"
    assert ext_row["file"] == "/etc/bind/views/ext.example.com"


def test_parse_zones_handles_hint_zone_without_file_gracefully() -> None:
    """``type hint`` zones lacking a ``file`` directive surface ``file=None``."""
    output = 'zone "." {\n\ttype hint;\n};\n'
    rows = parse_named_checkconf_zones(output)
    assert rows == [{"name": ".", "file": None, "type": "hint"}]


def test_parse_zones_empty_input_returns_empty_list() -> None:
    assert parse_named_checkconf_zones("") == []
    assert parse_named_checkconf_zones("options { };\n") == []


# ---------------------------------------------------------------------------
# parse_zonefile
# ---------------------------------------------------------------------------


_SAMPLE_ZONEFILE = """$TTL 3600
@ IN SOA ns1.evba.lab. admin.evba.lab. (
    2026051801 3600 600 604800 86400 )
@   IN NS ns1.evba.lab.
ns1 IN A 10.5.50.1
www IN A 10.5.50.2
mail IN A 10.5.50.3
mail IN AAAA 2001:db8::1
alias IN CNAME ns1.evba.lab.
@   IN MX 10 mail.evba.lab.
@   IN TXT "v=spf1 a -all"
"""


def test_parse_zonefile_emits_one_row_per_rrset_member() -> None:
    rows = parse_zonefile(_SAMPLE_ZONEFILE, origin="evba.lab.")
    types = sorted(row["type"] for row in rows)
    # SOA + NS + MX + TXT at apex; 3 A + 1 AAAA + 1 CNAME for hosts
    assert types.count("A") == 3
    assert types.count("AAAA") == 1
    assert types.count("CNAME") == 1
    assert types.count("MX") == 1
    assert types.count("TXT") == 1
    assert types.count("SOA") == 1
    assert types.count("NS") == 1


def test_parse_zonefile_normalises_names_to_fqdn_with_trailing_dot() -> None:
    rows = parse_zonefile(_SAMPLE_ZONEFILE, origin="evba.lab.")
    www_row = next(r for r in rows if r["type"] == "A" and r["rdata"] == "10.5.50.2")
    assert www_row["name"] == "www.evba.lab."
    assert www_row["ttl"] == 3600
    assert www_row["class"] == "IN"


def test_parse_zonefile_preserves_txt_quoting() -> None:
    rows = parse_zonefile(_SAMPLE_ZONEFILE, origin="evba.lab.")
    txt_row = next(r for r in rows if r["type"] == "TXT")
    assert txt_row["rdata"] == '"v=spf1 a -all"'


def test_parse_zonefile_emits_mx_priority_and_target_in_rdata() -> None:
    rows = parse_zonefile(_SAMPLE_ZONEFILE, origin="evba.lab.")
    mx_row = next(r for r in rows if r["type"] == "MX")
    assert mx_row["rdata"] == "10 mail.evba.lab."


def test_parse_zonefile_raises_on_invalid_syntax() -> None:
    """A malformed zonefile surfaces a :class:`dns.exception.DNSException`."""
    with pytest.raises(dns.exception.DNSException):
        parse_zonefile("this is not a zonefile\nblargh blargh\n", origin="evba.lab.")


# ---------------------------------------------------------------------------
# parse_dig_answer
# ---------------------------------------------------------------------------


def test_parse_dig_answer_handles_noall_answer_bare_lines() -> None:
    """``dig +noall +answer +nocomments`` shape -- bare per-record lines."""
    output = "www.evba.lab.\t\t3600\tIN\tA\t10.5.50.2\n"
    assert parse_dig_answer(output) == [
        {
            "name": "www.evba.lab.",
            "ttl": 3600,
            "class": "IN",
            "type": "A",
            "rdata": "10.5.50.2",
        }
    ]


def test_parse_dig_answer_handles_default_section_marker_shape() -> None:
    """Default-flags ``dig`` output with ``;; ANSWER SECTION:`` marker still parses."""
    output = (
        "; <<>> DiG 9.18.24 <<>> @localhost evba.lab TXT\n"
        ";; ANSWER SECTION:\n"
        'evba.lab.\t\t3600\tIN\tTXT\t"v=spf1 a -all"\n'
        "\n"
        ";; Query time: 0 msec\n"
    )
    rows = parse_dig_answer(output)
    assert len(rows) == 1
    assert rows[0]["type"] == "TXT"
    assert rows[0]["rdata"] == '"v=spf1 a -all"'


def test_parse_dig_answer_returns_empty_on_nxdomain_or_nodata() -> None:
    """No answer rows -> empty list; the handler treats empty as legitimate."""
    # NXDOMAIN -- dig prints only the comment header
    output = "; <<>> DiG 9.18.24 <<>> @localhost missing.evba.lab A\n;; Query time: 0 msec\n"
    assert parse_dig_answer(output) == []


@pytest.mark.parametrize(
    "line, expected_type, expected_rdata",
    [
        ("mail.evba.lab.\t3600\tIN\tAAAA\t2001:db8::1", "AAAA", "2001:db8::1"),
        ("alias.evba.lab.\t3600\tIN\tCNAME\tns1.evba.lab.", "CNAME", "ns1.evba.lab."),
        ("evba.lab.\t3600\tIN\tMX\t10 mail.evba.lab.", "MX", "10 mail.evba.lab."),
    ],
)
def test_parse_dig_answer_handles_each_supported_record_type(
    line: str, expected_type: str, expected_rdata: str
) -> None:
    rows = parse_dig_answer(line + "\n")
    assert len(rows) == 1
    assert rows[0]["type"] == expected_type
    assert rows[0]["rdata"] == expected_rdata


def test_parse_dig_answer_yields_multiple_rows_for_multi_member_rrset() -> None:
    """An MX rrset with two priorities -> two rows."""
    output = (
        "evba.lab.\t3600\tIN\tMX\t10 mail.evba.lab.\nevba.lab.\t3600\tIN\tMX\t20 mail2.evba.lab.\n"
    )
    rows = parse_dig_answer(output)
    assert len(rows) == 2
    assert {row["rdata"] for row in rows} == {
        "10 mail.evba.lab.",
        "20 mail2.evba.lab.",
    }


def test_parse_dig_answer_skips_blank_and_comment_lines() -> None:
    output = "\n; comment\n;; ANSWER SECTION:\nwww.evba.lab.\t3600\tIN\tA\t10.5.50.2\n\n"
    rows = parse_dig_answer(output)
    assert len(rows) == 1
    assert rows[0]["name"] == "www.evba.lab."


# ---------------------------------------------------------------------------
# ensure_path_under_root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested, expected",
    [
        ("named.conf", "/etc/bind/named.conf"),
        ("views/external.conf", "/etc/bind/views/external.conf"),
        ("/etc/bind/named.conf.local", "/etc/bind/named.conf.local"),
        ("./named.conf", "/etc/bind/named.conf"),
        # Path equal to root itself is accepted (operator can list it
        # via a future op; the current handler just cats the file).
        ("/etc/bind", "/etc/bind"),
    ],
)
def test_ensure_path_accepts_paths_under_root(requested: str, expected: str) -> None:
    assert ensure_path_under_root(requested, "/etc/bind") == expected


@pytest.mark.parametrize(
    "requested",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "../etc/shadow",
        "/etc/bindroot/named.conf",  # sibling directory; trailing-slash sentinel test
        "/etc/named/named.conf",  # absolute but outside the root
    ],
)
def test_ensure_path_rejects_paths_outside_root(requested: str) -> None:
    with pytest.raises(ConfigPathRejectedError):
        ensure_path_under_root(requested, "/etc/bind")


@pytest.mark.parametrize(
    "requested",
    [
        "named.conf\nrm -rf /",
        "named.conf\rsmuggled",
        "named.conf\x00",
    ],
)
def test_ensure_path_rejects_control_characters(requested: str) -> None:
    with pytest.raises(ConfigPathRejectedError, match="control character"):
        ensure_path_under_root(requested, "/etc/bind")


def test_ensure_path_rejects_empty_request() -> None:
    with pytest.raises(ConfigPathRejectedError, match="empty"):
        ensure_path_under_root("", "/etc/bind")


def test_ensure_path_rejects_non_absolute_allowed_root() -> None:
    with pytest.raises(ConfigPathRejectedError, match="absolute"):
        ensure_path_under_root("named.conf", "etc/bind")


# ---------------------------------------------------------------------------
# Handler shims -- bind9_zone_list / bind9_zone_read
# ---------------------------------------------------------------------------


_CHECKCONF_OUTPUT = (
    'zone "evba.lab" {\n'
    "\ttype master;\n"
    '\tfile "/etc/bind/db.evba.lab";\n'
    "};\n"
    'zone "10.5.50.in-addr.arpa" {\n'
    "\ttype master;\n"
    '\tfile "/etc/bind/db.10.5.50";\n'
    "};\n"
)


async def test_zone_list_runs_named_checkconf_and_returns_rows() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(return_value=_completed_process(stdout=_CHECKCONF_OUTPUT))
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_zone_list(connector, _TARGET, {})
    assert run_mock.await_args.args[1] == "named-checkconf -p"
    assert result["total"] == 2
    assert {row["name"] for row in result["rows"]} == {
        "evba.lab",
        "10.5.50.in-addr.arpa",
    }


async def test_zone_read_resolves_zonefile_path_via_checkconf_and_cats_file() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=_CHECKCONF_OUTPUT),  # named-checkconf -p
            _completed_process(stdout=_SAMPLE_ZONEFILE),  # cat /etc/bind/db.evba.lab
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_zone_read(connector, _TARGET, {"zone": "evba.lab"})
    # First call: checkconf parse. Second call: cat with quoted path.
    first_cmd = run_mock.await_args_list[0].args[1]
    second_cmd = run_mock.await_args_list[1].args[1]
    assert first_cmd == "named-checkconf -p"
    assert second_cmd == "cat '/etc/bind/db.evba.lab'"
    assert result["zone"] == "evba.lab"
    assert result["file"] == "/etc/bind/db.evba.lab"
    assert result["total"] >= 7  # 1 SOA + 1 NS + 3 A + 1 AAAA + 1 CNAME + 1 MX + 1 TXT
    assert any(row["type"] == "A" and row["rdata"] == "10.5.50.2" for row in result["rows"])


async def test_zone_read_accepts_trailing_dot_on_zone_argument() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        side_effect=[
            _completed_process(stdout=_CHECKCONF_OUTPUT),
            _completed_process(stdout=_SAMPLE_ZONEFILE),
        ]
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_zone_read(connector, _TARGET, {"zone": "evba.lab."})
    assert result["zone"] == "evba.lab."
    assert result["file"] == "/etc/bind/db.evba.lab"


async def test_zone_read_raises_zonefile_read_error_for_missing_zone() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(return_value=_completed_process(stdout=_CHECKCONF_OUTPUT))
    with (
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ZonefileReadError, match="not configured"),
    ):
        await bind9_zone_read(connector, _TARGET, {"zone": "missing.example.com"})


# ---------------------------------------------------------------------------
# Handler shim -- bind9_record_get
# ---------------------------------------------------------------------------


async def test_record_get_runs_dig_with_quoted_fqdn_and_default_type_a() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        return_value=_completed_process(stdout="www.evba.lab.\t3600\tIN\tA\t10.5.50.2\n")
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_record_get(connector, _TARGET, {"fqdn": "www.evba.lab"})
    cmd = run_mock.await_args.args[1]
    assert "dig @localhost" in cmd
    # ``shlex.quote`` only wraps in quotes when the input contains shell
    # metacharacters; a plain ``www.evba.lab`` round-trips unwrapped (and
    # that is the safe shape -- nothing to escape). The contract under
    # test is "the fqdn shows up in the command exactly once and is
    # bracketed only by whitespace or the quote shlex.quote chose".
    assert " www.evba.lab " in cmd
    assert " A " in cmd
    assert "+noall +answer +nocomments" in cmd
    assert result["type"] == "A"
    assert result["total"] == 1
    assert result["rows"][0]["rdata"] == "10.5.50.2"


async def test_record_get_quotes_fqdn_with_shell_metacharacters() -> None:
    """A semicolon-bearing FQDN must be quoted, not interpolated raw."""
    connector = Bind9Connector()
    run_mock = AsyncMock(return_value=_completed_process(stdout=""))
    with patch.object(connector, "_run_command", run_mock):
        await bind9_record_get(connector, _TARGET, {"fqdn": "weird;injection.evba.lab"})
    cmd = run_mock.await_args.args[1]
    # Either shlex shape: ``'weird;injection.evba.lab'`` is what shlex.quote
    # emits for a string containing ``;``. The defence is "the command is
    # safe to feed to sh -c"; the regex below asserts the dangerous bytes
    # are quoted.
    assert "'weird;injection.evba.lab'" in cmd


async def test_record_get_respects_explicit_type_argument() -> None:
    connector = Bind9Connector()
    run_mock = AsyncMock(
        return_value=_completed_process(stdout='evba.lab.\t3600\tIN\tTXT\t"v=spf1 a -all"\n')
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_record_get(connector, _TARGET, {"fqdn": "evba.lab", "type": "TXT"})
    assert result["type"] == "TXT"
    assert result["rows"][0]["type"] == "TXT"


async def test_record_get_returns_empty_rows_for_nxdomain() -> None:
    connector = Bind9Connector()
    # Empty stdout (or stdout with only comments) -> NXDOMAIN-shaped result.
    run_mock = AsyncMock(return_value=_completed_process(stdout=""))
    with patch.object(connector, "_run_command", run_mock):
        result = await bind9_record_get(connector, _TARGET, {"fqdn": "missing.evba.lab"})
    assert result["rows"] == []
    assert result["total"] == 0


async def test_record_get_rejects_unsupported_record_type_with_value_error() -> None:
    connector = Bind9Connector()
    # The schema layer should catch this at dispatch time; the handler's
    # defence-in-depth check runs when an internal caller bypasses the
    # dispatcher.
    with pytest.raises(ValueError, match="unsupported record type"):
        await bind9_record_get(connector, _TARGET, {"fqdn": "evba.lab", "type": "DNAME"})


# ---------------------------------------------------------------------------
# Handler shim -- bind9_config_show
# ---------------------------------------------------------------------------


async def test_config_show_reads_file_under_named_conf_path_directory() -> None:
    connector = Bind9Connector()
    fp = FingerprintResult(
        vendor="isc",
        product="bind9",
        version="9.18.24",
        build="BIND 9.18.24 (Test)",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="ssh: named -v",
        extras={"os": "debian 12", "named_conf_path": "/etc/bind/named.conf"},
    )
    run_mock = AsyncMock(return_value=_completed_process(stdout="options { };"))
    with (
        patch.object(connector, "fingerprint", AsyncMock(return_value=fp)),
        patch.object(connector, "_run_command", run_mock),
    ):
        result = await bind9_config_show(connector, _TARGET, {"path": "named.conf.options"})
    cmd = run_mock.await_args.args[1]
    # ``shlex.quote`` skips quoting when the input has no shell
    # metacharacters; ``/etc/bind/named.conf.options`` is safe as-is.
    # The contract is "the resolved path appears in the command and
    # the dispatch shape is ``cat <path>``" -- the quote shape is an
    # implementation detail of shlex.quote.
    assert cmd in {
        "cat /etc/bind/named.conf.options",
        "cat '/etc/bind/named.conf.options'",
    }
    assert result["file"] == "/etc/bind/named.conf.options"
    assert result["content"] == "options { };"


async def test_config_show_rejects_traversal_with_no_content_in_envelope() -> None:
    """A traversal path raises ``ConfigPathRejectedError`` before any wire IO."""
    connector = Bind9Connector()
    fp = FingerprintResult(
        vendor="isc",
        product="bind9",
        version="9.18.24",
        build="BIND 9.18.24 (Test)",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="ssh: named -v",
        extras={"os": "debian 12", "named_conf_path": "/etc/bind/named.conf"},
    )
    run_mock = AsyncMock()
    with (
        patch.object(connector, "fingerprint", AsyncMock(return_value=fp)),
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ConfigPathRejectedError),
    ):
        await bind9_config_show(connector, _TARGET, {"path": "../../etc/passwd"})
    # Defence-in-depth: rejection happens before any ``cat`` call so the
    # remote shell never sees the path and the envelope cannot accidentally
    # surface file content. The handler raises; the dispatcher's
    # connector_error branch wraps it. No ``_run_command`` invocation
    # touches the wire.
    assert run_mock.await_count == 0


async def test_config_show_via_dispatcher_returns_no_content_on_rejection() -> None:
    """End-to-end through ``Bind9Connector.execute`` -- envelope carries no content."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await Bind9Connector.register_operations()

    connector = Bind9Connector()
    fp = FingerprintResult(
        vendor="isc",
        product="bind9",
        version="9.18.24",
        build="BIND 9.18.24 (Test)",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="ssh: named -v",
        extras={"os": "debian 12", "named_conf_path": "/etc/bind/named.conf"},
    )
    run_mock = AsyncMock(return_value=_completed_process(stdout="WOULD-LEAK"))
    with (
        patch.object(connector, "fingerprint", AsyncMock(return_value=fp)),
        patch.object(connector, "_run_command", run_mock),
    ):
        envelope = await connector.execute(
            _TARGET, "bind9.config.show", {"path": "../../etc/passwd"}
        )
    assert envelope.status == "error"
    # Acceptance criterion: no file content leaked on rejection. Assert
    # the canary string never appears on any envelope field.
    assert envelope.result is None or "WOULD-LEAK" not in str(envelope.result)
    assert envelope.error is not None and "WOULD-LEAK" not in envelope.error
    # ``ensure_path_under_root`` raises before ``_run_command`` fires.
    assert run_mock.await_count == 0


# ---------------------------------------------------------------------------
# BIND9_OPS registration shape -- spec table invariants
# ---------------------------------------------------------------------------


def test_bind9_ops_table_includes_all_t2_read_ops() -> None:
    op_ids = {op.op_id for op in BIND9_OPS}
    assert "bind9.zone.list" in op_ids
    assert "bind9.zone.read" in op_ids
    assert "bind9.record.get" in op_ids
    assert "bind9.config.show" in op_ids
    # T1 canary still there
    assert "bind9.about" in op_ids


@pytest.mark.parametrize(
    "op_id",
    ["bind9.zone.list", "bind9.zone.read", "bind9.record.get", "bind9.config.show"],
)
def test_each_read_op_is_safe_and_no_approval(op_id: str) -> None:
    op = next(o for o in BIND9_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False


@pytest.mark.parametrize(
    "op_id",
    ["bind9.zone.list", "bind9.zone.read", "bind9.record.get", "bind9.config.show"],
)
def test_each_read_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in BIND9_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


@pytest.mark.parametrize(
    "op_id",
    ["bind9.zone.list", "bind9.zone.read", "bind9.record.get", "bind9.config.show"],
)
def test_each_read_op_has_llm_instructions_with_when_to_use(op_id: str) -> None:
    op = next(o for o in BIND9_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    assert op.llm_instructions.get("when_to_use", "").strip() != ""
    assert "output_shape" in op.llm_instructions


def test_zone_read_llm_instructions_mention_future_handle_wrapping() -> None:
    """AC: zone.read llm_instructions must note the result is handle-wrapped when large."""
    op = next(o for o in BIND9_OPS if o.op_id == "bind9.zone.read")
    assert op.llm_instructions is not None
    text = (
        op.llm_instructions.get("when_to_use", "")
        + " "
        + str(op.llm_instructions.get("output_shape", ""))
    )
    # The marker phrase "result handle" / "ResultHandle" / "handle-wrapped"
    # / "JSONFlux" is the load-bearing signal an agent picks up so it
    # knows to expect a handle-shaped response for large zones.
    assert any(token in text for token in ("ResultHandle", "result handle", "handle")), (
        "bind9.zone.read llm_instructions must mention the handle-wrapping behaviour"
    )


def test_record_get_default_type_is_a() -> None:
    op = next(o for o in BIND9_OPS if o.op_id == "bind9.record.get")
    type_schema = op.parameter_schema["properties"]["type"]
    assert type_schema["default"] == "A"
    assert "A" in type_schema["enum"]
    assert set(type_schema["enum"]) == {"A", "AAAA", "CNAME", "MX", "TXT"}
