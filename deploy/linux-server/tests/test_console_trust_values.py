#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""The meho pod of form 2 can trust the CA that `meho-host certificate`
writes (#6698): values-single-node.yaml mounts the optional ConfigMap
meho-console-trust, without subPath, and CONSOLE_CA_FILE names its file.
helm template with the two values files of install_core, as in
test_egress_values.py. The test reads the text: the CI job has no PyYAML."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

from harness import LINUX_SERVER, InstallTestCase
from test_core_install import CHART_DIR, PLACE_VALUES

# toYaml writes the keys of each item in name order. The volume is the last
# line of the Deployment, so it has no line end after the split on "---".
VOLUME = (
    "        - configMap:\n"
    "            name: meho-console-trust\n"
    "            optional: true\n"
    "          name: console-trust"
)
MOUNT = (
    "            - mountPath: /var/run/meho/console-trust\n"
    "              name: console-trust\n"
    "              readOnly: true\n"
)
SETTING = (
    "            - name: CONSOLE_CA_FILE\n"
    "              value: /var/run/meho/console-trust/ca.crt\n"
)


@unittest.skipUnless(
    shutil.which("helm") or os.environ.get("MEHO_REQUIRE_HELM") == "1",
    "helm is not on PATH; set MEHO_REQUIRE_HELM=1 to fail instead",
)
class ConsoleTrustRenderTests(InstallTestCase):
    def deployment_of_meho(self) -> str:
        """The Deployment of the meho pod, from helm template with the two
        values files of install_core."""
        self.assert_succeeds(self.machine.run())
        result = subprocess.run(
            [
                "helm",
                "template",
                "meho",
                str(CHART_DIR),
                "--namespace",
                "meho",
                "--set",
                "image.tag=test",
                "-f",
                str(LINUX_SERVER / "values-single-node.yaml"),
                "-f",
                str(self.machine.host_path(PLACE_VALUES)),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        deployments = [
            document
            for document in result.stdout.split("\n---\n")
            if "kind: Deployment\n" in document and "\n  name: meho\n" in document
        ]
        self.assertEqual(1, len(deployments), result.stdout)
        return deployments[0]

    def test_the_meho_pod_mounts_the_ca_that_meho_host_certificate_writes(self) -> None:
        deployment = self.deployment_of_meho()

        self.assertEqual(1, deployment.count(VOLUME), deployment)
        # The backplane container and the migrate init container.
        self.assertEqual(2, deployment.count(MOUNT), deployment)
        self.assertEqual(2, deployment.count(SETTING), deployment)
        self.assertNotIn("subPath", deployment)


if __name__ == "__main__":
    unittest.main()
