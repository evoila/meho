#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of storage/local-path-storage.yaml, the storage manifest of form 2.

It is Rancher's local-path provisioner v0.0.37 manifest with five changes
only: the class is the default one, the volumes live in /var/lib/meho/volumes,
both images are pinned by digest, the provisioner has CPU and memory bounds,
and the provisioner cannot gain more rights than it starts with.

fixtures/local-path-storage-v0.0.37.yaml is the upstream file, unchanged.
The CI job compares it with upstream, and anyone can check it by hand:

    curl -fsSL "$UPSTREAM_URL" | sha256sum

with UPSTREAM_URL set to
https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
"""

from __future__ import annotations

import difflib
import hashlib
import unittest
from pathlib import Path

from harness import LINUX_SERVER

OURS = LINUX_SERVER / "storage" / "local-path-storage.yaml"
UPSTREAM = Path(__file__).resolve().parent / "fixtures" / "local-path-storage-v0.0.37.yaml"
UPSTREAM_SHA256 = "9781b39c24f3f651bd6d6e41b561e04e4904bbdb6d4f8c7a6009df3a702dcd65"

PROVISIONER_DIGEST = "sha256:e757967a5ec338f6a9b371c5a9688bedaa8c3578ea3dd4db329ea0084be0a86f"
BUSYBOX_DIGEST = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"

# Each change as (upstream lines, our lines). An insertion has no upstream lines.
EXPECTED_CHANGES = [
    # Both images pinned by digest: the provisioner keeps its tag.
    (
        ["          image: docker.io/rancher/local-path-provisioner:v0.0.37"],
        [f"          image: docker.io/rancher/local-path-provisioner:v0.0.37@{PROVISIONER_DIGEST}"],
    ),
    # Resource bounds use the example in the upstream v0.0.37 chart values.
    (
        [],
        [
            "          resources:",
            "            requests:",
            "              cpu: 100m",
            "              memory: 128Mi",
            "            limits:",
            "              cpu: 100m",
            "              memory: 128Mi",
        ],
    ),
    # The provisioner cannot gain more rights than it starts with.
    ([], ["          securityContext:", "            allowPrivilegeEscalation: false"]),
    # The class is the default storage class.
    ([], ["  annotations:", '    storageclass.kubernetes.io/is-default-class: "true"']),
    # The volumes live in /var/lib/meho/volumes.
    (
        ['                    "paths":["/opt/local-path-provisioner"]'],
        ['                    "paths":["/var/lib/meho/volumes"]'],
    ),
    # Upstream names the helper image with no tag, so it is pinned to busybox 1.37.
    (
        ["        image: docker.io/library/busybox"],
        [f"        image: docker.io/library/busybox:1.37@{BUSYBOX_DIGEST}"],
    ),
]


class StorageManifestTests(unittest.TestCase):
    def test_fixture_is_the_upstream_v0_0_37_file(self) -> None:
        self.assertEqual(UPSTREAM_SHA256, hashlib.sha256(UPSTREAM.read_bytes()).hexdigest())

    def test_only_the_five_changes_differ_from_upstream(self) -> None:
        upstream = UPSTREAM.read_text().splitlines()
        ours = OURS.read_text().splitlines()

        matcher = difflib.SequenceMatcher(a=upstream, b=ours, autojunk=False)
        changes = [
            (upstream[i1:i2], ours[j1:j2])
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]

        self.assertEqual(EXPECTED_CHANGES, changes)

    def test_the_default_annotation_belongs_to_the_storage_class(self) -> None:
        ours = OURS.read_text().splitlines()

        at = ours.index("  annotations:")

        self.assertEqual(
            ["kind: StorageClass", "metadata:", "  name: local-path"], ours[at - 3 : at]
        )


if __name__ == "__main__":
    unittest.main()
