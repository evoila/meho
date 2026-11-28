# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give the k3s test node the images its test pods need.

The k3s node runs inside a test container and has its own image store. It
does not pull through the CI image proxy, so a pod there cannot get its
images. Every pod also needs the pause image, which holds the pod's network
before its containers start. Without it, a pod stays in ``ContainerCreating``.

So the Docker daemon of the tests pulls these images (in CI through the
proxy), and :func:`load_into_node` copies them into the node's image store.
The node then starts the pods with no pull of its own.
"""

from __future__ import annotations

import io
import os
import tarfile
import warnings
from typing import Any

import docker

_DEFAULT_PAUSE_IMAGE = "rancher/mirrored-pause:3.6"


def pause_image() -> str:
    """The pause image of the k3s node: ``MEHO_TEST_K3S_PAUSE_IMAGE``, else the public one."""
    return os.environ.get("MEHO_TEST_K3S_PAUSE_IMAGE") or _DEFAULT_PAUSE_IMAGE


def _image_archive(client: docker.DockerClient, image: str, name: str) -> bytes:
    """A tar archive that holds the saved *image* as the file *name*."""
    client.images.pull(image)
    saved = b"".join(client.api.get_image(image))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(saved)
        tar.addfile(info, io.BytesIO(saved))
    return archive.getvalue()


def load_into_node(container: Any, images: list[str]) -> None:
    """Copy each of *images* into the image store of the k3s node *container*.

    *container* is a started ``K3SContainer``. A failure gives a warning and
    does not stop the tests: the pods that need the image then wait, and
    their tests skip with the pod's events.
    """
    client = container.get_docker_client().client
    node = container.get_wrapped_container()
    for index, image in enumerate(images):
        name = f"meho-node-image-{index}.tar"
        try:
            node.put_archive("/", _image_archive(client, image, name))
            result = node.exec_run(["k3s", "ctr", "-n", "k8s.io", "images", "import", f"/{name}"])
            node.exec_run(["rm", "-f", f"/{name}"])
        except docker.errors.DockerException as exc:
            warnings.warn(f"k3s node image copy failed ({type(exc).__name__})", stacklevel=2)
            continue
        if result.exit_code != 0:
            output = result.output.decode(errors="replace")[-300:]
            warnings.warn(f"k3s node image import failed: {output}", stacklevel=2)
