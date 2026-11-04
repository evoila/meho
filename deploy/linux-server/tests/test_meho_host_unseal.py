#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host keyholder on, keyholder off and unseal (#4738), against
a fake cluster with one OpenBao pod: the switch of the mode, the restart,
the check of the key, every failed step, and the unseal key, which never
appears in the process list, the output or a file."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import termios
import time
import unittest
from unittest import mock

from cluster_harness import (
    CORE_LABELS,
    OTHER_KEY,
    RKE2_KUBECTL,
    SUPPORT_HINT,
    UNSEAL_KEY,
    UNSEAL_KEY_FILE,
    UNSEAL_SECRET,
    ClusterHost,
    ClusterTestCase,
    initial_state,
)
from host_harness import MEHO_HOST

ON_INTRO = (
    "In keyholder mode, OpenBao stays sealed after every restart of its pod or of this machine, "
    "and MEHO waits, until a keyholder runs: sudo meho-host unseal. Without the unseal key, "
    "nobody can open the secrets of MEHO again. Give the key from your key store, to show that "
    "it is there.\n"
)
OFF_INTRO = (
    "OpenBao will unseal itself after every restart again: the unseal key is kept in the Secret "
    "meho-core-vault-unseal on this machine, next to the data. Give the key from your key "
    "store.\n"
)
RESTARTING = "[note] restarting OpenBao. This can take up to 5 minutes.\n"
NODE_FIX = f"Fix: check the node and pods lines: sudo meho-host check. Then {SUPPORT_HINT}"
EXEC_UNSEAL = "-n meho exec -i meho-vault-0 -c bao -- bao write sys/unseal key=-"
WRONG_KEY_FIX = "Fix: copy the key again from your key store, and run sudo meho-host unseal again."


