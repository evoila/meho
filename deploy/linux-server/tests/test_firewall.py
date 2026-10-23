#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the MEHO host firewall (lib/firewall.sh).

They cover the admin network check of step 1, the files that step 2 writes,
the order in which step 2 turns the firewall on and other firewalls off,
the port list that the release test of form 2 reads, and the redirect of
port 80. test_firewall_nft.py checks the rules file with the real nft.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

from harness import LINUX_SERVER, InstallTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES = "/etc/meho/firewall.nft"
UNIT = "/etc/systemd/system/meho-firewall.service"
DROPIN = "/etc/systemd/system/rke2-server.service.d/meho-firewall.conf"
TRAEFIK = "/var/lib/rancher/rke2/server/manifests/rke2-traefik-config.yaml"

FIREWALL_CHECK = "nft -c -f"
FIREWALL_START = "systemctl restart meho-firewall.service"
FIREWALL_VERIFY = "nft list table inet meho"
FIREWALLD_OFF = "systemctl disable --now firewalld.service"
RKE2_START = "systemctl enable --now --no-block rke2-server.service"

# An admin at 10.0.50.9, inside the admin network 10.0.50.0/24.
ADMIN_SESSION = "10.0.50.9 50022 10.0.0.5 22"
ADMIN_NETWORK = ("--admin-network", "10.0.50.0/24")

LIBS = ("common", "net", "checks", "firewall")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def admin_network_set(rules: str) -> str:
    """The set admin_network of a rules file, from "set" to its closing brace."""
    start = rules.index("    set admin_network {")
    return rules[start : rules.index("    }\n", start) + 6]


def chain_rules(rules: str, chain: str) -> list[str]:
    """The lines of a chain without comments and blank lines, the type line first."""
    start = rules.index(f"    chain {chain} {{\n")
    body = rules[start : rules.index("\n    }\n", start)].splitlines()[1:]
    return [line.strip() for line in body if line.strip() and not line.strip().startswith("#")]


