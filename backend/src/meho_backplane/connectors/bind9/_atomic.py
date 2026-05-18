# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Atomic-apply primitive for bind9 zonefile / config-fragment edits.

G3.4-T3 (#589) of Initiative #367. The **load-bearing** discipline this
module encodes -- DNS is global; a half-applied zone change wedges the
whole datacenter. The consumer's ``scripts/bind9-dns.sh`` stages every
mutation in a temp dir, validates the proposed file, then commits +
reloads + verifies, rolling back on any failure. The connector ports
this verbatim as one reusable async primitive so T3 record writes and
T4 config writes inherit the same proof.

Sequence
--------

The :func:`atomic_apply` helper runs a fixed seven-step pipeline
inside **one** sudo-bash invocation (one ``_remote_bash_with_sudo``
call). Bundling into a single remote bash script means the snapshot
+ commit + reload always share an interpreter on the target, so a
local connection failure between steps cannot leave a half-applied
state -- either the whole script completed, or nothing about
``/etc/bind/`` was touched. The seven steps:

1. **Snapshot.** Tar ``/etc/bind/`` to a temp path (``/tmp/<token>.tar.gz``).
   The snapshot includes every file under the bind config root, not
   just the zonefile being edited -- callbacks may transitively edit
   ``named.conf.local`` or fragment files, and the rollback contract
   is that the pre-op tree is restored byte-identical.
2. **Capture state_before.** ``cat`` the audit-slice path (the
   zonefile or fragment the op semantically edits) so the audit row
   can carry pre-op content for replay (G8.2). The slice is informational
   only -- the rollback always uses the full tar snapshot.
3. **Stage.** Write the caller's proposed file content (passed as
   bytes) to the audit-slice path.
4. **Validate.** Run ``named-checkzone <zone> <file>`` against the
   staged file. Non-zero exit -> failure: restore from snapshot,
   ``rndc reload``, return ``checkconf`` step.

   Why ``named-checkzone`` rather than ``named-checkconf -p``: the
   latter parses the *active* config tree (which already references
   the new file, since step 3 wrote in place). A syntactically broken
   zonefile would be caught either way, but ``named-checkzone`` runs
   the per-zone integrity check at the granularity the operator's
   change actually has -- a record-write only touches one zonefile,
   so per-zone is the right blast-radius for the validation gate.
5. **Reload.** ``rndc reload`` -- bind9 picks up the staged file.
   Non-zero exit -> restore from snapshot, ``rndc reload`` again to
   come back to the known-good config, return ``reload`` step.

   Note: ``rndc reload`` is a "tell named to reload" request -- named
   reads the zonefile asynchronously. The verify step below catches
   the propagation by polling ``dig``.
6. **Verify.** Run the caller-supplied verify command (e.g.
   ``dig @localhost <fqdn>`` returns the new IP). The command must
   exit 0 to signal success; any non-zero verifies as failure.
7. **On verify failure:** restore from snapshot, ``rndc reload``,
   return ``verify`` step. On success, delete the snapshot tar and
   return success.

Step 6's rollback is the most paranoid case: named has already
loaded the change; the rollback restores the prior tree and reloads
named back to that. The dig-verify failure mode this guards against
is "the staged file parses but doesn't actually resolve <fqdn>
post-reload" -- a logic bug in the caller's stager, or a view's RPZ
swallowing the new record. Either way, the operator-visible state
post-rollback is identical to pre-op.

State capture
-------------

The primitive returns ``state_before`` and ``state_after`` for the
**audit-slice path** (typically the affected zonefile). The captured
bytes are decoded as UTF-8 with ``errors="replace"`` -- bind9 zonefiles
are 7-bit ASCII in practice (the grammar forbids non-ASCII in record
names; UTF-8 IDN labels are punycoded), so replacement should never
fire on real input but the contract keeps the result JSON-serialisable
regardless of upstream encoding drift.

Sudo discipline
---------------

Every step of the pipeline runs under sudo through T1's
:meth:`~meho_backplane.connectors.bind9.connector.Bind9Connector._remote_bash_with_sudo`.
The primitive **never** builds a sudo command line of its own --
the whole pipeline is one bash script body fed to the safe-sudo
helper. The password is streamed via stdin per T1's contract; the
script body never contains the password.

