# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two bind9 zone writes at the same time must not undo each other.

Two layers are tested:

* The bash pipeline of ``atomic_apply`` is run for real (with fake
  ``rndc`` / ``named-checkzone`` programs) to prove the server lock and
  the "file changed since it was read" check.
* The record handlers are run against an in-memory zone to prove that a
  write whose file changed reads it again and redoes its change.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import meho_backplane.connectors.bind9.ops_record as ops_record_module
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.bind9._atomic import (
    AtomicApplyError,
    AtomicApplyResult,
    _build_pipeline_script,
    _compose_full_script,
    _interpret_pipeline_result,
    atomic_apply,
)
from meho_backplane.connectors.bind9.ops_record import (
    bind9_record_add,
    bind9_record_delete,
    bind9_record_remove,
)

_ZONE_TEXT = (
    "$ORIGIN evba.lab.\n"
    "$TTL 3600\n"
    "@ IN SOA ns1.evba.lab. admin.evba.lab. (\n"
    "    2026010101 ; serial\n"
    "    3600 1800 604800 3600 )\n"
    "@ IN NS ns1.evba.lab.\n"
    "ns1 IN A 10.0.0.1\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The real bash pipeline
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("sha256sum") is None,
    reason="needs flock and sha256sum",
)
class TestPipelineLockAndConflict:
    @staticmethod
    def _sandbox(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
        bind_root = tmp_path / "bind"
        bind_root.mkdir()
        zonefile = bind_root / "db.evba.lab"
        zonefile.write_text(_ZONE_TEXT)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # Fake rndc is slow on purpose, so two applies would overlap
        # without the lock.
        for name, body in (("rndc", "sleep 0.3\nexit 0\n"), ("named-checkzone", "exit 0\n")):
            prog = bin_dir / name
            prog.write_text("#!/bin/sh\n" + body)
            prog.chmod(prog.stat().st_mode | stat.S_IXUSR)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        return bind_root, zonefile, env

    @staticmethod
    def _script(
        tmp_path: Path,
        bind_root: Path,
        zonefile: Path,
        *,
        name: str,
        new_text: str,
        expected: str | None,
        lock_wait: int = 20,
    ) -> str:
        pipeline = _build_pipeline_script(
            snapshot_path=str(tmp_path / f"snap-{name}.tar.gz"),
            audit_slice_path=str(zonefile),
            zone_name="evba.lab",
            bind_root=str(bind_root),
            lock_path=str(tmp_path / "apply.lock"),
            lock_wait_seconds=lock_wait,
        )
        return _compose_full_script(
            staged_bytes=new_text.encode("utf-8"),
            staged_tar_bytes=None,
            validate_command="true",
            verify_command="true",
            pipeline_script=pipeline,
            expected_sha256=expected,
        )

    def test_two_parallel_applies_one_lands_and_one_reports_conflict(self, tmp_path: Path) -> None:
        bind_root, zonefile, env = self._sandbox(tmp_path)
        scripts = {
            name: self._script(
                tmp_path,
                bind_root,
                zonefile,
                name=name,
                new_text=_ZONE_TEXT + f"{name} IN A 10.0.0.9\n",
                expected=_sha(_ZONE_TEXT),
            )
            for name in ("alpha", "beta")
        }
        procs = {
            name: subprocess.Popen(
                ["bash", "-c", script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name, script in scripts.items()
        }
        outputs = {name: proc.communicate(timeout=60)[0] for name, proc in procs.items()}
        winners = [n for n, out in outputs.items() if "===SUCCESS===" in out]
        losers = [n for n, out in outputs.items() if "===FAILED_STEP===conflict" in out]
        assert len(winners) == 1, outputs
        assert len(losers) == 1, outputs
        # The loser wrote nothing: the file is exactly the winner's content.
        assert zonefile.read_text() == _ZONE_TEXT + f"{winners[0]} IN A 10.0.0.9\n"
        # And the loser's error is the conflict step the handlers retry on.
        proc = type("P", (), {"stdout": outputs[losers[0]], "stderr": "", "exit_status": 7})()
        with pytest.raises(AtomicApplyError) as exc_info:
            _interpret_pipeline_result(proc, audit_slice_path=str(zonefile))
        assert exc_info.value.step == "conflict"

    def test_apply_on_unchanged_file_lands(self, tmp_path: Path) -> None:
        bind_root, zonefile, env = self._sandbox(tmp_path)
        script = self._script(
            tmp_path,
            bind_root,
            zonefile,
            name="one",
            new_text=_ZONE_TEXT + "x IN A 10.0.0.7\n",
            expected=_sha(_ZONE_TEXT),
        )
        out = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False
        ).stdout
        assert "===SUCCESS===" in out
        assert "x IN A 10.0.0.7" in zonefile.read_text()

    def test_missing_file_with_a_hash_is_a_conflict(self, tmp_path: Path) -> None:
        bind_root, zonefile, env = self._sandbox(tmp_path)
        zonefile.unlink()
        script = self._script(
            tmp_path, bind_root, zonefile, name="m", new_text="x\n", expected=_sha(_ZONE_TEXT)
        )
        out = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False
        ).stdout
        assert "===FAILED_STEP===conflict" in out
        assert not zonefile.exists()

    def test_no_expected_hash_skips_the_check(self, tmp_path: Path) -> None:
        bind_root, zonefile, env = self._sandbox(tmp_path)
        zonefile.write_text(_ZONE_TEXT + "changed IN A 10.0.0.5\n")
        script = self._script(
            tmp_path, bind_root, zonefile, name="n", new_text="replaced\n", expected=None
        )
        out = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False
        ).stdout
        assert "===SUCCESS===" in out
        assert zonefile.read_text() == "replaced\n"

    def test_lock_not_free_in_time_fails_at_lock_step_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        bind_root, zonefile, env = self._sandbox(tmp_path)
        holder = subprocess.Popen(
            ["flock", str(tmp_path / "apply.lock"), "sleep", "10"],
        )
        try:
            script = self._script(
                tmp_path,
                bind_root,
                zonefile,
                name="l",
                new_text="should not land\n",
                expected=None,
                lock_wait=1,
            )
            out = subprocess.run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout
        finally:
            holder.kill()
            holder.wait()
        assert "===FAILED_STEP===lock" in out
        assert zonefile.read_text() == _ZONE_TEXT

    def test_conflict_check_runs_before_the_snapshot(self) -> None:
        script = _build_pipeline_script(
            snapshot_path="/tmp/s",
            audit_slice_path="/etc/bind/db.x",
            zone_name="x",
            bind_root="/etc/bind",
        )
        assert script.index("flock -w") < script.index("===FAILED_STEP===conflict")
        assert script.index("===FAILED_STEP===conflict") < script.index("tar -czf")


