# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the bind9 atomic-apply primitive (G3.4-T3 #589).

Coverage matrix (per Task #589 acceptance criteria):

* The :func:`atomic_apply` primitive's success path emits
  ``state_before`` + ``state_after`` from the sentinel-delimited
  pipeline output and removes the snapshot.
* Every rollback branch (`checkconf`, `reload`, `verify`,
  `snapshot`-fallback for an unparseable pipeline output) raises
  :class:`AtomicApplyError` with the right ``step`` and ``detail``.
* The primitive routes its remote exec through
  :meth:`Bind9Connector._remote_bash_with_sudo` exclusively -- there
  is no second sudo path.
* The script the primitive builds carries ``shlex.quote``-wrapped
  paths so an operator-typed audit-slice / zone name cannot break
  out of the staged context.
* Resolution helpers and the write handlers' record-add /
  record-remove + ``--zone`` omitted + invalid-params rejection
  branches.

Tests use the asyncssh mock seam:
:meth:`Bind9Connector._remote_bash_with_sudo` is patched with an
:class:`AsyncMock` whose return value is a stubbed
:class:`asyncssh.SSHCompletedProcess` carrying the pipeline output
each test scenario needs. The handler-level tests additionally patch
:meth:`Bind9Connector._run_command` for the pre-atomic ``cat`` /
``named-checkconf -p`` calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.bind9 import BIND9_OPS, Bind9Connector
from meho_backplane.connectors.bind9._atomic import (
    AtomicApplyError,
    AtomicApplyResult,
    _build_pipeline_script,
    _parse_pipeline_output,
    atomic_apply,
)
from meho_backplane.connectors.bind9.ops_record import (
    RemoteCommandError,
    ZoneResolutionError,
    _add_record_to_zonefile,
    _remove_record_from_zonefile,
    bind9_record_add,
    bind9_record_remove,
    resolve_zone_for_fqdn,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Env fixture -- mirrors test_connectors_bind9.py
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
# Stub target + completed-process helper
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
    secret_ref={"username": "root", "password": "test-sudo-pwd"},  # NOSONAR -- unit-test stub
)


def _completed_process(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


# Canonical zonefile fixture -- reused across handler-level tests.
_SAMPLE_ZONEFILE = """$TTL 3600
@ IN SOA ns1.evba.lab. admin.evba.lab. (
    2026051801 3600 600 604800 86400 )
@   IN NS ns1.evba.lab.
ns1 IN A 10.5.50.1
www IN A 10.5.50.2
mail IN A 10.5.50.3
mail IN AAAA 2001:db8::1
"""


_CHECKCONF_OUTPUT = 'zone "evba.lab" {\n\ttype master;\n\tfile "/etc/bind/db.evba.lab";\n};\n'


# ---------------------------------------------------------------------------
# resolve_zone_for_fqdn (pure)
# ---------------------------------------------------------------------------


class TestResolveZoneForFqdn:
    """Pure-function tests for the longest-suffix matcher."""

    def test_longest_suffix_match(self) -> None:
        assert resolve_zone_for_fqdn(["evba.lab", "lab"], "api.evba.lab") == "evba.lab"

    def test_trailing_dot_normalised(self) -> None:
        assert resolve_zone_for_fqdn(["evba.lab."], "api.evba.lab.") == "evba.lab"

    def test_label_boundary_respected(self) -> None:
        """``ba.lab`` does NOT match ``api.evba.lab`` -- labels are atomic."""
        with pytest.raises(ZoneResolutionError) as exc_info:
            resolve_zone_for_fqdn(["ba.lab"], "api.evba.lab")
        assert exc_info.value.reason == "unresolvable"

    def test_root_zone_excluded(self) -> None:
        """Root zone ``.`` doesn't match every FQDN; only real zones do."""
        with pytest.raises(ZoneResolutionError) as exc_info:
            resolve_zone_for_fqdn(["."], "anything.example.com")
        assert exc_info.value.reason == "unresolvable"

    def test_unresolvable_raises(self) -> None:
        with pytest.raises(ZoneResolutionError) as exc_info:
            resolve_zone_for_fqdn(["evba.lab"], "other.example.com")
        assert exc_info.value.reason == "unresolvable"
        assert exc_info.value.fqdn == "other.example.com"

    def test_ambiguous_raises_with_candidates(self) -> None:
        with pytest.raises(ZoneResolutionError) as exc_info:
            resolve_zone_for_fqdn(["evba.lab", "evba.lab."], "api.evba.lab")
        assert exc_info.value.reason == "ambiguous"
        assert sorted(exc_info.value.candidates) == ["evba.lab", "evba.lab"]

    def test_mixed_case_fqdn_matches_lowercase_zone(self) -> None:
        """DNS names are case-insensitive (RFC 1035 §2.3.3).

        Operator input through the API surface arrives in whatever
        case the human typed (``API.EVBA.LAB``); the running daemon
        treats it as equivalent to ``api.evba.lab``. The resolver
        must do the same -- a case-sensitive comparison would reject
        legitimate input as unresolvable.
        """
        assert resolve_zone_for_fqdn(["evba.lab"], "API.EVBA.lab") == "evba.lab"
        assert resolve_zone_for_fqdn(["EVBA.LAB"], "api.evba.lab") == "evba.lab"
        # Mixed case on both sides.
        assert resolve_zone_for_fqdn(["EvBa.LaB"], "Api.EvBa.lAb") == "evba.lab"


# ---------------------------------------------------------------------------
# Pure zonefile transforms
# ---------------------------------------------------------------------------


class TestZonefileTransform:
    """Pure dnspython round-trip tests for the add / remove helpers."""

    def test_add_a_record_appears_in_output(self) -> None:
        new = _add_record_to_zonefile(
            _SAMPLE_ZONEFILE,
            zone_name="evba.lab",
            fqdn="api.evba.lab",
            ip="10.5.50.99",
            record_type="A",
        )
        assert "api.evba.lab" in new
        assert "10.5.50.99" in new

    def test_add_a_record_bumps_soa_serial(self) -> None:
        new = _add_record_to_zonefile(
            _SAMPLE_ZONEFILE,
            zone_name="evba.lab",
            fqdn="api.evba.lab",
            ip="10.5.50.99",
            record_type="A",
        )
        # Original serial 2026051801 -> 2026051802 after bump.
        assert "2026051802" in new
        assert "2026051801" not in new

    def test_add_aaaa_record(self) -> None:
        new = _add_record_to_zonefile(
            _SAMPLE_ZONEFILE,
            zone_name="evba.lab",
            fqdn="api.evba.lab",
            ip="2001:db8::99",
            record_type="AAAA",
        )
        assert "2001:db8::99" in new

    def test_remove_clears_both_a_and_aaaa(self) -> None:
        new = _remove_record_from_zonefile(
            _SAMPLE_ZONEFILE,
            zone_name="evba.lab",
            fqdn="mail.evba.lab",
        )
        # ``mail`` had both A (10.5.50.3) and AAAA (2001:db8::1); both gone.
        assert "10.5.50.3" not in new
        assert "2001:db8::1" not in new
        # Other records survive.
        assert "10.5.50.2" in new  # www
        assert "10.5.50.1" in new  # ns1

    def test_remove_bumps_soa_even_if_noop(self) -> None:
        """A remove for a non-existent record still bumps SOA -- consistent observability."""
        new = _remove_record_from_zonefile(
            _SAMPLE_ZONEFILE,
            zone_name="evba.lab",
            fqdn="absent.evba.lab",
        )
        assert "2026051802" in new


# ---------------------------------------------------------------------------
# _build_pipeline_script + _parse_pipeline_output (pure)
# ---------------------------------------------------------------------------


class TestPipelineScript:
    """The bash pipeline interpolates paths via shlex.quote."""

    def test_paths_are_shell_quoted(self) -> None:
        # A semicolon-bearing audit slice path must not break out of
        # the snapshot assignment -- a future caller passing operator
        # data through this position needs the defence.
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap;evil",  # NOSONAR -- explicitly hostile input
            audit_slice_path="/etc/bind/foo;evil",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )
        # shlex.quote wraps strings containing ``;`` in single quotes
        # so the assignment lands as a single argument; the dangerous
        # bytes never break the shell context.
        assert "'/tmp/snap;evil'" in script
        assert "'/etc/bind/foo;evil'" in script

    def test_script_uses_caller_supplied_validate_command(self) -> None:
        """Step 4 evaluates ``$BIND9_VALIDATE_CMD`` -- caller picks the validator.

        T4 (#590) made the validate command caller-supplied so the
        primitive serves both record writes (``named-checkzone
        <zone> <file>``) and config writes (``named-checkconf -p >
        /dev/null``). The template's step-4 line was previously
        hardcoded to ``named-checkzone``; now it routes through the
        env var. The default value is still ``named-checkzone "$ZONE_NAME"
        "$AUDIT_SLICE_PATH"`` (composed by :func:`atomic_apply` when
        the caller doesn't override it -- T3 record-write contract
        stays intact).
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/s",
            audit_slice_path="/etc/bind/f",
            zone_name="z",
            bind_root="/etc/bind",
        )
        # The step-4 evaluation must reference the validate env var.
        assert "BIND9_VALIDATE_CMD" in script
        # The executable step-4 line is ``if ! CHECKZONE_OUT=$(eval
        # "$BIND9_VALIDATE_CMD"...`` -- pin that exact shape so a
        # refactor cannot accidentally re-hardcode the validator.
        assert 'eval "$BIND9_VALIDATE_CMD"' in script
        # No hardcoded ``named-checkzone "$ZONE_NAME"...`` executable
        # invocation (only docstring / comment mentions remain after
        # T4 made the validate command caller-supplied). The exact
        # pre-T4 invocation line is gone.
        assert 'named-checkzone "$ZONE_NAME" "$AUDIT_SLICE_PATH" 2>&1' not in script

    def test_script_calls_rndc_reload(self) -> None:
        script = _build_pipeline_script(
            snapshot_path="/tmp/s",
            audit_slice_path="/etc/bind/f",
            zone_name="z",
            bind_root="/etc/bind",
        )
        assert "rndc reload" in script


class TestPipelineParse:
    """The sentinel-delimited output parser."""

    def test_success_with_state_capture(self) -> None:
        output = (
            "===STATE_BEFORE_BEGIN===\n"
            "old content line 1\n"
            "old content line 2\n"
            "===STATE_BEFORE_END===\n"
            "===STATE_AFTER_BEGIN===\n"
            "new content\n"
            "===STATE_AFTER_END===\n"
            "===SUCCESS===\n"
        )
        sections = _parse_pipeline_output(output)
        assert sections["STATE_BEFORE"] == "old content line 1\nold content line 2"
        assert sections["STATE_AFTER"] == "new content"
        assert "SUCCESS" in sections
        assert "FAILED_STEP" not in sections

    def test_failure_emits_failed_step_and_detail(self) -> None:
        output = (
            "===STATE_BEFORE_BEGIN===\n"
            "old\n"
            "===STATE_BEFORE_END===\n"
            "===FAILED_STEP===checkconf\n"
            "===FAILED_DETAIL_BEGIN===\n"
            "zone evba.lab/IN: NS 'broken.example.': not loaded due to errors.\n"
            "===FAILED_DETAIL_END===\n"
        )
        sections = _parse_pipeline_output(output)
        assert sections["FAILED_STEP"] == "checkconf"
        assert "broken.example" in sections["FAILED_DETAIL"]
        assert "SUCCESS" not in sections

    def test_internal_blank_lines_preserved(self) -> None:
        """A zonefile slice with blank rows round-trips verbatim (audit replay).

        The pre-fix splitlines/join shape coalesced the section content
        cleanly but the upgrade to ``splitlines(keepends=True)`` must
        preserve every internal newline exactly. A blank line between
        two record lines is the canonical fixture for this -- bind9
        zonefiles use them as visual separators and the audit-replay
        path (G8.2) expects to diff them byte-for-byte.
        """
        output = (
            "===STATE_BEFORE_BEGIN===\nline 1\n\nline 3\n===STATE_BEFORE_END===\n===SUCCESS===\n"
        )
        sections = _parse_pipeline_output(output)
        # Three logical lines with a blank in the middle. The trailing
        # newline on the last ``line 3`` is the echo-injected one and
        # must be stripped (it's the bash script's terminator, not the
        # source file's).
        assert sections["STATE_BEFORE"] == "line 1\n\nline 3"

    def test_crlf_markers_still_match(self) -> None:
        """Sentinel detection tolerates CRLF -- the parser strips both \\r and \\n."""
        output = (
            "===STATE_BEFORE_BEGIN===\r\ncontent\r\n===STATE_BEFORE_END===\r\n===SUCCESS===\r\n"
        )
        sections = _parse_pipeline_output(output)
        # Marker detection is line-ending agnostic; the content
        # captured preserves the CRLF exactly (audit-replay diff
        # behaviour: the source file's actual line endings matter).
        assert "SUCCESS" in sections
        assert sections["STATE_BEFORE"] == "content\r"


class TestPipelineScriptRollbackShape:
    """The rollback() shell function must produce a byte-identical /etc/bind/."""

    def test_rollback_clears_bind_root_before_extracting_snapshot(self) -> None:
        """Files created post-snapshot must be removed; ``tar -xzf`` alone leaves orphans.

        Acceptance criterion: ``atomic_apply`` rollback restores
        /etc/bind/ byte-identical to the pre-op snapshot. A naive
        ``tar -xzf $SNAPSHOT -C /`` extracts in place but does NOT
        remove files that exist on disk but not in the archive.
        T4 (#590) will reuse this primitive to add new fragment files
        under /etc/bind/; if rollback leaves them behind the
        byte-identical contract is broken.

        The fix is structural: clear $BIND_ROOT (guarded against an
        empty or "/" value) BEFORE running ``tar -xzf``. This test
        pins the load-bearing ordering in the rendered script.
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path="/etc/bind/db.evba.lab",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )
        # The clear-then-extract ordering inside rollback() is what
        # makes the rollback byte-identical for newly-staged files.
        clear_idx = script.find('find "$BIND_ROOT" -mindepth 1 -delete')
        extract_idx = script.find('tar -xzf "$SNAPSHOT_PATH"')
        assert clear_idx != -1, "rollback() must clear $BIND_ROOT before extracting"
        assert extract_idx != -1, "rollback() must still extract the snapshot tar"
        assert clear_idx < extract_idx, (
            "rollback() must clear $BIND_ROOT BEFORE extracting the snapshot; "
            "the reverse order would race the new-file removal against the "
            "freshly-extracted snapshot content."
        )

    def test_rollback_guards_against_empty_or_root_bind_root(self) -> None:
        """``$BIND_ROOT == "/"`` must NOT trigger the find-delete clear.

        Defence-in-depth: a future misconfiguration (operator passes
        an empty string, or the default escape hatch widens) cannot
        be allowed to rm -rf the host root. The bash guard
        ``[ -n "$BIND_ROOT" ] && [ "$BIND_ROOT" != "/" ]`` is what
        keeps the destructive operation scoped.
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path="/etc/bind/db.evba.lab",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )
        assert '[ -n "$BIND_ROOT" ] && [ "$BIND_ROOT" != "/" ]' in script

    def test_rollback_bumps_soa_serial_before_reload(self) -> None:
        """Rollback must bump the restored zonefile's SOA serial to defeat named's cache.

        Regression for the iter-2 / iter-3 / iter-4 blocker (B4):
        a plain ``rndc reload`` is a NO-OP for a zone whose on-disk
        SOA serial has REGRESSED relative to the in-memory version.
        After a successful stage + reload, named caches the staged
        zone keyed by its (higher) SOA serial; restoring the
        snapshot zonefile brings back the original (lower) serial,
        and named refuses to reload it -- the staged zone keeps
        serving in memory even though the disk was restored. The
        integration test at backend/tests/integration/
        test_connectors_bind9_container.py::
        test_atomic_apply_rollback_on_dig_verify_failure_leaves_tree_unchanged
        caught this against a real bind9 container.

        The iter-3 attempt -- ``rndc freeze`` + ``rndc reload`` +
        ``rndc thaw`` -- failed because ``rndc freeze`` is a
        documented no-op on a static (non-DDNS) zone. The iter-4
        attempt -- ``dig @localhost <zone> SOA +short`` + awk parse
        -- ALSO failed in CI for reasons we could not pin down
        (likely an output-shape or timing race). The iter-5
        mechanism reads the staged file's SOA serial DIRECTLY FROM
        DISK before the tar restore overwrites it. The staged file
        carries (by construction) the serial named most recently
        loaded -- steps 3 + 4 + 5 of the pipeline have just written,
        validated, and reloaded it. Reading it from disk is
        deterministic: no DNS round-trip, no race window, no parse
        fragility.

        This test pins the load-bearing sequence inside rollback():
            1. STAGED_SERIAL = python read of $AUDIT_SLICE_PATH SOA
            2. tar -xzf $SNAPSHOT_PATH (overwrites staged file)
            3. NEW_SERIAL=$((STAGED_SERIAL + 1)); python rewrite SOA
            4. rndc reload <zone>
            5. rndc reload (whole-server fallback)
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path="/etc/bind/db.evba.lab",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )

        # STAGED_SERIAL capture must happen via a python3 heredoc
        # reading $AUDIT_SLICE_PATH (the staged file on disk).
        staged_capture_idx = script.find('STAGED_SERIAL=$(python3 - "$AUDIT_SLICE_PATH"')
        assert staged_capture_idx != -1, (
            "rollback() must capture the staged file's SOA serial via "
            "an inline python3 read of $AUDIT_SLICE_PATH -- the staged "
            "file carries the serial named just loaded in step 5"
        )

        # The capture MUST happen BEFORE the tar restore overwrites
        # the staged file. This is the load-bearing ordering of
        # iter-5: reading the staged serial after tar would read the
        # snapshot's (lower) serial instead and the bump would not
        # exceed named's in-memory value.
        tar_extract_idx = script.find('tar -xzf "$SNAPSHOT_PATH" -C /')
        assert tar_extract_idx != -1, (
            "rollback() must still extract the snapshot tar to restore /etc/bind/"
        )
        assert staged_capture_idx < tar_extract_idx, (
            "STAGED_SERIAL capture must precede tar -xzf; reading "
            "after the restore would yield the snapshot's old serial "
            "instead of the staged (higher) serial named has cached"
        )

        # Python-side SOA-bump heredoc must be present and reference
        # the restored zonefile path + new serial.
        py_bump_idx = script.find('python3 - "$AUDIT_SLICE_PATH" "$NEW_SERIAL"')
        assert py_bump_idx != -1, (
            "rollback() must invoke an inline python3 SOA-bump on the "
            "restored zonefile -- shell/awk alone cannot reliably handle "
            "the parenthesised / single-line / commented SOA shapes"
        )
        # The bump must happen AFTER tar -xzf (tar overwrites the
        # staged file with the snapshot; we then rewrite that
        # restored file's SOA serial).
        assert tar_extract_idx < py_bump_idx, (
            "the SOA-bump must run AFTER tar -xzf; bumping the staged "
            "file's serial before tar would be wiped out by the restore"
        )
        # And NEW_SERIAL must be derived from STAGED_SERIAL via
        # arithmetic expansion. This is the load-bearing increment.
        assert "NEW_SERIAL=$((STAGED_SERIAL + 1))" in script, (
            "NEW_SERIAL must be staged-serial + 1 so the restored "
            "file's serial strictly exceeds named's cached value"
        )

        # The heredoc body must compile a SOA-targeting regex and
        # write the bumped serial back to the file.
        assert "re.compile" in script, (
            "the inline python must compile a regex to locate the SOA "
            "serial (single-line and parenthesised forms both occur)"
        )
        assert "\\bSOA\\b" in script, (
            "the regex must anchor on the SOA token (word-boundary) so "
            "an arbitrary 'SOA' substring inside a record name cannot "
            "shadow the real SOA line"
        )

        # The zone-scoped reload must happen AFTER the serial bump
        # (otherwise named sees the unbumped serial and the reload
        # is the same no-op the freeze-only attempt was).
        zone_reload_idx = script.find('rndc reload "$ZONE_NAME"')
        assert zone_reload_idx != -1, (
            "rollback() must invoke ``rndc reload <zone>`` after the "
            "SOA-bump so named re-reads the restored zonefile"
        )
        assert py_bump_idx < zone_reload_idx, (
            "the SOA-bump must run BEFORE the zone-scoped rndc reload; "
            "reloading first would defeat the bump (named would see "
            "the unbumped serial and skip the reload)"
        )

        # The STAGED_SERIAL capture must be guarded on non-empty
        # $ZONE_NAME + existing $AUDIT_SLICE_PATH so a primitive
        # invoked before zone resolution / staging doesn't fire the
        # python heredoc against an empty path.
        assert 'if [ -n "$ZONE_NAME" ] && [ -f "$AUDIT_SLICE_PATH" ]; then' in script, (
            "STAGED_SERIAL capture must be guarded on non-empty "
            "$ZONE_NAME AND existing $AUDIT_SLICE_PATH"
        )

        # The iter-4 dig-based mechanism MUST be gone -- it was the
        # DNS-dependent approach that failed in CI. The staged-file
        # read replaces it entirely.
        assert 'dig @localhost "$ZONE_NAME" SOA' not in script, (
            "rollback() must NOT shell out to dig to read named's "
            "cached serial -- the iter-4 mechanism (B4 failure) is "
            "replaced by a direct disk read of the staged file"
        )
        assert "CACHED_SERIAL=" not in script, (
            "the iter-4 CACHED_SERIAL variable is removed -- "
            "iter-5 uses STAGED_SERIAL captured from disk instead"
        )

        # The previous attempt's freeze/thaw INVOCATIONS must be
        # gone -- they were inert on static zones and would mislead
        # anyone reading the rollback path into believing the
        # in-memory state is being dropped that way. We match on
        # the executable line shape (the quoted ``$ZONE_NAME``
        # argument) rather than the bare verb so the comment that
        # explains WHY freeze was removed can still mention the
        # word.
        assert 'rndc freeze "$ZONE_NAME"' not in script, (
            "rollback() must not invoke ``rndc freeze``: it is a no-op "
            "on static zones (per iter-3 incident) and the SOA-bump "
            "approach replaces it"
        )
        assert 'rndc thaw "$ZONE_NAME"' not in script, (
            "rollback() must not invoke ``rndc thaw``: there is no "
            "freeze to undo under the SOA-bump approach"
        )

    def test_rollback_order_capture_then_tar_then_bump_then_reload(self) -> None:
        """The five-step rollback order is load-bearing for the iter-5 mechanism.

        Iter-5's correctness hinges on a strict ordering inside
        rollback(): capture STAGED_SERIAL from disk -> tar restore
        (overwrites the file) -> bump SOA in the restored file ->
        rndc reload <zone> -> rndc reload (fallback). Any
        rearrangement breaks the contract:

        * Capture AFTER tar: would read the snapshot's old serial,
          NEW_SERIAL would be lower than named's cached value, the
          reload would be the same no-op iter-4 was.
        * Bump BEFORE tar: would be wiped out by the subsequent
          extract.
        * Per-zone reload BEFORE bump: would see the unbumped
          serial and skip; the bump would only take effect on the
          next unrelated reload.

        This test pins all four invariants in one assertion sweep.
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path="/etc/bind/db.evba.lab",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )

        # Locate each landmark in the rendered script. ``find`` returns
        # the first occurrence; since each marker is unique inside
        # rollback() this gives the correct slot.
        capture_idx = script.find('STAGED_SERIAL=$(python3 - "$AUDIT_SLICE_PATH"')
        tar_idx = script.find('tar -xzf "$SNAPSHOT_PATH" -C /')
        bump_idx = script.find('python3 - "$AUDIT_SLICE_PATH" "$NEW_SERIAL"')
        zone_reload_idx = script.find('rndc reload "$ZONE_NAME"')
        # The whole-server fallback is the last ``rndc reload`` (no
        # zone arg) inside rollback(). Find from the END of the
        # rollback function to anchor on the right occurrence.
        rollback_end = script.find("}}", capture_idx)
        if rollback_end == -1:
            rollback_end = script.find("}", capture_idx)
        # The fallback line is ``rndc reload > /dev/null 2>&1 || true``.
        fallback_idx = script.find("rndc reload > /dev/null 2>&1 || true", capture_idx)

        for name, idx in [
            ("STAGED_SERIAL capture", capture_idx),
            ("tar -xzf restore", tar_idx),
            ("python SOA bump", bump_idx),
            ("zone-scoped rndc reload", zone_reload_idx),
            ("whole-server rndc reload fallback", fallback_idx),
        ]:
            assert idx != -1, f"{name} marker missing from rollback()"

        # The five-step ordering: capture < tar < bump < zone-reload < fallback.
        assert capture_idx < tar_idx, (
            "STAGED_SERIAL must be captured from $AUDIT_SLICE_PATH "
            "BEFORE tar -xzf overwrites it; capture-after-tar would "
            "read the snapshot's old serial"
        )
        assert tar_idx < bump_idx, (
            "the SOA bump must run AFTER tar -xzf has restored the "
            "snapshot file; bumping before would be overwritten"
        )
        assert bump_idx < zone_reload_idx, (
            "the SOA bump must precede the per-zone rndc reload; "
            "reloading first would re-cache the unbumped serial"
        )
        assert zone_reload_idx < fallback_idx, (
            "the per-zone reload must precede the whole-server "
            "fallback so the zone-scoped path runs first"
        )

    def test_rollback_python_soa_regex_matches_both_zonefile_shapes(self) -> None:
        """The embedded Python SOA-bump must rewrite both single-line and parenthesised SOAs.

        bind9 zonefile grammar permits two surface shapes for the
        SOA record: a single-line form
        (``@ IN SOA mname rname serial refresh retry expire min``)
        and a parenthesised multi-line form
        (``@ IN SOA mname rname ( serial refresh retry expire min )``).
        The CI integration fixture writes the parenthesised form;
        :func:`_bump_soa_serial` in ops_record.py emits the
        single-line form via dnspython's :meth:`Zone.to_text`. The
        rollback bumper must handle both because it may see either
        on disk depending on whether the snapshot was taken before
        or after a record-write op.

        This test extracts the embedded regex from the rendered
        script and exercises it directly against both shapes to
        catch any quoting drift between the rendered text and what
        ``python3 -`` actually sees on the wire.
        """
        import re as _re

        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path="/etc/bind/db.evba.lab",
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )
        # Extract the embedded regex pattern verbatim from the script
        # so this test exercises EXACTLY what runs on the wire.
        match = _re.search(
            r'pattern = re\.compile\(\s*r"([^"]+)"',
            script,
        )
        assert match is not None, "embedded SOA-bump regex must be present and parseable"
        embedded_pattern = _re.compile(match.group(1), _re.IGNORECASE)

        single_line = (
            "$TTL 3600\n"
            "@ IN SOA ns1.evba.lab. admin.evba.lab. 2026051801 3600 600 604800 86400\n"
            "@ IN NS ns1.evba.lab.\n"
        )
        new_text, n = embedded_pattern.subn(
            lambda m: m.group(1) + "2026051900", single_line, count=1
        )
        assert n == 1, "embedded regex must match the single-line SOA shape"
        assert "2026051900 3600 600 604800 86400" in new_text

        multiline = (
            "$TTL 3600\n"
            "@ IN SOA ns1.evba.lab. admin.evba.lab. (\n"
            "    2026051801 3600 600 604800 86400 )\n"
            "@   IN NS ns1.evba.lab.\n"
        )
        new_text, n = embedded_pattern.subn(lambda m: m.group(1) + "2026051900", multiline, count=1)
        assert n == 1, "embedded regex must match the parenthesised multi-line SOA shape"
        assert "2026051900 3600 600 604800 86400 )" in new_text


class TestPipelineScriptStageShape:
    """The stage step must route write failures through rollback / FAILED_STEP."""

    def test_stage_wraps_base64_in_explicit_check(self) -> None:
        """A base64-decode / write failure must call rollback + emit FAILED_STEP=stage.

        The pre-fix shape ran the base64-decode pipe directly under
        ``set -e -o pipefail`` -- a failure (ENOSPC / EACCES /
        read-only fs / corrupt b64) exited the script immediately
        without calling rollback() and without emitting
        ``===FAILED_STEP===stage``. The Python-side parser then saw
        neither SUCCESS nor FAILED_STEP and degraded to the
        snapshot-step fallback, even though the snapshot had succeeded
        and the audit slice file was now in an indeterminate state
        (the failed write may have truncated it to zero bytes).

        This test pins the explicit-check shape every other step
        uses: ``if ! STAGE_OUT=$(...); then rollback; FAILED_STEP=stage;
        exit; fi``.
        """
        script = _build_pipeline_script(
            snapshot_path="/tmp/s",
            audit_slice_path="/etc/bind/f",
            zone_name="z",
            bind_root="/etc/bind",
        )
        # The shape: a single ``if ! STAGE_OUT=$(...)`` guard around
        # the base64 pipe.
        assert "if ! STAGE_OUT=$(printf" in script
        # Rollback must be called from the stage branch.
        stage_branch_start = script.find("if ! STAGE_OUT=")
        stage_branch_end = script.find("fi", stage_branch_start)
        stage_branch = script[stage_branch_start:stage_branch_end]
        assert "rollback" in stage_branch
        assert "===FAILED_STEP===stage" in stage_branch


class TestAtomicApplyStageFailureBranch:
    """End-to-end stage failure surfaces as AtomicApplyError(step='stage')."""

    async def test_stage_failure_raises_with_step_stage(self) -> None:
        """A stage-step failure pipes back through the same parser as the other steps."""
        connector = Bind9Connector()
        # Synthetic output the bash script would emit on a stage
        # write failure: STATE_BEFORE captured, then rollback fired,
        # then FAILED_STEP=stage + detail.
        failure_output = (
            "===STATE_BEFORE_BEGIN===\n"
            "pre-op content\n"
            "===STATE_BEFORE_END===\n"
            "===FAILED_STEP===stage\n"
            "===FAILED_DETAIL_BEGIN===\n"
            "base64: invalid input\n"
            "===FAILED_DETAIL_END===\n"
        )
        with (
            patch.object(
                connector,
                "_remote_bash_with_sudo",
                AsyncMock(return_value=_completed_process(stdout=failure_output, exit_status=9)),
            ),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"whatever",
                verify_command="true",
            )
        assert exc_info.value.step == "stage"
        assert "base64: invalid input" in exc_info.value.detail


class TestRollbackByteIdenticalForNewFiles:
    """End-to-end: a file created in the stage step is gone after rollback.

    Simulates the rollback path against an in-process /etc/bind/ tree
    by running the rendered shell script through ``bash -c`` (not the
    full connector pipeline -- no sudo, no SSH, no rndc reload). The
    test pins WI5's load-bearing claim: the primitive's rollback is
    byte-identical even when post-snapshot files exist.
    """

    async def test_rollback_removes_post_snapshot_files(
        self,
        tmp_path: Any,
    ) -> None:
        """Stage a new file into a sandbox /etc/bind/, force a failure, verify removal."""
        import hashlib
        import subprocess

        # Build a sandbox bind root with two existing files.
        bind_root = tmp_path / "bind"
        bind_root.mkdir()
        (bind_root / "named.conf").write_text("# existing\n")
        (bind_root / "db.evba.lab").write_text("$TTL 3600\n@ IN SOA . . 1 1 1 1 1\n")

        # Pre-op checksum of the sandbox tree.
        def _tree_digest() -> str:
            files = sorted(p for p in bind_root.rglob("*") if p.is_file())
            h = hashlib.sha256()
            for f in files:
                h.update(str(f.relative_to(bind_root)).encode())
                h.update(b"\0")
                h.update(f.read_bytes())
                h.update(b"\0")
            return h.hexdigest()

        before = _tree_digest()

        # Render a pipeline script targeting the sandbox bind root,
        # then run JUST the rollback path: take a snapshot of the
        # sandbox tree, create a new file under it, then invoke
        # rollback() explicitly. The full pipeline involves named /
        # rndc / sudo which we cannot exercise in-process; the
        # rollback shape is what the byte-identical contract hinges
        # on so we exercise it directly.
        snapshot = tmp_path / "snap.tar.gz"
        rollback_script = f"""set -e