def run_on_a_terminal(host: ClusterHost, *args: str, typed: str) -> tuple[int, str]:
    """Run meho-host with a terminal as its input and output, and type the
    text only once the terminal echoes nothing: once read -s turned the
    echo off. Return the exit code and everything the terminal showed."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [str(MEHO_HOST), *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=host.environment(),
            start_new_session=True,
        )
        shown, typed_yet, deadline = b"", False, time.monotonic() + 30
        while time.monotonic() < deadline:
            echo_off = not termios.tcgetattr(slave)[3] & termios.ECHO
            if not typed_yet and echo_off and b"(it is not shown): " in shown:
                os.write(master, typed.encode() + b"\n")
                typed_yet = True
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                shown += os.read(master, 4096)
            elif process.poll() is not None:
                break
        else:
            process.kill()
        return process.wait(timeout=30), shown.decode()
    finally:
        os.close(master)
        os.close(slave)


class KeyholderOnTests(ClusterTestCase):
    def test_with_the_right_key_the_secret_goes_and_openbao_restarts_sealed(self) -> None:
        result = self.host.run("keyholder", "on", stdin=UNSEAL_KEY + "\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            ON_INTRO
            + "[ok]   deleted the Secret meho-core-vault-unseal: OpenBao no longer unseals itself\n"
            + RESTARTING
            + "[ok]   OpenBao restarted and is sealed, so MEHO waits now. Unseal it with: sudo "
            "meho-host unseal\n",
            result.stdout,
        )
        self.assertEqual("", result.stderr)
        self.assertIsNone(self.host.secret(UNSEAL_SECRET))
        state = self.host.state()["openbao"]
        self.assertEqual((True, 1), (state["sealed"], state["restarts"]))
        self.assertEqual(1, len(self.kubectl_calls("-n meho delete secret meho-core-vault-unseal")))
        self.assertEqual(1, len(self.kubectl_calls("rollout restart statefulset/meho-vault")))
        self.assertIn("sleep 5\n", self.host.calls())

    def test_a_wrong_key_changes_nothing(self) -> None:
        result = self.host.run("keyholder", "on", stdin=OTHER_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] this is not the unseal key of this OpenBao. Nothing was changed. Fix: copy the "
            "key again from your key store. If it is not there, copy it there first from "
            "/var/lib/meho/unseal-key, while that file exists.",
        )
        self.assertEqual(UNSEAL_KEY, self.host.secret(UNSEAL_SECRET)["data"]["key"])  # type: ignore[index]
        self.assertEqual([], self.kubectl_calls(" delete "))
        self.assertEqual([], self.kubectl_calls(" rollout "))

    def test_no_key_or_text_that_is_not_a_key_changes_nothing(self) -> None:
        cases = {
            "": "[FAIL] no unseal key was given. Nothing was changed. Fix: run the command again "
            "and give the key from your key store, on the prompt or on standard input.",
            "made-up phrase!\n": "[FAIL] this is not an unseal key: an unseal key holds only "
            "letters, digits, + and /, with = at the end. Nothing was changed. Fix: copy the key "
            "again from your key store, with nothing before or after it.",
        }
        for stdin, line in cases.items():
            with self.subTest(stdin=stdin):
                result = self.host.run("keyholder", "on", stdin=stdin)

                self.assert_one_fail_line(result, line)
                self.assertNotIn("phrase!", self.output(result))
                self.assertIsNotNone(self.host.secret(UNSEAL_SECRET))
                self.assertEqual([], self.kubectl_calls(" delete "))

    def test_in_keyholder_mode_already_it_changes_nothing_and_asks_for_no_key(self) -> None:
        self.host.keyholder_mode(sealed=False)

        result = self.host.run("keyholder", "on")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[ok]   OpenBao is in keyholder mode already: a keyholder unseals it after every "
            "restart. Nothing was changed.\n",
            result.stdout,
        )
        self.assertEqual([], self.kubectl_calls(" rollout "))

    def test_the_one_time_key_file_gets_a_warning(self) -> None:
        self.host.host_file(UNSEAL_KEY_FILE, UNSEAL_KEY + "\n")

        result = self.host.run("keyholder", "on", stdin=UNSEAL_KEY + "\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[WARN] /var/lib/meho/unseal-key still holds the unseal key on this machine. Delete it "
            "now that the key is in your key store: rm /var/lib/meho/unseal-key\n",
            result.stderr,
        )
        self.assertTrue(self.host.host_path(UNSEAL_KEY_FILE).exists())

    def test_openbao_that_does_not_answer_after_the_restart_is_red(self) -> None:
        self.host.change(boot_status_calls=1000)

        result = self.host.run("keyholder", "on", stdin=UNSEAL_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] OpenBao did not answer within 5 minutes after the restart. It is in keyholder "
            "mode. Fix: check the pods line: sudo meho-host check. When OpenBao runs, unseal it "
            f"with: sudo meho-host unseal. If it does not start, {SUPPORT_HINT}",
        )
        self.assertEqual(60, self.host.calls().count("sleep 5\n"))

    def test_a_failed_step_says_what_changed(self) -> None:
        cases = {
            "down": (
                "[FAIL] RKE2 does not answer, so meho-host cannot read the Secret "
                f"meho-core-vault-unseal. Nothing was changed. {NODE_FIX}",
                True,
            ),
            "delete": (
                "[FAIL] the Secret meho-core-vault-unseal could not be deleted. Nothing was "
                f"changed. {NODE_FIX}",
                True,
            ),
            "restart": (
                "[FAIL] OpenBao could not be restarted, so it stays unsealed until its next "
                f"restart. From then on, a keyholder unseals it. {NODE_FIX}",
                False,
            ),
        }
        for failing, (line, secret_stays) in cases.items():
            with self.subTest(failing=failing):
                self.host.save(initial_state())
                if failing == "down":
                    state = self.host.state()
                    state["down"] = True
                    self.host.save(state)
                else:
                    self.host.fail(failing)

                result = self.host.run("keyholder", "on", stdin=UNSEAL_KEY + "\n")

                self.assertEqual(1, result.returncode, self.output(result))
                self.assertEqual(line, result.stderr.splitlines()[-1])
                state = self.host.state()
                self.assertEqual(secret_stays, UNSEAL_SECRET in state["objects"])
                self.assertEqual(0, state["openbao"]["restarts"])


class KeyholderOffTests(ClusterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.keyholder_mode(sealed=False)

    def test_the_secret_comes_back_and_openbao_unseals_itself_after_the_restart(self) -> None:
        result = self.host.run("keyholder", "off", stdin=UNSEAL_KEY + "\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            OFF_INTRO
            + "[ok]   created the Secret meho-core-vault-unseal with the unseal key\n"
            + RESTARTING
            + "[ok]   OpenBao restarted and unsealed itself: it does so after every restart now\n",
            result.stdout,
        )
        self.assertEqual(
            {"type": "Opaque", "labels": CORE_LABELS, "data": {"key": UNSEAL_KEY}},
            self.host.secret(UNSEAL_SECRET),
        )
        state = self.host.state()["openbao"]
        self.assertEqual((False, 1), (state["sealed"], state["restarts"]))
        self.assertEqual(1, len(self.kubectl_calls("-n meho create -f -")))

    def test_when_sealed_the_key_unseals_first(self) -> None:
        self.host.change(sealed=True)

        result = self.host.run("keyholder", "off", stdin=UNSEAL_KEY + "\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(OFF_INTRO + "[ok]   unsealed OpenBao with the key\n", result.stdout)
        self.assertEqual(1, len(self.kubectl_calls(EXEC_UNSEAL)))
        self.assertIsNotNone(self.host.secret(UNSEAL_SECRET))

    def test_when_sealed_a_wrong_key_changes_nothing(self) -> None:
        self.host.change(sealed=True)

        result = self.host.run("keyholder", "off", stdin=OTHER_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] OpenBao is still sealed after the key was sent, so this is not the unseal key "
            "of this OpenBao. Nothing was changed. Fix: copy the key again from your key store, "
            "and run the command again.",
        )
        self.assertIsNone(self.host.secret(UNSEAL_SECRET))
        self.assertEqual(0, self.host.state()["openbao"]["restarts"])

    def test_when_unsealed_the_restart_finds_a_wrong_key_and_the_secret_goes_again(self) -> None:
        result = self.host.run("keyholder", "off", stdin=OTHER_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] OpenBao is still sealed 5 minutes after the restart, so the key does not "
            "unseal it. meho-host deleted the Secret meho-core-vault-unseal again: OpenBao stays "
            "in keyholder mode. Fix: unseal it with the right key from your key store: sudo "
            "meho-host unseal. Then run sudo meho-host keyholder off again with the same key.",
        )
        self.assertIsNone(self.host.secret(UNSEAL_SECRET))
        self.assertEqual(60, self.host.calls().count("sleep 5\n"))

    def test_with_the_secret_already_it_changes_nothing(self) -> None:
        self.host.save(initial_state())

        result = self.host.run("keyholder", "off")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[ok]   OpenBao unseals itself already: the Secret meho-core-vault-unseal holds its "
            "key. Nothing was changed.\n",
            result.stdout,
        )
        self.assertEqual([], self.kubectl_calls(" create "))

    def test_a_failed_create_changes_nothing_and_hides_kubectls_message(self) -> None:
        self.host.fail("create")

        result = self.host.run("keyholder", "off", stdin=UNSEAL_KEY + "\n")

        self.assert_one_fail_line(
            result,
            f"[FAIL] the Secret meho-core-vault-unseal could not be created. Nothing was changed. "
            f"{NODE_FIX}",
        )
        self.assertNotIn(UNSEAL_KEY, self.output(result))
        self.assertEqual(0, self.host.state()["openbao"]["restarts"])


class UnsealTests(ClusterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.keyholder_mode(sealed=True)

    def test_the_right_key_unseals_and_goes_on_standard_input(self) -> None:
        result = self.host.run("unseal", stdin=UNSEAL_KEY + "\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[ok]   unsealed: OpenBao is open again, and MEHO is ready within a minute. Check it "
            "with: sudo meho-host check\n",
            result.stdout,
        )
        self.assertFalse(self.host.state()["openbao"]["sealed"])
        self.assertEqual(1, len(self.kubectl_calls(EXEC_UNSEAL)))
        self.assertEqual(
            ["exec -i meho-vault-0 -c bao -- bao write sys/unseal key=-"],
            self.host.state()["key_inputs"],
        )

    def test_a_wrong_key_leaves_it_sealed(self) -> None:
        result = self.host.run("unseal", stdin=OTHER_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] sealed: OpenBao is still sealed, so this is not the unseal key of this "
            f"OpenBao. {WRONG_KEY_FIX}",
        )
        self.assertTrue(self.host.state()["openbao"]["sealed"])

    def test_white_space_and_windows_line_ends_around_the_key_are_removed(self) -> None:
        result = self.host.run("unseal", stdin=f"  {UNSEAL_KEY} \r\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertFalse(self.host.state()["openbao"]["sealed"])

    def test_unsealed_already_it_asks_for_no_key(self) -> None:
        self.host.change(sealed=False)

        result = self.host.run("unseal")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[ok]   unsealed: OpenBao is unsealed already. Nothing was changed.\n", result.stdout
        )
        self.assertEqual([], self.kubectl_calls(EXEC_UNSEAL))

    def test_openbao_that_does_not_answer_cannot_be_unsealed(self) -> None:
        self.host.change(answers=False)

        result = self.host.run("unseal", stdin=UNSEAL_KEY + "\n")

        self.assert_one_fail_line(
            result,
            "[FAIL] OpenBao does not answer, so it cannot be unsealed now. Fix: check the pods "
            f"line: sudo meho-host check. If OpenBao does not start, {SUPPORT_HINT}",
        )
        self.assertEqual([], self.kubectl_calls(EXEC_UNSEAL))

    def test_on_a_terminal_the_prompt_hides_the_key(self) -> None:
        code, shown = run_on_a_terminal(self.host, "unseal", typed=UNSEAL_KEY)

        self.assertEqual(0, code, shown)
        self.assertIn("Unseal key (it is not shown): ", shown)
        self.assertIn("[ok]   unsealed:", shown)
        self.assertNotIn(UNSEAL_KEY, shown)
        self.assertFalse(self.host.state()["openbao"]["sealed"])

    def test_a_key_on_the_command_line_is_a_usage_error_and_is_not_printed(self) -> None:
        for args in (("unseal", UNSEAL_KEY), ("keyholder", "off", UNSEAL_KEY), ("keyholder",)):
            with self.subTest(args=args[:2]):
                result = self.host.run(*args)

                self.assertEqual(2, result.returncode, self.output(result))
                self.assertNotIn(UNSEAL_KEY, self.output(result))
                self.assertIn("never", result.stderr)
        self.assertTrue(self.host.state()["openbao"]["sealed"])


class FakeKubectlTests(unittest.TestCase):
    """The fake kubectl runs with the Python of the tests, also in CI."""

    def test_the_stub_keeps_the_library_path_of_the_test_python(self) -> None:
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/made-up/python lib"}):
            host = ClusterHost()
        self.addCleanup(host.close)

        result = host.run("unseal")

        self.assertIn(
            "export LD_LIBRARY_PATH='/made-up/python lib'\n",
            host.host_path(RKE2_KUBECTL).read_text(),
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("LD_LIBRARY_PATH", host.environment())

    def test_without_a_library_path_the_stub_sets_none(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("LD_LIBRARY_PATH", None)
            host = ClusterHost()
        self.addCleanup(host.close)

        self.assertNotIn("LD_LIBRARY_PATH", host.host_path(RKE2_KUBECTL).read_text())
        self.assertEqual(0, host.run("unseal").returncode)


class TheKeyIsNeverShownTests(ClusterTestCase):
    def test_the_key_never_appears_in_the_process_list_the_output_or_a_file(self) -> None:
        outputs = []
        for args in (("keyholder", "on"), ("unseal",), ("keyholder", "off")):
            result = self.host.run(*args, stdin=UNSEAL_KEY + "\n")
            self.assertEqual(0, result.returncode, self.output(result))
            outputs.append(self.output(result))

        self.assertFalse(self.host.state()["openbao"]["sealed"])
        # calls.log holds the arguments of every command meho-host started.
        self.assertNotIn(UNSEAL_KEY, self.host.calls())
        self.assertNotIn(UNSEAL_KEY, "".join(outputs))
        for command in ("logger", "systemd-cat", "journalctl"):
            self.assertNotIn(f"\n{command} ", "\n" + self.host.calls())
        for folder, _, names in os.walk(self.host.root):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, "rb") as file:
                    self.assertNotIn(UNSEAL_KEY.encode(), file.read(), path)
        self.assertEqual(
            [
                "exec -i meho-vault-0 -c bao -- bao write sys/unseal key=-",
                "create -f -",
            ],
            self.host.state()["key_inputs"],
        )


if __name__ == "__main__":
    unittest.main()