Why one bash script vs N round-trips
------------------------------------

Originally considered: one ``_remote_bash_with_sudo`` call per
step (snapshot, cat, stage, checkzone, reload, verify, rollback).
Rejected because:

* Each call re-prompts sudo (TTY-less sudo timestamp cache is per
  session, not per connection); a 7-call pipeline pays the sudo cost
  7x.
* A local network blip between, e.g., stage and validate would
  leave the staged file in place with no rollback. Bundling into one
  remote bash keeps the pipeline atomic from the target's POV.
* Each call writes one structured-log row; the burst hurts
  observability rather than helping it.

The trade-off: the bash body grows to ~80 lines (snapshot tar,
validate, conditional rollback). The body is generated entirely by
this module (no caller-supplied substring lands in the shell), so
the safe-sudo helper's invariants extend uninjured.

References
----------

* Parent Task: G3.4-T3 (#589).
* Parent Initiative: G3.4 (#367) WI5 (atomic-apply discipline).
* Safe-sudo primitive reused: T1 (#587)
  ``_remote_bash_with_sudo``.
* Audit replay consumer of ``state_before`` / ``state_after``: G8.2.
* ISC bind9 9.18 ``named-checkzone`` -- per-zone validity check used
  in step 4:
  https://manpages.debian.org/bookworm/bind9-utils/named-checkzone.1.en.html.
* ``rndc reload`` reference: ISC bind9 9.18 manpages.
* Consumer wrapper whose pattern is ported:
  https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/bind9-dns.sh.
"""

from __future__ import annotations

import secrets
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "AtomicApplyError",
    "AtomicApplyResult",
    "atomic_apply",
]


# The seven failure steps. Surfaced verbatim in :class:`AtomicApplyError`
# so callers (and audit-replay) can distinguish "validation refused the
# proposed change" from "reload itself failed" from "named loaded the
# change but it doesn't resolve". The naming mirrors the consumer
# wrapper's ``--stage / --validate / --commit / --reload / --verify``
# verbs so an operator switching between the two surfaces sees the
# same vocabulary.
AtomicApplyStep = Literal[
    "snapshot",
    "stage",
    "checkconf",
    "reload",
    "verify",
]


class AtomicApplyError(RuntimeError):
    """Atomic-apply rolled back at *step*.

    Carries the structured failure step so the caller (a handler
    typically) can wrap into the dispatcher's ``connector_error``
    envelope with ``extras.failed_step=<step>``. The pre-op ``/etc/bind/``
    tree is restored byte-identical by the time this is raised.
    """

    def __init__(self, step: AtomicApplyStep, detail: str) -> None:
        super().__init__(f"atomic-apply failed at step={step!r}: {detail}")
        self.step: AtomicApplyStep = step
        self.detail: str = detail


@dataclass(frozen=True)
class AtomicApplyResult:
    """Successful atomic-apply -- pre-op + post-op slice content captured.

    ``state_before`` and ``state_after`` are decoded UTF-8 (with
    ``errors="replace"``) so the audit row carries a JSON-serialisable
    snapshot. ``state_before`` reflects the slice content **before**
    step 3 wrote the staged bytes; ``state_after`` reflects the slice
    after step 5 (``rndc reload``) and step 6 (verify) both succeeded.
    For a successful apply ``state_after`` therefore equals the bytes
    the caller staged plus whatever transformations the post-stage
    pipeline applied -- on the current pipeline that's identity, so
    ``state_after == staged_bytes.decode()`` is the common case.
    """

    state_before: str
    state_after: str
    audit_slice_path: str


# The bash pipeline rendered into the safe-sudo body. ``snapshot_path``
# and ``audit_slice_path`` are interpolated into the script body via
# ``shlex.quote`` so an attacker who controls those values (a future
# caller passing operator-typed paths) cannot break out into arbitrary
# shell. ``audit_slice_path`` flows from the handler's zone-resolution
# logic; it is constrained to the result of ``named-checkconf -p``
# (T2's zone parser), so under the current call sites the value is
# guaranteed to be a literal path bind9 already trusts. The
# ``shlex.quote`` wraps every interpolation regardless -- defence in
# depth.
#
# ``BIND9_VERIFY_CMD`` and ``BIND9_STAGED_B64`` are exported as
# environment variables in the script body (not interpolated into the
# script text) so a verify command containing single quotes or a
# multiline staged-bytes payload cannot break the shell quoting -- the
# script reads them via ``$VAR`` after the bash interpreter has parsed
# the literal script body.
_PIPELINE_TEMPLATE: str = """\
set -e -u -o pipefail
SNAPSHOT_PATH={snapshot_path}
AUDIT_SLICE_PATH={audit_slice_path}
ZONE_NAME={zone_name}
BIND_ROOT={bind_root}

rollback() {{
    if [ -f "$SNAPSHOT_PATH" ]; then
        # Byte-identical rollback contract: the snapshot tar must
        # restore /etc/bind/ to exactly its pre-op shape, including
        # the absence of any file the stage step (or a future T4
        # config-write op) created after the snapshot was taken.
        # ``tar -xzf`` extracts in place but does NOT remove files
        # that exist on disk but not in the archive, so a naive
        # extract leaves orphans behind. We therefore clear $BIND_ROOT
        # first, then extract -- with an explicit guard against an
        # empty / "/" value so a misconfiguration can never rm -rf
        # the host root.
        if [ -n "$BIND_ROOT" ] && [ "$BIND_ROOT" != "/" ]; then
            find "$BIND_ROOT" -mindepth 1 -delete > /dev/null 2>&1 || true
        fi
        tar -xzf "$SNAPSHOT_PATH" -C / > /dev/null 2>&1 || true
        rndc reload > /dev/null 2>&1 || true
    fi
}}

# Step 1: snapshot the bind config root. ``-C /`` so the archive
# carries absolute paths; restoration extracts back into ``/`` cleanly.
# The trailing ``/`` is stripped before passing to tar so the archive
# member names round-trip stably.
mkdir -p "$(dirname "$SNAPSHOT_PATH")"
ROOT_NO_SLASH="${{BIND_ROOT#/}}"
tar -czf "$SNAPSHOT_PATH" -C / "$ROOT_NO_SLASH" >/dev/null

# Step 2: capture state_before. Emitted to stdout with a sentinel so
# the Python side can split out the slice content from the rest of the
# script's progress output. ``cat`` returns 1 on missing; treat that
# as empty (the slice may be a new zonefile being created in a later
# variant) by surfacing an empty marker block.
echo "===STATE_BEFORE_BEGIN==="
if [ -f "$AUDIT_SLICE_PATH" ]; then
    cat "$AUDIT_SLICE_PATH"
fi
echo "===STATE_BEFORE_END==="

# Step 3: stage. The staged bytes arrive base64-encoded in the
# BIND9_STAGED_B64 env var so a payload containing arbitrary bytes
# (including newlines and quoting that would clash with heredoc
# delimiters) round-trips unchanged. Decoded inline. Wrapped in the
# same explicit-check shape every other step uses so a write failure
# (ENOSPC / EACCES / read-only fs / base64 decode failure on a corrupt
# payload) routes through rollback and surfaces step=``stage`` rather
# than exiting under ``set -e`` with no rollback and no FAILED_STEP
# marker (which would degrade to the snapshot-step fallback even
# though the snapshot succeeded and the audit slice may be partially
# truncated).
if ! STAGE_OUT=$(printf '%s' "$BIND9_STAGED_B64" | base64 -d > "$AUDIT_SLICE_PATH" 2>&1); then
    rollback
    echo "===FAILED_STEP===stage"
    echo "===FAILED_DETAIL_BEGIN==="
    printf '%s' "$STAGE_OUT"
    echo
    echo "===FAILED_DETAIL_END==="
    exit 9
fi
# Preserve the bind:bind ownership the daemon expects.
chown root:bind "$AUDIT_SLICE_PATH" 2>/dev/null || true
chmod 644 "$AUDIT_SLICE_PATH" 2>/dev/null || true

# Step 4: validate against the staged file. named-checkzone validates
# the zonefile against the zone name; any non-zero exit triggers
# rollback. Stderr is captured so the failure surface carries the
# real diagnostic (bind9 prints the offending line + reason).
if ! CHECKZONE_OUT=$(named-checkzone "$ZONE_NAME" "$AUDIT_SLICE_PATH" 2>&1); then
    rollback
    echo "===FAILED_STEP===checkconf"
    echo "===FAILED_DETAIL_BEGIN==="
    printf '%s' "$CHECKZONE_OUT"
    echo
    echo "===FAILED_DETAIL_END==="
    exit 10
fi

# Step 5: reload. ``rndc reload`` tells named to re-read the
# now-staged file. A non-zero exit means rndc itself failed (the
# control channel is down, or named is mid-shutdown); the rollback
# restores the prior tree and reloads back to it. The verify step
# below catches the case where rndc succeeded but the change didn't
# propagate to the resolver yet.
if ! RELOAD_OUT=$(rndc reload 2>&1); then
    rollback
    echo "===FAILED_STEP===reload"
    echo "===FAILED_DETAIL_BEGIN==="
    printf '%s' "$RELOAD_OUT"
    echo
    echo "===FAILED_DETAIL_END==="
    exit 11
fi

# Step 6: verify. The caller-supplied predicate must exit 0; any
# non-zero is verification failure. The predicate runs *after*
# ``rndc reload`` returns -- bind9 zone reloads are typically
# synchronous-enough for the dig predicate to see the change on the
# first poll, but the predicate may itself loop (the handler decides
# its own polling strategy; this primitive treats it as a single
# command).
if ! VERIFY_OUT=$(eval "$BIND9_VERIFY_CMD" 2>&1); then
    rollback
    echo "===FAILED_STEP===verify"
    echo "===FAILED_DETAIL_BEGIN==="
    printf '%s' "$VERIFY_OUT"
    echo
    echo "===FAILED_DETAIL_END==="
    exit 12
fi

# Step 7 (success): capture state_after and emit. Snapshot can be
# discarded; the rollback path no longer needs it.
echo "===STATE_AFTER_BEGIN==="
cat "$AUDIT_SLICE_PATH"
echo "===STATE_AFTER_END==="

rm -f "$SNAPSHOT_PATH" || true
echo "===SUCCESS==="
"""


def _build_pipeline_script(
    *,
    snapshot_path: str,
    audit_slice_path: str,
    zone_name: str,
    bind_root: str,
) -> str:
    """Render the seven-step bash pipeline with safely-quoted paths.

    ``shlex.quote`` is the canonical single-line POSIX-shell quoter and
    is what stdlib uses for shell-command construction. Each
    interpolation is wrapped so an operator-typed value with shell
    metacharacters cannot break out of its position. ``zone_name`` is
    the only operator-typed value with a non-path shape, but the same
    quoting works -- bind9 zone names are dotted DNS labels and never
    contain shell metacharacters in practice; the quote covers any
    future input shape.
    """
    return _PIPELINE_TEMPLATE.format(
        snapshot_path=shlex.quote(snapshot_path),
        audit_slice_path=shlex.quote(audit_slice_path),
        zone_name=shlex.quote(zone_name),
        bind_root=shlex.quote(bind_root),
    )


def _parse_pipeline_output(
    stdout: str,
) -> dict[str, str]:
    """Extract the sentinel-delimited sections from the pipeline's stdout.

    The bash script emits ``===<NAME>_BEGIN===`` / ``===<NAME>_END===``
    bracketing for ``STATE_BEFORE`` and ``STATE_AFTER`` (the audit
    slice content) and ``===<NAME>_BEGIN===`` / ``===<NAME>_END===``
    for ``FAILED_DETAIL`` (on failure). ``===FAILED_STEP===<name>``
    is a one-liner. ``===SUCCESS===`` is the success terminator.

    The parser is conservative: missing sentinels surface as missing
    dict keys, not empty strings -- so the caller can distinguish "the
    section was present and contained empty content" from "the script
    didn't reach that section".

    Newline fidelity: ``splitlines(keepends=True)`` preserves each
    line's exact line-ending bytes; sentinel-bracketed sections are
    reassembled via ``"".join(...)`` so the captured slice round-trips
    every byte of the original content (including or excluding a
    trailing newline depending on what the source file carried). The
    audit-replay path (G8.2) compares pre/post-op slice content
    byte-for-byte; a parser that normalised line endings would force
    every replay to diff cosmetically and obscure real semantic
    differences. Marker detection uses ``rstrip("\\r\\n")`` so the
    ``=== ... ===`` prefix/suffix checks match regardless of the
    line's terminator (Unix LF, Windows CRLF, or a final line with no
    terminator at all).
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    failed_step: str | None = None
    saw_success = False

    for raw_line in stdout.splitlines(keepends=True):
        stripped = raw_line.rstrip("\r\n")
        # ``===FAILED_STEP===<name>`` -- one-line marker
        if stripped.startswith("===FAILED_STEP==="):
            failed_step = stripped[len("===FAILED_STEP===") :].strip()
            continue
        if stripped == "===SUCCESS===":
            saw_success = True
            continue
        # ``===<NAME>_BEGIN===`` opens a multi-line section
        if stripped.startswith("===") and stripped.endswith("_BEGIN==="):
            name = stripped[len("===") : -len("_BEGIN===")]
            current = name
            sections[name] = []
            continue
        # ``===<NAME>_END===`` closes the current section
        if stripped.startswith("===") and stripped.endswith("_END==="):
            current = None
            continue
        if current is not None:
            sections[current].append(raw_line)

    result: dict[str, str] = {name: "".join(lines) for name, lines in sections.items()}
    # The bash script bookends each captured slice with explicit
    # ``echo`` calls (which append a newline of their own) so the slice
    # arrives with one trailing LF that the script added, not the
    # source file. Strip exactly one terminal newline (if present) so
    # the captured value matches what ``cat`` produced on the wire --
    # the file content itself. Multi-line slices keep their internal
    # newlines intact byte-for-byte. The "exactly one" rule is what
    # distinguishes this from the pre-fix splitlines/join shape, which
    # silently dropped a trailing-newline distinction that mattered
    # for audit replay.
    for name in ("STATE_BEFORE", "STATE_AFTER", "FAILED_DETAIL"):
        if name in result and result[name].endswith("\n"):
            result[name] = result[name][:-1]
    if failed_step is not None:
        result["FAILED_STEP"] = failed_step
    if saw_success:
        result["SUCCESS"] = ""
    return result


def _compose_full_script(
    *,
    staged_bytes: bytes,
    verify_command: str,
    pipeline_script: str,
) -> str:
    """Prepend env-var assignments so the pipeline script's ``$VAR`` refs resolve.

    base64-encoding the staged bytes keeps the value safe to splice
    into an env-var assignment regardless of the bytes' shape (control
    characters, embedded NULs, etc.). ``BIND9_VERIFY_CMD`` is
    single-quoted; any single quote in the caller's command is escaped
    via the standard ``'\\''`` rewrite. Pulled out of
    :func:`atomic_apply` to keep the parent function within the
    code-quality block limit and to make the env-var quoting logic
    independently testable.
    """
    import base64

    staged_b64 = base64.b64encode(staged_bytes).decode("ascii")
    verify_quoted = "'" + verify_command.replace("'", "'\\''") + "'"
    return (
        f"export BIND9_STAGED_B64='{staged_b64}'\n"
        f"export BIND9_VERIFY_CMD={verify_quoted}\n"
        f"{pipeline_script}"
    )


def _interpret_pipeline_result(
    proc: Any,
    *,
    audit_slice_path: str,
) -> AtomicApplyResult:
    """Translate the safe-sudo proc result into success or :class:`AtomicApplyError`.

    Pulled out of :func:`atomic_apply` so the post-exec decoding is
    a single non-async function -- callers exercising the parsing
    contract in unit tests don't need to spin an event loop.

    Raises :class:`AtomicApplyError` with the failed step on any
    failure surface (parsed ``FAILED_STEP`` or unparseable output).
    Returns an :class:`AtomicApplyResult` on success.
    """
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    output = stdout if isinstance(stdout, str) else ""
    sections = _parse_pipeline_output(output)

    if "FAILED_STEP" in sections:
        step_raw = sections["FAILED_STEP"]
        detail = sections.get("FAILED_DETAIL", "")
        # Narrow to the typed literal -- defensive against a future
        # bash-side typo emitting an unknown step name; surface
        # "unknown" as ``checkconf`` (the conservative default --
        # rollback-on-validation, the most paranoid step).
        step: AtomicApplyStep
        if step_raw in ("snapshot", "stage", "checkconf", "reload", "verify"):
            step = step_raw  # type: ignore[assignment]
        else:
            step = "checkconf"
        raise AtomicApplyError(step, detail)

    if "SUCCESS" not in sections:
        # Pipeline neither emitted FAILED_STEP nor SUCCESS -- treat as
        # a snapshot-step failure (the most paranoid surface: the
        # snapshot tar may or may not exist; named's running config
        # is untouched).
        stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
        stderr_text = stderr if isinstance(stderr, str) else ""
        detail = stderr_text or output or "no output"
        raise AtomicApplyError("snapshot", detail)

    return AtomicApplyResult(
        state_before=sections.get("STATE_BEFORE", ""),
        state_after=sections.get("STATE_AFTER", ""),
        audit_slice_path=audit_slice_path,
    )


async def atomic_apply(
    connector: Bind9Connector,
    target: Any,
    *,
    raw_jwt: str,
    sudo_password: str,
    audit_slice_path: str,
    zone_name: str,
    staged_bytes: bytes,
    verify_command: str,
    bind_root: str = "/etc/bind",
) -> AtomicApplyResult:
    """Stage *staged_bytes* at *audit_slice_path* with full snapshot rollback.

    Returns :class:`AtomicApplyResult` on success; raises
    :class:`AtomicApplyError` with the failed step on any failure.
    By the time this returns (success or raise), the remote
    ``/etc/bind/`` tree is in a single coherent state: either the
    staged file is live + reload succeeded + verify succeeded, OR the
    pre-op tree is restored byte-identical via the snapshot tar.

    Parameters
    ----------
    connector
        The :class:`Bind9Connector` -- routes sudo through
        :meth:`~Bind9Connector._remote_bash_with_sudo`.
    target
        The remote bind9 target.
    raw_jwt, sudo_password
        Forwarded to ``_remote_bash_with_sudo``. The sudo password
        invariants (no newlines / NUL) apply.
    audit_slice_path
        The file the operation semantically edits (the affected
        zonefile, typically). Used for ``state_before`` /
        ``state_after`` capture and as the staging write target.
    zone_name
        Zone name passed to ``named-checkzone`` for validation.
    staged_bytes
        The proposed file content. Streamed via a base64 env var so
        arbitrary bytes (incl. newlines) round-trip unchanged.
    verify_command
        Shell command that exits 0 iff the post-reload state matches
        the operation's intent. Commonly
        ``dig @localhost <fqdn> +short A | grep -qx <expected-ip>``.
        Run under sudo (the same shell as the rest of the pipeline);
        callers needing to drop privileges should do so inside the
        command.
    bind_root
        Root directory snapshotted by tar. Defaults to ``/etc/bind``;
        the only escape hatch is for tests using a temporary tree.

    Raises
    ------
    AtomicApplyError
        On any failure at any step. ``error.step`` carries the
        failing step verb; the pre-op tree is restored.
    """
    # Per-call snapshot path. ``secrets.token_hex`` gives 32 hex chars
    # of CSPRNG entropy -- collision-resistant across concurrent
    # invocations on the same target (the orchestrator may fan out
    # parallel writes; each gets its own snapshot tar).
    snapshot_token = secrets.token_hex(16)
    snapshot_path = f"/tmp/meho-bind9-snapshot-{snapshot_token}.tar.gz"
    pipeline_script = _build_pipeline_script(
        snapshot_path=snapshot_path,
        audit_slice_path=audit_slice_path,
        zone_name=zone_name,
        bind_root=bind_root,
    )
    full_script = _compose_full_script(
        staged_bytes=staged_bytes,
        verify_command=verify_command,
        pipeline_script=pipeline_script,
    )
    proc = await connector._remote_bash_with_sudo(
        target,
        full_script,
        raw_jwt=raw_jwt,
        sudo_password=sudo_password,
        timeout=90.0,
    )
    return _interpret_pipeline_result(proc, audit_slice_path=audit_slice_path)
