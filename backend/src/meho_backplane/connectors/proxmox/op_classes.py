# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The marks the Proxmox connector declares for its own operations.

See :mod:`meho_backplane.op_classes`.
"""

from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.op_classes import declare_credential_capable, declare_credential_check

# The raw write can reach any path of the Proxmox API, also the ones that
# set a password or make an API token. So every call waits for a person,
# and no standing grant, agent grant or run approval covers it.
declare_credential_capable(("proxmox.api.write",))

#: The guest agent reads of a QEMU VM whose answer holds no file content, no
#: command output and no secret: ``nodes/<node>/qemu/<vmid>/agent/<read>``.
#: Every other read under ``agent`` waits for a person, for example
#: ``file-read`` (a file inside the guest) and ``exec-status`` (the output
#: of a command that ran in the guest).
PLAIN_AGENT_READS: Final[frozenset[str]] = frozenset(
    {
        "get-fsinfo",
        "get-host-name",
        "get-memory-block-info",
        "get-memory-blocks",
        "get-osinfo",
        "get-time",
        "get-timezone",
        "get-users",
        "get-vcpus",
        "info",
        "network-get-interfaces",
    }
)

#: A path with one of these segments opens a console or a tunnel into a
#: guest or a node.
_CONSOLE_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"mtunnelwebsocket", "spiceproxy", "termproxy", "vncproxy", "vncwebsocket"}
)

#: Paths, as segments, whose answer can hold a secret value: the API data of
#: an ACME DNS plugin, the secrets and headers of a webhook, the login of an
#: IPAM, DNS or metric server, the keys of an auth realm and the
#: subscription key. ``*`` is one segment of any value. A path that starts
#: with one of them waits.
_SECRET_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("access", "domains", "*"),
    ("cluster", "acme", "plugins"),
    ("cluster", "metrics", "server"),
    ("cluster", "notifications", "endpoints", "webhook"),
    ("cluster", "sdn", "dns"),
    ("cluster", "sdn", "ipams"),
    ("nodes", "*", "subscription"),
)


def _starts_with(segments: list[str], prefix: tuple[str, ...]) -> bool:
    if len(segments) < len(prefix):
        return False
    return all(want in ("*", got) for want, got in zip(prefix, segments, strict=False))


def _reads_from_guest(segments: list[str]) -> bool:
    """True for a read inside a guest: a guest agent read, cloud-init or a file restore."""
    if _starts_with(segments, ("nodes", "*", "qemu", "*", "agent")):
        return len(segments) > 6 or (len(segments) == 6 and segments[5] not in PLAIN_AGENT_READS)
    if _starts_with(segments, ("nodes", "*", "qemu", "*", "cloudinit")):
        return True
    return _starts_with(segments, ("nodes", "*", "storage", "*", "file-restore"))


def path_reads_guest_or_secret(params: Mapping[str, Any]) -> bool:
    """True when the ``proxmox.api.get`` call in *params* can return a secret.

    That is a read from inside a guest (a guest agent read other than
    :data:`PLAIN_AGENT_READS`, the cloud-init data of a VM, a file from a
    backup), a console or tunnel path, or a path of :data:`_SECRET_PATHS`.
    The path is compared in lower case. A path MEHO cannot read counts too:
    it is not text, or it has an empty or a ``.`` segment.
    """
    path = params.get("path")
    if not isinstance(path, str) or not path:
        return True
    segments = path.lower().split("/")
    if segments[-1] == "":
        segments.pop()
    if any(segment in ("", ".") for segment in segments):
        return True
    if any(segment in _CONSOLE_SEGMENTS for segment in segments):
        return True
    if _reads_from_guest(segments):
        return True
    return any(_starts_with(segments, prefix) for prefix in _SECRET_PATHS)


# A read inside a guest or of a stored secret is a credential read: such a
# call waits for a person on every call. The plain reads (nodes, VM status
# and config, storage, tasks) keep the rules of proxmox.api.get.
declare_credential_check("proxmox.api.get", path_reads_guest_or_secret)