class TestExpectedHashValidation:
    async def test_non_hex_hash_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expected_sha256"):
            await atomic_apply(
                Bind9Connector(),
                object(),
                sudo_password="pw",  # NOSONAR -- unit-test stub
                audit_slice_path="/etc/bind/db.x",
                zone_name="x",
                staged_bytes=b"x",
                verify_command="true",
                expected_sha256="abc'; rm -rf /; echo '",
            )


# ---------------------------------------------------------------------------
# The record handlers on an in-memory zone
# ---------------------------------------------------------------------------


class _FakeZone:
    """One zonefile whose write checks the hash, like the bash pipeline does."""

    def __init__(self) -> None:
        self.text = _ZONE_TEXT
        self.read_count = 0
        self.expected_hashes: list[str | None] = []

    async def read(self, *_a: Any, **_k: Any) -> str:
        self.read_count += 1
        snapshot = self.text
        # Let the other writer read the same content before this one writes.
        await asyncio.sleep(0.01)
        return snapshot

    async def apply(self, *_a: Any, **kw: Any) -> AtomicApplyResult:
        self.expected_hashes.append(kw["expected_sha256"])
        if kw["expected_sha256"] != _sha(self.text):
            raise AtomicApplyError("conflict", "the file changed after it was read")
        before = self.text
        self.text = kw["staged_bytes"].decode("utf-8")
        return AtomicApplyResult(
            state_before=before, state_after=self.text, audit_slice_path="/etc/bind/db.evba.lab"
        )


@pytest.fixture
def zone() -> Any:
    fake = _FakeZone()

    async def _resolve(*_a: Any, **_k: Any) -> tuple[str, str, str | None]:
        return "evba.lab", "/etc/bind/db.evba.lab", None

    async def _pw(*_a: Any, **_k: Any) -> str:
        return "pw"

    with (
        patch.object(ops_record_module, "_resolve_zone_and_path", _resolve),
        patch.object(ops_record_module, "_sudo_password_from_target", _pw),
        patch.object(ops_record_module, "_read_zonefile_text", fake.read),
        patch.object(ops_record_module, "atomic_apply", fake.apply),
    ):
        yield fake


async def _add(name: str, ip: str) -> dict[str, Any]:
    return await bind9_record_add(
        Bind9Connector(),
        object(),
        {"fqdn": f"{name}.evba.lab", "ip": ip, "type": "A", "zone": "evba.lab"},
    )