class FirewallTestCase(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.machine.ssh_connection = ADMIN_SESSION

    def host_text(self, path: str) -> str:
        return self.machine.host_path(path).read_text()

    def call_lines(self) -> list[str]:
        return self.machine.calls().splitlines()

    def index_of(self, prefix: str) -> int:
        calls = self.call_lines()
        for index, line in enumerate(calls):
            if line.startswith(prefix):
                return index
        self.fail(f"no call starts with {prefix!r}:\n" + "\n".join(calls))

    def run_lib(self, snippet: str) -> subprocess.CompletedProcess[str]:
        """Run bash with the files of lib/ sourced, on the fake machine."""
        sources = "".join(f'source "{LINUX_SERVER}/lib/{name}.sh"\n' for name in LIBS)
        env = {
            "PATH": f"{self.machine.stubs}:{self.machine.tools}",
            "MEHO_TEST_ROOT": str(self.machine.root),
            "MEHO_FAKE": str(self.machine.fake),
            "LC_ALL": "C",
        }
        return subprocess.run(
            [str(self.machine.tools / "bash"), "-c", "set -euo pipefail\n" + sources + snippet],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=30,
        )


class RulesFileTests(FirewallTestCase):
    def test_the_rules_file_is_the_one_readable_file_of_the_design(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        expected = (FIXTURES / "firewall-admin-network.nft").read_text()
        self.assertEqual(expected, self.host_text(RULES))
        self.assertEqual(0o600, mode(self.machine.host_path(RULES)))

    def test_the_file_changes_only_the_meho_table(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        commands = [
            line
            for line in self.host_text(RULES).splitlines()
            if line and not line.startswith((" ", "#", "}"))
        ]
        self.assertEqual(
            ["table inet meho", "delete table inet meho", "table inet meho {"], commands
        )
        for line in self.host_text(RULES).splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotIn("flush", line)

    def test_the_chain_sees_the_packet_before_the_port_forwarding(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        rules = self.host_text(RULES)
        # mangle is priority -150, before dstnat (-100), where RKE2's host
        # port for Traefik rewrites the destination.
        self.assertIn("type filter hook prerouting priority mangle; policy drop;", rules)
        self.assertIn("fib daddr type local tcp dport { 80, 443 } accept", rules)
        self.assertIn("fib daddr type local ip saddr @admin_network tcp dport 22 accept", rules)

    def test_the_cluster_interfaces_pass_before_any_other_rule(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        rules = self.host_text(RULES).splitlines()
        policy = rules.index("        type filter hook prerouting priority mangle; policy drop;")
        accepted = [line.strip() for line in rules[policy + 1 : policy + 6] if line.strip()]
        self.assertEqual(
            [
                "# The machine itself and its pods.",
                'iifname "lo" accept',
                'iifname "cali*" accept',
                'iifname "flannel*" accept',
            ],
            accepted,
        )

    def test_custom_pod_and_service_ranges_are_dropped_from_network_cards(self) -> None:
        result = self.machine.run(
            *ADMIN_NETWORK, "--cluster-cidr", "172.30.0.0/16", "--service-cidr", "172.31.0.0/20"
        )

        self.assert_succeeds(result)
        self.assertIn("ip saddr { 172.30.0.0/16, 172.31.0.0/20 } drop", self.host_text(RULES))

    def test_the_appliance_chains_let_every_packet_pass_on_form_2(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        rules = self.host_text(RULES)
        for hook in ("input", "forward", "output"):
            with self.subTest(chain=hook):
                lines = chain_rules(rules, hook)
                self.assertEqual(
                    f"type filter hook {hook} priority filter; policy accept;", lines[0]
                )
                self.assertEqual([], [line for line in lines[1:] if "drop" in line])
                self.assertEqual("counter", lines[-1])

    def test_the_input_chain_accepts_what_the_chain_prerouting_accepts(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        rules = self.host_text(RULES)
        prerouting = [
            line.removeprefix("fib daddr type local ")
            for line in chain_rules(rules, "prerouting")
            if line.endswith(" accept")
        ]
        accepted = [line for line in chain_rules(rules, "input") if line.endswith(" accept")]
        self.assertEqual(prerouting, accepted)
        self.assertNotIn("fib", "\n".join(accepted))

    def test_the_forward_and_output_chains_accept_the_pod_and_service_ranges(self) -> None:
        result = self.machine.run(
            *ADMIN_NETWORK, "--cluster-cidr", "172.30.0.0/16", "--service-cidr", "172.31.0.0/20"
        )

        self.assert_succeeds(result)
        rules = self.host_text(RULES)
        ranges = "{ 172.30.0.0/16, 172.31.0.0/20 }"
        self.assertIn(f"ip saddr {ranges} accept", chain_rules(rules, "forward"))
        self.assertIn(f"ip daddr {ranges} accept", chain_rules(rules, "forward"))
        self.assertIn("ct status dnat accept", chain_rules(rules, "forward"))
        self.assertIn(f"ip daddr {ranges} accept", chain_rules(rules, "output"))

    def test_the_admin_network_set_for_each_choice(self) -> None:
        cases = {
            "range": (
                ADMIN_NETWORK,
                ADMIN_SESSION,
                "Given with --admin-network at the install.",
                "elements = { 10.0.50.0/24 }",
            ),
            "one address": (
                ("--admin-network", "10.0.50.9"),
                ADMIN_SESSION,
                "Given with --admin-network at the install.",
                "elements = { 10.0.50.9 }",
            ),
            "the SSH session": (
                (),
                ADMIN_SESSION,
                "Only the address of the SSH session that ran install.sh",
                "elements = { 10.0.50.9 }",
            ),
            "none": (
                ("--admin-network", "none"),
                None,
                "Empty: install.sh ran with --admin-network none",
                None,
            ),
        }
        for case, (args, session, note, elements) in cases.items():
            with self.subTest(case=case):
                self.machine.ssh_connection = session

                self.assert_succeeds(self.machine.run(*args))

                rules = self.host_text(RULES)
                self.assertIn(f"    # {note}", rules)
                admin_set = admin_network_set(rules)
                if elements is None:
                    self.assertNotIn("elements", admin_set)
                else:
                    self.assertIn(f"        {elements}\n", admin_set)


class BootUnitTests(FirewallTestCase):
    def test_the_unit_loads_the_file_before_the_network_and_rke2(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        unit = self.host_text(UNIT).splitlines()
        nft = self.machine.stubs / "nft"
        for line in (
            "Wants=network-pre.target",
            "Before=network-pre.target shutdown.target rke2-server.service",
            "After=nftables.service",
            "DefaultDependencies=no",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"ExecStart={nft} -f /etc/meho/firewall.nft",
            f"ExecReload={nft} -f /etc/meho/firewall.nft",
            "WantedBy=sysinit.target",
        ):
            with self.subTest(line=line):
                self.assertIn(line, unit)
        self.assertFalse(
            [line for line in unit if line.startswith("ExecStop")],
            "stopping the unit must never remove the rules",
        )
        self.assertEqual(0o644, mode(self.machine.host_path(UNIT)))

    def test_rke2_starts_only_while_the_meho_table_is_loaded(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        dropin = self.host_text(DROPIN).splitlines()
        nft = self.machine.stubs / "nft"
        for line in (
            "Wants=meho-firewall.service",
            "After=meho-firewall.service",
            f"ExecStartPre=/bin/sh -c '{nft} list table inet meho >/dev/null'",
        ):
            with self.subTest(line=line):
                self.assertIn(line, dropin)


class FirewallStartTests(FirewallTestCase):
    def test_step_2_checks_and_loads_the_firewall_before_rke2(self) -> None:
        result = self.machine.run(*ADMIN_NETWORK)

        self.assert_succeeds(result)
        order = [
            FIREWALL_CHECK,
            "systemctl daemon-reload",
            "systemctl enable meho-firewall.service",
            FIREWALL_START,
            FIREWALL_VERIFY,
            "rke2-installer",
            RKE2_START,
        ]
        indexes = [self.index_of(call) for call in order]
        self.assertEqual(sorted(indexes), indexes)
        self.assertIn(f"nft -c -f {self.machine.host_path(RULES)}", self.call_lines())
        self.assertIn(
            "[ok]   the MEHO firewall is on: from another machine, only TCP 80 and 443 answer,"
            " and SSH from 10.0.50.0/24",
            result.stdout,
        )

    def test_a_file_that_nft_refuses_stops_before_any_rule_is_loaded(self) -> None:
        self.machine.answer("active_units", "systemd-resolved\nfirewalld.service\n")
        self.machine.answer("nft_check_rc", "1")
        self.machine.answer("nft_check_out", "Error: Could not process rule: No such file\n")

        result = self.machine.run(*ADMIN_NETWORK)

        self.assert_fails_once(result, "nft does not accept the MEHO firewall file")
        self.assertIn("Error: Could not process rule: No such file", result.stderr)
        for call in (FIREWALL_START, FIREWALLD_OFF, RKE2_START):
            with self.subTest(call=call):
                self.assertNotIn(call, self.machine.calls())

    def test_a_failed_load_stops_with_the_log_command(self) -> None:
        self.machine.answer("systemctl_restart_rc", "1")

        self.assert_fails_once(
            self.machine.run(*ADMIN_NETWORK),
            "could not load the MEHO firewall",
            "journalctl -u meho-firewall.service",
        )
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_a_table_that_is_not_loaded_after_the_start_keeps_firewalld_on(self) -> None:
        self.machine.answer("active_units", "systemd-resolved\nfirewalld.service\n")
        self.machine.answer("nft_table_rc", "1")

        self.assert_fails_once(
            self.machine.run(*ADMIN_NETWORK),
            "The MEHO firewall is not on after its start (the table inet meho is not loaded).",
        )
        self.assertNotIn(FIREWALLD_OFF, self.machine.calls())

    def test_firewalld_is_turned_off_only_after_the_meho_firewall_is_on(self) -> None:
        for units in ("active_units", "enabled_units"):
            with self.subTest(firewalld=units):
                self.machine.answer(units, f"{self.machine_default(units)}firewalld.service\n")
                (self.machine.fake / "calls.log").write_text("")

                result = self.machine.run(*ADMIN_NETWORK)

                self.assert_succeeds(result)
                self.assertLess(self.index_of(FIREWALL_VERIFY), self.index_of(FIREWALLD_OFF))
                self.assertLess(self.index_of(FIREWALLD_OFF), self.index_of(RKE2_START))
                self.assertIn("[ok]   turned firewalld off", result.stdout)
                self.machine.answer(units, self.machine_default(units))

    def test_an_active_ufw_is_turned_off_after_the_meho_firewall_is_on(self) -> None:
        self.machine.add_stub("ufw", 'case "$1" in status) echo "Status: active" ;; esac')

        result = self.machine.run(*ADMIN_NETWORK)

        self.assert_succeeds(result)
        self.assertLess(self.index_of(FIREWALL_VERIFY), self.index_of("ufw disable"))
        self.assertIn("[ok]   turned ufw off", result.stdout)

    def test_an_inactive_ufw_and_a_missing_firewalld_are_left_alone(self) -> None:
        self.machine.add_stub("ufw", 'case "$1" in status) echo "Status: inactive" ;; esac')

        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        self.assertNotIn("ufw disable", self.machine.calls())
        self.assertNotIn("systemctl disable", self.machine.calls())

    def test_a_machine_without_nft_stops_in_step_1(self) -> None:
        (self.machine.stubs / "nft").unlink()

        self.assert_fails_once(
            self.machine.run(*ADMIN_NETWORK), "nft, the nftables tool, is not on this machine."
        )
        self.assertEqual([], self.written_files())

    def machine_default(self, name: str) -> str:
        return {"active_units": "systemd-resolved\n", "enabled_units": "meho-firewall.service\n"}[
            name
        ]


class AdminNetworkCheckTests(FirewallTestCase):
    def assert_stopped_in_step_1(
        self, result: subprocess.CompletedProcess[str], *text: str
    ) -> None:
        self.assert_fails_once(result, *text)
        self.assertEqual([], self.written_files())
        self.assertNotIn("nft -c", self.machine.calls())

    def test_a_session_inside_the_admin_network_passes(self) -> None:
        result = self.machine.run(*ADMIN_NETWORK)

        self.assert_succeeds(result)
        self.assertIn(
            "[ok]   SSH is allowed from 10.0.50.0/24, which holds the address of this session"
            " (10.0.50.9)",
            result.stdout,
        )

    def test_a_session_outside_the_admin_network_stops_before_any_change(self) -> None:
        self.machine.ssh_connection = "10.0.0.9 50022 10.0.0.5 22"

        self.assert_stopped_in_step_1(
            self.machine.run(*ADMIN_NETWORK),
            "This SSH session comes from 10.0.0.9, which is outside --admin-network 10.0.50.0/24.",
            "pass a range that holds 10.0.0.9",
        )

    def test_without_the_option_only_the_session_address_may_use_ssh(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn(
            "[WARN] No --admin-network was given, so SSH is allowed only from 10.0.50.9, the"
            " address of this SSH session. To allow a range, change the set admin_network in"
            " /etc/meho/firewall.nft, then run: systemctl reload meho-firewall.service",
            result.stderr,
        )

    def test_without_the_option_on_the_console_the_install_stops(self) -> None:
        self.machine.ssh_connection = None

        self.assert_stopped_in_step_1(
            self.machine.run(),
            "--admin-network is missing, and install.sh does not run in an SSH session",
            "--admin-network 10.0.50.0/24, or --admin-network none",
        )

    def test_on_the_console_a_range_or_none_passes(self) -> None:
        self.machine.ssh_connection = None
        cases = {
            "10.0.50.0/24": "[ok]   SSH is allowed from 10.0.50.0/24",
            "none": "[ok]   SSH is closed for every address (--admin-network none)",
        }
        for value, line in cases.items():
            with self.subTest(admin_network=value):
                result = self.machine.run("--admin-network", value)

                self.assert_succeeds(result)
                self.assertIn(line, result.stdout)

    def test_none_over_ssh_warns_that_no_new_login_works(self) -> None:
        result = self.machine.run("--admin-network", "none")

        self.assert_succeeds(result)
        self.assertIn("[WARN] --admin-network none closes SSH for every address.", result.stderr)

    def test_an_ipv6_session(self) -> None:
        self.machine.ssh_connection = "2001:db8::9 50022 2001:db8::5 22"

        self.assert_stopped_in_step_1(
            self.machine.run(), "this SSH session comes from 2001:db8::9, which is not an IPv4"
        )

        result = self.machine.run(*ADMIN_NETWORK)
        self.assert_succeeds(result)
        self.assertIn("[WARN] This SSH session comes from 2001:db8::9", result.stderr)

    def test_an_ipv4_mapped_session_address_counts_as_ipv4(self) -> None:
        self.machine.ssh_connection = "::ffff:10.0.50.9 50022 ::ffff:10.0.0.5 22"

        result = self.machine.run(*ADMIN_NETWORK)

        self.assert_succeeds(result)
        self.assertIn("holds the address of this session (10.0.50.9)", result.stdout)

    def test_sshd_on_another_port_stops_unless_ssh_is_closed(self) -> None:
        self.machine.ssh_connection = "10.0.50.9 50022 10.0.0.5 2222"

        self.assert_stopped_in_step_1(
            self.machine.run(*ADMIN_NETWORK),
            "This SSH session reached sshd on port 2222.",
            "'Port 22' line in /etc/ssh/sshd_config",
        )

        self.assert_succeeds(self.machine.run("--admin-network", "none"))

    def test_every_address_as_admin_network_warns(self) -> None:
        result = self.machine.run("--admin-network", "0.0.0.0/0")

        self.assert_succeeds(result)
        self.assertIn(
            "[WARN] --admin-network 0.0.0.0/0 allows SSH from every address.", result.stderr
        )

    def test_the_join_options_warn_that_the_firewall_blocks_a_join(self) -> None:
        token_file = self.machine.home / "join-token"
        token_file.write_text("K10test-token\n")

        joined = self.machine.run(
            *ADMIN_NETWORK, "--join-address", "10.0.0.20", "--join-token-file", str(token_file)
        )
        alone = self.machine.run(*ADMIN_NETWORK)

        self.assert_succeeds(joined)
        warning = (
            "[WARN] --join-address is given, but the MEHO firewall drops RKE2's ports when they"
            " come from another machine. The join works only after both machines accept the"
            " other node's address: add accept rules to the chain prerouting in"
            " /etc/meho/firewall.nft on each machine, then run:"
            " systemctl reload meho-firewall.service"
        )
        self.assertIn(warning, joined.stderr)
        self.assert_succeeds(alone)
        self.assertNotIn("--join-address", alone.stderr)

    def test_under_sudo_the_session_is_found_in_a_parent_process(self) -> None:
        # sudo resets the environment, so install.sh itself has no
        # SSH_CONNECTION. Its parent process (this test) and the processes
        # above it are read from /proc, which the fake root holds.
        self.machine.ssh_connection = None
        parent = os.getpid()
        self.machine.host_file(f"/proc/{parent}/environ", "HOME=/root\0TERM=xterm\0")
        self.machine.host_file(f"/proc/{parent}/status", "Name:\tsudo\nPPid:\t4242\n")
        self.machine.host_file(
            "/proc/4242/environ", "USER=admin\0SSH_CONNECTION=10.0.50.7 40000 10.0.0.5 22\0"
        )

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("SSH is allowed only from 10.0.50.7", result.stderr)
        self.assertIn("elements = { 10.0.50.7 }", self.host_text(RULES))


class PortListTests(FirewallTestCase):
    def test_the_port_list_that_the_release_test_reads(self) -> None:
        result = self.run_lib("firewall_port_list")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            textwrap.dedent("""\
                80/tcp open
                443/tcp open
                22/tcp admin-network
                6443/tcp closed
                9345/tcp closed
                10250/tcp closed
                2379/tcp closed
                2380/tcp closed
                2381/tcp closed
                9099/tcp closed
                30000-32767/tcp closed
                8472/udp closed
                """),
            result.stdout,
        )

    def test_every_port_that_rke2_needs_is_in_the_port_list(self) -> None:
        result = self.run_lib(
            'printf "%s/tcp\\n" "${REQUIRED_TCP_PORTS[@]}"\n'
            'printf "%s/udp\\n" "${REQUIRED_UDP_PORTS[@]}"'
        )
        listed = self.run_lib("firewall_port_list | awk '{ print $1 }'")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertLessEqual(set(result.stdout.split()), set(listed.stdout.split()))

    def test_the_rules_open_exactly_the_open_ports_of_the_list(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        # The chain prerouting decides what answers from another machine. The
        # chain input repeats its accept rules (see RulesFileTests).
        tcp_rules = [
            line for line in chain_rules(self.host_text(RULES), "prerouting") if "tcp dport" in line
        ]
        self.assertEqual(
            [
                "fib daddr type local tcp dport { 80, 443 } accept",
                "fib daddr type local ip saddr @admin_network tcp dport 22 accept",
            ],
            tcp_rules,
        )

    def test_firewall_state_reports_on_and_off(self) -> None:
        cases = {
            "on": ("0", "meho-firewall.service\n", 0, "on"),
            "table missing": (
                "1",
                "meho-firewall.service\n",
                1,
                "off: the table inet meho is not loaded",
            ),
            "unit disabled": (
                "0",
                "",
                1,
                "off: meho-firewall.service is not enabled, so the next start of the machine"
                " loads no firewall",
            ),
        }
        for case, (table_rc, enabled, returncode, line) in cases.items():
            with self.subTest(case=case):
                self.machine.answer("nft_table_rc", table_rc)
                self.machine.answer("enabled_units", enabled)

                result = self.run_lib("firewall_state")

                self.assertEqual(returncode, result.returncode, result.stderr)
                self.assertEqual(line + "\n", result.stdout)


class TraefikRedirectTests(FirewallTestCase):
    def test_port_80_answers_only_with_a_redirect_to_443(self) -> None:
        self.assert_succeeds(self.machine.run(*ADMIN_NETWORK))

        self.assertEqual(
            textwrap.dedent("""\
                # Written by MEHO install.sh. Port 80 answers only with a redirect to 443.
                apiVersion: helm.cattle.io/v1
                kind: HelmChartConfig
                metadata:
                  name: rke2-traefik
                  namespace: kube-system
                spec:
                  valuesContent: |-
                    ports:
                      web:
                        http:
                          redirections:
                            entryPoint:
                              to: websecure
                              scheme: https
                              permanent: true
                """),
            self.host_text(TRAEFIK),
        )
        self.assertEqual(0o600, mode(self.machine.host_path(TRAEFIK)))


if __name__ == "__main__":
    unittest.main()
