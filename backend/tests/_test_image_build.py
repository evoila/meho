# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build a test image from a base image that is already on the runner.

In CI the step "Pull the test images" pulls each base image before pytest
starts (``scripts/ci/pull_test_images.sh``). The build here then uses that
copy: it asks for no pull (``pull=False``), so Docker takes the base image
from the runner and does not ask a registry again.

A failed build raises an error that names only the image tag. Docker's own
build output stays out of it, because the logs of this repository are
public and that output can name a host.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def build_test_image(dockerfile: str, tag: str, buildargs: dict[str, str]) -> None:
    """Build *dockerfile* as *tag*, with the base image that is on the runner."""
    import docker.errors
    from testcontainers.core.image import DockerImage

    with tempfile.TemporaryDirectory() as build_dir:
        (Path(build_dir) / "Dockerfile").write_text(dockerfile)
        image = DockerImage(path=build_dir, tag=tag, buildargs=buildargs, pull=False)
        try:
            image.build()
        except docker.errors.DockerException as exc:
            raise RuntimeError(
                f"The test image {tag} did not build ({type(exc).__name__})."
            ) from None