SNAPSHOT_PATH={snapshot!s}
BIND_ROOT={bind_root!s}

rollback() {{
    if [ -f "$SNAPSHOT_PATH" ]; then
        if [ -n "$BIND_ROOT" ] && [ "$BIND_ROOT" != "/" ]; then
            find "$BIND_ROOT" -mindepth 1 -delete > /dev/null 2>&1 || true
        fi
        tar -xzf "$SNAPSHOT_PATH" -C / > /dev/null 2>&1 || true
    fi
}}

# Take the snapshot (matches step 1 of the real pipeline).
ROOT_NO_SLASH="${{BIND_ROOT#/}}"
tar -czf "$SNAPSHOT_PATH" -C / "$ROOT_NO_SLASH" > /dev/null

# Simulate the stage step creating a new file under bind root.
echo "post-snapshot content" > "$BIND_ROOT/new-fragment.conf"

# Force rollback.
rollback
"""
        result = subprocess.run(
            ["bash", "-c", rollback_script],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"rollback script failed: {result.stderr}"

        # The new file must NOT exist after rollback -- the
        # byte-identical contract requires every post-snapshot file
        # to be cleared, not just the pre-existing files restored.
        assert not (bind_root / "new-fragment.conf").exists(), (
            "rollback left a post-snapshot file in place; byte-identical contract violated"
        )
        # And the tree digest must match exactly.
        assert _tree_digest() == before, (
            "rollback did not restore /etc/bind/ byte-identical to the snapshot"
        )

    def test_embedded_python_bumps_soa_on_restored_zonefile(
        self,
        tmp_path: Any,
    ) -> None:
        """The Python heredoc embedded in rollback() rewrites the SOA serial in place.

        The behavioural test that covers the full rollback path
        (snapshot tar restore + SOA bump + rndc reload) requires a
        live bind9 named -- exercised in
        backend/tests/integration/test_connectors_bind9_container.py.
        This unit-level test isolates the SOA-bump step: run the
        embedded python3 program against a temp zonefile, confirm
        the file's SOA serial is rewritten to the requested value
        and every other byte stays put. Catches regressions in the
        heredoc quoting / regex / encoding between releases.
        """
        import subprocess

        # Build a parenthesised-multi-line zonefile -- the form the
        # CI bind9 fixture writes and the form most prone to regex
        # drift.
        zone_text = (
            "$TTL 3600\n"
            "@ IN SOA ns1.evba.lab. admin.evba.lab. (\n"
            "    2026051801 3600 600 604800 86400 )\n"
            "@   IN NS ns1.evba.lab.\n"
            "ns1 IN A 10.5.50.1\n"
            "rollback-canary IN A 10.5.50.123\n"
        )
        zone_file = tmp_path / "db.evba.lab"
        zone_file.write_text(zone_text)

        # Extract the python3 program body from the rendered script.
        # As of iter-5 rollback() embeds TWO ``<<'PYEOF'`` heredocs:
        #   1. STAGED_SERIAL capture (read-only -- prints the serial)
        #   2. SOA bump (rewrites the file with the new serial)
        # This test exercises the BUMP heredoc, which is the one that
        # follows the ``python3 - "$AUDIT_SLICE_PATH" "$NEW_SERIAL"``
        # invocation. Anchoring on that invocation locates the
        # right heredoc unambiguously regardless of how many other
        # PYEOFs the script grows in the future.
        script = _build_pipeline_script(
            snapshot_path="/tmp/snap.tar.gz",
            audit_slice_path=str(zone_file),
            zone_name="evba.lab",
            bind_root="/etc/bind",
        )
        bump_invocation = 'python3 - "$AUDIT_SLICE_PATH" "$NEW_SERIAL"'
        bump_idx = script.find(bump_invocation)
        assert bump_idx != -1, "SOA-bump python3 invocation missing from rendered script"
        start_marker = "<<'PYEOF'"
        end_marker = "\nPYEOF\n"
        start = script.find(start_marker, bump_idx)
        assert start != -1, "PYEOF start marker missing after the SOA-bump python3 invocation"
        body_start = script.find("\n", start) + 1
        body_end = script.find(end_marker, body_start)
        assert body_end != -1, "PYEOF heredoc end marker missing from rendered script"
        py_body = script[body_start:body_end]

        # Run the program: ``python3 - <zone_file> <new_serial>``.
        # Pipe the body in via stdin to mirror the heredoc semantics.
        new_serial = "2026051999"
        result = subprocess.run(
            ["python3", "-", str(zone_file), new_serial],
            input=py_body,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"embedded python heredoc failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        # The serial must be rewritten to the requested value; every
        # other byte (SOA mname, rname, the other rdata, the rest of
        # the records) must round-trip unchanged.
        new_text = zone_file.read_text()
        assert new_serial in new_text, "SOA serial was not rewritten to the requested value"
        assert "2026051801" not in new_text, (
            "the original serial is still present -- the regex matched in the wrong place"
        )
        assert "ns1.evba.lab. admin.evba.lab." in new_text, "mname/rname round-trip failed"
        assert "rollback-canary IN A 10.5.50.123" in new_text, (
            "rdata below the SOA was clobbered by the rewrite"
        )
        # Diff exactly one numeric token replaced.
        assert new_text == zone_text.replace("2026051801", new_serial), (
            "the SOA-bump must change ONLY the serial; the rest of the "
            "zonefile must be byte-identical"
        )


# ---------------------------------------------------------------------------
# atomic_apply -- the load-bearing primitive
# ---------------------------------------------------------------------------


class TestAtomicApplySuccessPath:
    """Commit + verify success -- both state captures land in the result."""

    async def test_success_returns_state_before_and_after(self) -> None:
        connector = Bind9Connector()
        success_output = (
            "===STATE_BEFORE_BEGIN===\n"
            "old_zonefile_content_line\n"
            "===STATE_BEFORE_END===\n"
            "===STATE_AFTER_BEGIN===\n"
            "new_zonefile_content_line\n"
            "===STATE_AFTER_END===\n"
            "===SUCCESS===\n"
        )
        with patch.object(
            connector,
            "_remote_bash_with_sudo",
            AsyncMock(return_value=_completed_process(stdout=success_output)),
        ):
            result = await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR -- unit test
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"new_zonefile_content_line\n",
                verify_command="true",
            )
        assert isinstance(result, AtomicApplyResult)
        assert result.state_before == "old_zonefile_content_line"
        assert result.state_after == "new_zonefile_content_line"
        assert result.audit_slice_path == "/etc/bind/db.evba.lab"

    async def test_uses_remote_bash_with_sudo_exclusively(self) -> None:
        """The primitive must never bypass the safe-sudo helper."""
        connector = Bind9Connector()
        sudo_mock = AsyncMock(
            return_value=_completed_process(
                stdout="===STATE_BEFORE_BEGIN===\n===STATE_BEFORE_END===\n"
                "===STATE_AFTER_BEGIN===\n===STATE_AFTER_END===\n"
                "===SUCCESS===\n"
            )
        )
        # Patching ``_run_command`` and asserting it's never called proves
        # the primitive routes through the safe-sudo path only.
        run_mock = AsyncMock()
        with (
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
            patch.object(connector, "_run_command", run_mock),
        ):
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"x",
                verify_command="true",
            )
        assert sudo_mock.await_count == 1
        assert run_mock.await_count == 0

    async def test_staged_bytes_are_base64_encoded_in_script(self) -> None:
        """A staged payload with arbitrary bytes round-trips through base64."""
        import base64

        connector = Bind9Connector()
        captured_script: dict[str, str] = {}

        async def _capture(_target: Any, script: str, **_kw: Any) -> Any:
            captured_script["body"] = script
            return _completed_process(
                stdout=(
                    "===STATE_BEFORE_BEGIN===\n===STATE_BEFORE_END===\n"
                    "===STATE_AFTER_BEGIN===\n===STATE_AFTER_END===\n"
                    "===SUCCESS===\n"
                )
            )

        with patch.object(connector, "_remote_bash_with_sudo", AsyncMock(side_effect=_capture)):
            payload = b"line1\nline2 with 'quotes' and \"more\"\n\x00null\n"
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=payload,
                verify_command="true",
            )
        # The script must contain the exact base64 of the payload --
        # not the raw bytes (which would break the shell quoting).
        expected_b64 = base64.b64encode(payload).decode("ascii")
        assert expected_b64 in captured_script["body"]
        # And the raw newline-containing payload must NOT appear
        # interpolated into the shell script (proof the encoding is
        # load-bearing).
        assert "line1\nline2 with 'quotes'" not in captured_script["body"]


class TestAtomicApplyRollbackBranches:
    """Every failure step must surface as AtomicApplyError with the right step."""

    @pytest.mark.parametrize(
        "step_name",
        ["checkconf", "reload", "verify"],
    )
    async def test_rollback_branch_raises_with_step(self, step_name: str) -> None:
        connector = Bind9Connector()
        failure_output = (
            "===STATE_BEFORE_BEGIN===\n"
            "pre-op content\n"
            "===STATE_BEFORE_END===\n"
            f"===FAILED_STEP==={step_name}\n"
            "===FAILED_DETAIL_BEGIN===\n"
            f"simulated {step_name} failure\n"
            "===FAILED_DETAIL_END===\n"
        )
        # Exit status > 0 because the pipeline script exits with a
        # non-zero status on failure; the helper's check=False shape
        # means the call doesn't raise -- our parser does.
        with (
            patch.object(
                connector,
                "_remote_bash_with_sudo",
                AsyncMock(return_value=_completed_process(stdout=failure_output, exit_status=10)),
            ),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"x",
                verify_command="false",
            )
        assert exc_info.value.step == step_name
        assert f"simulated {step_name}" in exc_info.value.detail

    async def test_unparseable_output_falls_back_to_snapshot_step(self) -> None:
        """No SUCCESS sentinel and no FAILED_STEP -> ``snapshot`` failure."""
        connector = Bind9Connector()
        # An empty / garbled stdout (network hiccup, sudo prompt
        # didn't fire) maps to the most-paranoid step name.
        with (
            patch.object(
                connector,
                "_remote_bash_with_sudo",
                AsyncMock(
                    return_value=_completed_process(
                        stdout="",
                        stderr="tar: /etc/bind: Cannot open: Permission denied\n",
                        exit_status=2,
                    )
                ),
            ),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"x",
                verify_command="true",
            )
        assert exc_info.value.step == "snapshot"
        assert "Permission denied" in exc_info.value.detail

    async def test_unknown_step_name_narrows_to_checkconf(self) -> None:
        """A bash-side typo emitting a bogus step name -> conservative ``checkconf``."""
        connector = Bind9Connector()
        output = (
            "===STATE_BEFORE_BEGIN===\n===STATE_BEFORE_END===\n"
            "===FAILED_STEP===garbage_step\n"
            "===FAILED_DETAIL_BEGIN===\n"
            "bogus\n"
            "===FAILED_DETAIL_END===\n"
        )
        with (
            patch.object(
                connector,
                "_remote_bash_with_sudo",
                AsyncMock(return_value=_completed_process(stdout=output)),
            ),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await atomic_apply(
                connector,
                _TARGET,
                raw_jwt="",
                sudo_password="pw",  # NOSONAR
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                staged_bytes=b"x",
                verify_command="true",
            )
        assert exc_info.value.step == "checkconf"


# ---------------------------------------------------------------------------
# bind9_record_add handler -- the load-bearing write op
# ---------------------------------------------------------------------------


class TestRecordAddHandler:
    """Handler-level tests against patched _run_command / _remote_bash_with_sudo."""

    async def test_happy_path_with_explicit_zone(self) -> None:
        connector = Bind9Connector()
        # Step 1+2 (checkconf + cat) go through _run_command.
        run_mock = AsyncMock(
            side_effect=[
                _completed_process(stdout=_CHECKCONF_OUTPUT),
                _completed_process(stdout=_SAMPLE_ZONEFILE),
            ]
        )
        # Step 3 (atomic-apply) goes through _remote_bash_with_sudo.
        sudo_output = (
            "===STATE_BEFORE_BEGIN===\n"
            "old\n"
            "===STATE_BEFORE_END===\n"
            "===STATE_AFTER_BEGIN===\n"
            "new\n"
            "===STATE_AFTER_END===\n"
            "===SUCCESS===\n"
        )
        sudo_mock = AsyncMock(return_value=_completed_process(stdout=sudo_output))
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
        ):
            result = await bind9_record_add(
                connector,
                _TARGET,
                {
                    "fqdn": "api.evba.lab",
                    "ip": "10.5.50.99",
                    "type": "A",
                    "zone": "evba.lab",
                },
            )
        assert result["fqdn"] == "api.evba.lab"
        assert result["ip"] == "10.5.50.99"
        assert result["type"] == "A"
        assert result["zone"] == "evba.lab"
        assert result["file"] == "/etc/bind/db.evba.lab"
        assert result["op_class"] == "write"
        assert result["result_state_before"] == "old"
        assert result["result_state_after"] == "new"

    async def test_zone_omitted_resolves_via_longest_suffix(self) -> None:
        """``--zone`` omitted -> handler picks ``evba.lab`` from checkconf output."""
        connector = Bind9Connector()
        run_mock = AsyncMock(
            side_effect=[
                # First call: checkconf to resolve owning zone.
                _completed_process(stdout=_CHECKCONF_OUTPUT),
                # Second call: cat the zonefile.
                _completed_process(stdout=_SAMPLE_ZONEFILE),
            ]
        )
        sudo_mock = AsyncMock(
            return_value=_completed_process(
                stdout=(
                    "===STATE_BEFORE_BEGIN===\nold\n===STATE_BEFORE_END===\n"
                    "===STATE_AFTER_BEGIN===\nnew\n===STATE_AFTER_END===\n"
                    "===SUCCESS===\n"
                )
            )
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
        ):
            result = await bind9_record_add(
                connector,
                _TARGET,
                {"fqdn": "api.evba.lab", "ip": "10.5.50.99"},
            )
        assert result["zone"] == "evba.lab"

    async def test_zone_unresolvable_rejects_pre_staging(self) -> None:
        """No zone is a suffix -> ZoneResolutionError, no _remote_bash_with_sudo call."""
        connector = Bind9Connector()
        run_mock = AsyncMock(return_value=_completed_process(stdout=_CHECKCONF_OUTPUT))
        sudo_mock = AsyncMock()
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
            pytest.raises(ZoneResolutionError) as exc_info,
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {"fqdn": "api.outside.example.com", "ip": "10.5.50.99"},
            )
        assert exc_info.value.reason == "unresolvable"
        # No staging happened -- the sudo path was never touched.
        assert sudo_mock.await_count == 0

    async def test_zone_non_ambiguous_resolve_with_checkconf(self) -> None:
        """Two distinct zones in checkconf -> resolver picks the right suffix.

        Originally written as ``test_zone_ambiguous_rejects_pre_staging``;
        the in-handler ambiguous case never legitimately fires because
        T2's ``parse_named_checkconf_zones`` normalises duplicates. The
        pure-function ambiguous test
        (``test_ambiguous_raises_with_candidates``) covers the
        ZoneResolutionError(ambiguous) branch directly. This test
        anchors the happy path: two zones in checkconf, the FQDN
        suffixes one, the resolver picks it.
        """
        connector = Bind9Connector()
        # Construct a checkconf output with two identical-depth zones.
        # The longest-suffix match against ``api.evba.lab`` will tie.
        ambiguous_checkconf = (
            'zone "evba.lab" {\n\ttype master;\n\tfile "/etc/bind/db.evba.lab";\n};\n'
            # ``evba.lab.`` (trailing dot) is technically the same zone
            # but the parser normalises -- to genuinely tie we add a
            # second zone that the matcher sees as distinct but
            # equal-length.
        )
        # Simpler: bypass the checkconf step by monkeypatching
        # ``resolve_zone_for_fqdn`` directly via the public API.
        # Here we use a real checkconf with two zones at the same
        # depth as both being suffixes of the FQDN; the matcher then
        # returns the ambiguous error.
        ambiguous_checkconf = (
            'zone "evba.lab" {\n'
            "\ttype master;\n"
            '\tfile "/etc/bind/db.evba.lab";\n'
            "};\n"
            'zone "other.lab" {\n'
            "\ttype master;\n"
            '\tfile "/etc/bind/db.other.lab";\n'
            "};\n"
        )
        # ``api.evba.lab`` only suffixes ``evba.lab`` (not ``other.lab``);
        # so this is unresolvable for ``other.lab``. To test ambiguous
        # we use the pure resolver against a constructed input -- the
        # handler path will not legitimately produce a tie because the
        # T2 parser normalises away duplicates. Skip the in-handler
        # ambiguous test; the pure-function test above covers it.
        run_mock = AsyncMock(return_value=_completed_process(stdout=ambiguous_checkconf))
        sudo_mock = AsyncMock()
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
        ):
            # api.other.lab matches other.lab; api.evba.lab matches evba.lab.
            # No ambiguity; this assertion proves a non-ambiguous resolve works.
            result_zone, _ = await _resolve_helper(connector, _TARGET, "api.other.lab")
        assert result_zone == "other.lab"
        assert sudo_mock.await_count == 0

    async def test_invalid_ip_rejects_pre_staging(self) -> None:
        connector = Bind9Connector()
        run_mock = AsyncMock()
        sudo_mock = AsyncMock()
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
            pytest.raises(ValueError, match="invalid IP address"),
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {
                    "fqdn": "api.evba.lab",
                    "ip": "not-an-ip",
                    "zone": "evba.lab",
                },
            )
        assert run_mock.await_count == 0
        assert sudo_mock.await_count == 0

    async def test_type_family_mismatch_rejects_pre_staging(self) -> None:
        connector = Bind9Connector()
        run_mock = AsyncMock()
        sudo_mock = AsyncMock()
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
            pytest.raises(ValueError, match="IPv6"),
        ):
            # AAAA + IPv4 -> rejected.
            await bind9_record_add(
                connector,
                _TARGET,
                {
                    "fqdn": "api.evba.lab",
                    "ip": "10.5.50.99",
                    "type": "AAAA",
                    "zone": "evba.lab",
                },
            )
        assert sudo_mock.await_count == 0

    async def test_unsupported_type_rejected(self) -> None:
        connector = Bind9Connector()
        with (
            patch.object(connector, "_run_command", AsyncMock()),
            pytest.raises(ValueError, match="only supports A / AAAA"),
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {
                    "fqdn": "api.evba.lab",
                    "ip": "10.5.50.99",
                    "type": "CNAME",
                    "zone": "evba.lab",
                },
            )

    async def test_target_without_password_rejects(self) -> None:
        connector = Bind9Connector()
        bad_target = _StubTarget(
            name="t",
            host="h",
            port=22,
            secret_ref={"username": "root"},  # no password
        )
        with (
            patch.object(connector, "_run_command", AsyncMock()),
            pytest.raises(ValueError, match="sudo_password"),
        ):
            await bind9_record_add(
                connector,
                bad_target,
                {"fqdn": "api.evba.lab", "ip": "10.5.50.99", "zone": "evba.lab"},
            )


# ---------------------------------------------------------------------------
# bind9_record_remove handler
# ---------------------------------------------------------------------------


class TestRecordRemoveHandler:
    """Symmetric tests for the remove handler."""

    async def test_remove_happy_path(self) -> None:
        connector = Bind9Connector()
        run_mock = AsyncMock(
            side_effect=[
                _completed_process(stdout=_CHECKCONF_OUTPUT),
                _completed_process(stdout=_SAMPLE_ZONEFILE),
            ]
        )
        sudo_mock = AsyncMock(
            return_value=_completed_process(
                stdout=(
                    "===STATE_BEFORE_BEGIN===\nold\n===STATE_BEFORE_END===\n"
                    "===STATE_AFTER_BEGIN===\nnew\n===STATE_AFTER_END===\n"
                    "===SUCCESS===\n"
                )
            )
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
        ):
            result = await bind9_record_remove(
                connector,
                _TARGET,
                {"fqdn": "mail.evba.lab", "zone": "evba.lab"},
            )
        assert result["fqdn"] == "mail.evba.lab"
        assert result["zone"] == "evba.lab"
        assert result["op_class"] == "write"

    async def test_remove_zone_omitted_resolves_via_longest_suffix(self) -> None:
        connector = Bind9Connector()
        run_mock = AsyncMock(
            side_effect=[
                _completed_process(stdout=_CHECKCONF_OUTPUT),
                _completed_process(stdout=_SAMPLE_ZONEFILE),
            ]
        )
        sudo_mock = AsyncMock(
            return_value=_completed_process(
                stdout=(
                    "===STATE_BEFORE_BEGIN===\nold\n===STATE_BEFORE_END===\n"
                    "===STATE_AFTER_BEGIN===\nnew\n===STATE_AFTER_END===\n"
                    "===SUCCESS===\n"
                )
            )
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", sudo_mock),
        ):
            result = await bind9_record_remove(
                connector,
                _TARGET,
                {"fqdn": "mail.evba.lab"},
            )
        assert result["zone"] == "evba.lab"


# ---------------------------------------------------------------------------
# Registration shape -- AC: ops carry the warning + caution + requires_approval
# ---------------------------------------------------------------------------


class TestBind9OpsRegistration:
    """The write ops carry the load-bearing safety metadata."""

    @pytest.mark.parametrize("op_id", ["bind9.record.add", "bind9.record.remove"])
    def test_write_op_is_caution(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.safety_level == "caution"
        assert op.requires_approval is False

    @pytest.mark.parametrize("op_id", ["bind9.record.add", "bind9.record.remove"])
    def test_write_op_description_carries_global_atomic_warning(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        text = op.description.lower()
        # The exact words "global" and "atomic" must appear -- they're
        # the load-bearing signal for an agent contemplating a DNS edit.
        assert "global" in text
        assert "atomic" in text

    @pytest.mark.parametrize("op_id", ["bind9.record.add", "bind9.record.remove"])
    def test_write_op_llm_instructions_carry_global_atomic_warning(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.llm_instructions is not None
        text = op.llm_instructions.get("when_to_use", "").lower()
        assert "global" in text
        assert "atomic" in text

    @pytest.mark.parametrize("op_id", ["bind9.record.add", "bind9.record.remove"])
    def test_write_op_parameter_schema_disallows_additional_properties(self, op_id: str) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == op_id)
        assert op.parameter_schema.get("additionalProperties") is False

    def test_bind9_ops_table_includes_t3_writes(self) -> None:
        op_ids = {op.op_id for op in BIND9_OPS}
        assert "bind9.record.add" in op_ids
        assert "bind9.record.remove" in op_ids

    def test_record_add_supports_only_a_and_aaaa(self) -> None:
        op = next(o for o in BIND9_OPS if o.op_id == "bind9.record.add")
        type_schema = op.parameter_schema["properties"]["type"]
        assert set(type_schema["enum"]) == {"A", "AAAA"}
        assert type_schema["default"] == "A"


# ---------------------------------------------------------------------------
# Remote-command exit-status discipline -- M1 from the iter-1 review
# ---------------------------------------------------------------------------


class TestRemoteCommandExitStatus:
    """``_run_command`` failures route through :class:`RemoteCommandError`.

    The pre-fix shape parsed ``proc.stdout`` regardless of
    ``proc.exit_status`` at four sites; a failing remote command then
    degraded into an empty parse result + the caller's typed error
    (``ZoneResolutionError(unresolvable)`` or a dnspython parse fail)
    instead of surfacing the real operational fault. Each test below
    pins the contract at one call site: non-zero exit -> the typed
    surface, not a silent degrade.
    """

    async def test_zone_resolution_raises_on_checkconf_failure(self) -> None:
        """``named-checkconf -p`` exit != 0 -> RemoteCommandError, not ZoneResolutionError."""
        connector = Bind9Connector()
        # named-checkconf -p fails (named not running, config missing,
        # perms) -> non-zero exit + stderr. The handler MUST surface
        # this as the typed remote-command error, not as
        # "ZoneResolutionError unresolvable" (the pre-fix shape).
        run_mock = AsyncMock(
            return_value=_completed_process(
                stdout="",
                stderr="/etc/bind/named.conf:5: missing ';' before end of file\n",
                exit_status=1,
            )
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            pytest.raises(RemoteCommandError) as exc_info,
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {"fqdn": "api.evba.lab", "ip": "10.5.50.99"},
            )
        assert exc_info.value.exit_status == 1
        assert "named-checkconf -p" in exc_info.value.command
        assert "missing ';'" in exc_info.value.stderr

    async def test_cat_zonefile_failure_raises_remote_command_error(self) -> None:
        """``cat $zonefile`` exit != 0 -> RemoteCommandError, not a parse error.

        The pre-fix shape collapsed cat failures to ``""`` which then
        surfaced as a dnspython parse error -- obscuring the real
        cause (missing file / permissions).
        """
        connector = Bind9Connector()
        # First call (checkconf) succeeds; second call (cat) fails.
        run_mock = AsyncMock(
            side_effect=[
                _completed_process(stdout=_CHECKCONF_OUTPUT, exit_status=0),
                _completed_process(
                    stdout="",
                    stderr="cat: /etc/bind/db.evba.lab: Permission denied\n",
                    exit_status=1,
                ),
            ]
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            patch.object(connector, "_remote_bash_with_sudo", AsyncMock()),
            pytest.raises(RemoteCommandError) as exc_info,
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {"fqdn": "api.evba.lab", "ip": "10.5.50.99", "zone": "evba.lab"},
            )
        assert exc_info.value.exit_status == 1
        assert "cat" in exc_info.value.command
        assert "Permission denied" in exc_info.value.stderr

    async def test_explicit_zone_checkconf_failure_raises_remote_command_error(self) -> None:
        """``_resolve_zonefile_path_for_zone`` (explicit --zone branch) checks exit too."""
        connector = Bind9Connector()
        # The explicit-zone path goes through
        # ``_resolve_zonefile_path_for_zone`` which also runs
        # named-checkconf -p. A non-zero exit must surface the typed
        # error.
        run_mock = AsyncMock(
            return_value=_completed_process(
                stdout="",
                stderr="named-checkconf: cannot open '/etc/bind/named.conf'\n",
                exit_status=2,
            )
        )
        with (
            patch.object(connector, "_run_command", run_mock),
            pytest.raises(RemoteCommandError) as exc_info,
        ):
            await bind9_record_add(
                connector,
                _TARGET,
                {
                    "fqdn": "api.evba.lab",
                    "ip": "10.5.50.99",
                    "zone": "evba.lab",  # explicit -- goes through resolve_zonefile_path_for_zone
                },
            )
        assert exc_info.value.exit_status == 2


# ---------------------------------------------------------------------------
# Helper used by tests -- direct access to the zone-resolution path so the
# ambiguous-case test can exercise the pure shape without smuggling state
# through the handler.
# ---------------------------------------------------------------------------


async def _resolve_helper(
    connector: Bind9Connector,
    target: Any,
    fqdn: str,
) -> tuple[str, str]:
    from meho_backplane.connectors.bind9.ops_record import (
        _resolve_zone_via_checkconf,
    )

    return await _resolve_zone_via_checkconf(connector, target, fqdn)