class TestHandlersRedoOnConflict:
    async def test_two_parallel_adds_both_land(self, zone: _FakeZone) -> None:
        results = await asyncio.gather(_add("one", "10.0.0.11"), _add("two", "10.0.0.12"))
        assert {r["fqdn"] for r in results} == {"one.evba.lab", "two.evba.lab"}
        assert "10.0.0.11" in zone.text
        assert "10.0.0.12" in zone.text
        # Each write raised the serial once: 2026010101 -> 2026010103.
        assert "2026010103" in zone.text
        # One writer was refused once and read the file again.
        assert zone.read_count == 3
        assert zone.expected_hashes[0] == zone.expected_hashes[1] == _sha(_ZONE_TEXT)
        assert zone.expected_hashes[2] != _sha(_ZONE_TEXT)

    async def test_add_and_remove_in_parallel_both_land(self, zone: _FakeZone) -> None:
        zone.text = _ZONE_TEXT + "old IN A 10.0.0.30\n"

        async def _remove() -> dict[str, Any]:
            return await bind9_record_remove(
                Bind9Connector(), object(), {"fqdn": "old.evba.lab", "zone": "evba.lab"}
            )

        await asyncio.gather(_add("new", "10.0.0.31"), _remove())
        assert "10.0.0.31" in zone.text
        assert "10.0.0.30" not in zone.text

    async def test_delete_that_loses_the_race_is_refused_not_found(self, zone: _FakeZone) -> None:
        zone.text = _ZONE_TEXT + "gone IN A 10.0.0.40\n"

        async def _delete() -> dict[str, Any]:
            return await bind9_record_delete(
                Bind9Connector(),
                object(),
                {"fqdn": "gone.evba.lab", "type": "A", "zone": "evba.lab"},
            )

        async def _remove() -> dict[str, Any]:
            return await bind9_record_remove(
                Bind9Connector(), object(), {"fqdn": "gone.evba.lab", "zone": "evba.lab"}
            )

        deleted, _removed = await asyncio.gather(_delete(), _remove())
        # The remove landed first or second; either way the record is gone and
        # a delete that finds nothing on the new file says so.
        assert "10.0.0.40" not in zone.text
        assert deleted["status"] in {"deleted", "not_found"}
        if deleted["status"] == "not_found":
            assert deleted["deleted"] is False

    async def test_conflict_that_never_ends_is_raised_after_the_retry_limit(self) -> None:
        reads = 0

        async def _read(*_a: Any, **_k: Any) -> str:
            nonlocal reads
            reads += 1
            return _ZONE_TEXT

        async def _always_conflict(*_a: Any, **_k: Any) -> AtomicApplyResult:
            raise AtomicApplyError("conflict", "the file changed after it was read")

        async def _resolve(*_a: Any, **_k: Any) -> tuple[str, str, str | None]:
            return "evba.lab", "/etc/bind/db.evba.lab", None

        async def _pw(*_a: Any, **_k: Any) -> str:
            return "pw"

        with (
            patch.object(ops_record_module, "_resolve_zone_and_path", _resolve),
            patch.object(ops_record_module, "_sudo_password_from_target", _pw),
            patch.object(ops_record_module, "_read_zonefile_text", _read),
            patch.object(ops_record_module, "atomic_apply", _always_conflict),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await _add("x", "10.0.0.50")
        assert exc_info.value.step == "conflict"
        assert reads == ops_record_module._MAX_CONFLICT_RETRIES + 1

    async def test_other_failures_are_not_retried(self) -> None:
        reads = 0

        async def _read(*_a: Any, **_k: Any) -> str:
            nonlocal reads
            reads += 1
            return _ZONE_TEXT

        async def _verify_failed(*_a: Any, **_k: Any) -> AtomicApplyResult:
            raise AtomicApplyError("verify", "dig did not answer")

        async def _resolve(*_a: Any, **_k: Any) -> tuple[str, str, str | None]:
            return "evba.lab", "/etc/bind/db.evba.lab", None

        async def _pw(*_a: Any, **_k: Any) -> str:
            return "pw"

        with (
            patch.object(ops_record_module, "_resolve_zone_and_path", _resolve),
            patch.object(ops_record_module, "_sudo_password_from_target", _pw),
            patch.object(ops_record_module, "_read_zonefile_text", _read),
            patch.object(ops_record_module, "atomic_apply", _verify_failed),
            pytest.raises(AtomicApplyError) as exc_info,
        ):
            await _add("x", "10.0.0.50")
        assert exc_info.value.step == "verify"
        assert reads == 1
