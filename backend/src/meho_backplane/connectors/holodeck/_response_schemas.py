# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas for the 18 Holodeck operations, keyed by op id.

Each schema names the fields the handler really returns (see the handlers
in ``ops.py``, ``ops_read.py``, ``ops_write.py``, ``ops_deploy.py`` and
``ops_remove.py``).
Holodeck has no published API file. Where a field is a cmdlet answer passed
through as is (``Get-HoloDeckConfig``, ``Get-HoloDeckPod``), the schema keeps
that object open and names only the keys MEHO itself reads. The schemas are
informational: the dispatcher does not check answers against them.

Kept in its own module so the op tables stay readable.
"""

from __future__ import annotations

from typing import Any

__all__ = ["HOLODECK_RESPONSE_SCHEMAS"]


def _f(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    """One response field with a type and a description."""
    return {"type": type_, "description": description, **extra}


def _obj(description: str, properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """An object field with named properties."""
    return {"type": "object", "description": description, "properties": properties, **extra}


def _envelope(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The top-level answer. Extra keys stay allowed, as before."""
    return {"type": "object", "properties": properties, "additionalProperties": True, **extra}


_ERROR = _f(["string", "null"], "Set when the command failed or a safety check refused the call.")
_STDOUT = _f("string", "Standard output of the command on the appliance.")
_STDERR = _f("string", "Standard error of the command, cut at 4096 characters.")
_EXIT = _f(["integer", "null"], "Exit code of the command; null when unknown.")
_TOTAL = _f("integer", "Number of rows.")
_OK = _f("boolean", "False when this sub-command failed or printed nothing.")
_COMMAND = _f("string", "The exact shell command MEHO ran, for the audit trail.")


_ABOUT = _envelope(
    {
        "vendor": _f("string", "Always 'vmware'."),
        "product": _f("string", "Always 'holodeck'."),
        "version": _f(["string", "null"], "Holodeck version from Get-HoloDeckConfig."),
        "build": _f(["string", "null"], "First line of /etc/photon-release."),
        "photon_version": _f(["string", "null"], "Photon OS release, for example '5.0'."),
        "pod_id": _f(["string", "null"], "Holodeck pod id, when the appliance has one."),
    },
    required=["vendor", "product"],
)

_CONFIG_SHOW = _envelope(
    {
        "config": _f(
            ["object", "null"],
            "The Get-HoloDeckConfig answer as the appliance returns it. MEHO reads "
            "'Version' (or 'HolodeckVersion') and 'PodId' (or 'PodID') from it; "
            "other keys pass through unchanged. Null when the cmdlet failed.",
            additionalProperties=True,
        ),
        "error": _ERROR,
    }
)

_POD_LIST = _envelope(
    {
        "rows": _f(
            "array",
            "One object per pod, as Get-HoloDeckPod returns it.",
            items={"type": "object", "additionalProperties": True},
        ),
        "total": _TOTAL,
        "error": _ERROR,
    }
)

_POD_INFO = _envelope(
    {
        "pod": _f(
            ["object", "null"],
            "The Get-HoloDeckPod -Id answer as the appliance returns it. Null on failure.",
            additionalProperties=True,
        ),
        "error": _ERROR,
    }
)

_SERVICE_LIST = _envelope(
    {
        "rows": _f(
            "array",
            "One row per service whose name starts with 'Holo'.",
            items=_obj(
                "One service.",
                {
                    "Name": _f("string", "Service name."),
                    "Status": _f(
                        ["string", "integer"],
                        "Service state, for example 'Running' (or 4) or 'Stopped' (or 1).",
                    ),
                    "DisplayName": _f("string", "Display name of the service."),
                },
            ),
        ),
        "total": _TOTAL,
        "error": _ERROR,
    }
)

_K8S_EXEC = _envelope({"stdout": _STDOUT, "stderr": _STDERR, "exit_status": _EXIT, "error": _ERROR})

_LOGS_TAIL = _envelope(
    {
        "files": _f(
            "array",
            "One entry per log file that matched.",
            items=_obj(
                "The tail of one log file.",
                {
                    "path": _f(["string", "null"], "File path; null when only one file matched."),
                    "lines": _f("string", "The last lines of the file, as one text."),
                },
            ),
        ),
        "raw": _f("string", "The full output of tail."),
        "lines_requested": _f("integer", "The line count asked for; 0 when the call was refused."),
        "error": _ERROR,
    }
)

_NETWORKING_SHOW = _envelope(
    {
        "bgp": _obj(
            "FRR BGP peer summary.",
            {"summary_text": _f("string", "Text of 'show bgp summary'."), "ok": _OK},
        ),
        "routes": _obj(
            "Routing table as FRR sees it.",
            {"text": _f("string", "Text of 'show ip route'."), "ok": _OK},
        ),
        "dns": _obj(
            "DNS zones from Get-DnsServerZone.",
            {
                "zones": _f(
                    "array",
                    "One object per zone.",
                    items=_obj(
                        "One DNS zone.",
                        {
                            "ZoneName": _f("string", "Zone name."),
                            "ZoneType": _f(["string", "integer"], "Zone type."),
                        },
                    ),
                ),
                "total": _f("integer", "Number of zones."),
                "ok": _f("boolean", "False when the DNS cmdlet failed."),
            },
        ),
        "dhcp": _obj(
            "DHCP leases.",
            {"leases_text": _f("string", "Raw text of the dhcpd.leases file."), "ok": _OK},
        ),
    }
)

_DISK_USAGE = _envelope(
    {
        "root_fs": _obj(
            "Root file system usage from 'df -B1 /'.",
            {
                "total_bytes": _f(["integer", "null"], "Size in bytes."),
                "used_bytes": _f(["integer", "null"], "Used bytes."),
                "avail_bytes": _f(["integer", "null"], "Free bytes."),
                "percent_used": _f(["number", "null"], "Used share in percent, from the bytes."),
                "ok": _OK,
            },
        ),
        "growth_dirs": _f(
            "array",
            "Size of each fixed growth directory (/var/backups, /holodeck-runtime).",
            items=_obj(
                "One directory.",
                {
                    "path": _f("string", "Directory path."),
                    "used_bytes": _f(["integer", "null"], "Size in bytes from 'du -sb'."),
                    "ok": _OK,
                },
            ),
        ),
    }
)

_BACKUPS_LIST = _envelope(
    {
        "rows": _f(
            "array",
            "One row per file, newest first.",
            items=_obj(
                "One backup file.",
                {
                    "path": _f("string", "Full file path."),
                    "mtime": _f(
                        "number", "When the file was last modified, in seconds since 1970."
                    ),
                    "size_bytes": _f("integer", "File size in bytes."),
                },
            ),
        ),
        "total": _TOTAL,
        "error": _ERROR,
    }
)

_PODS_GC = _envelope(
    {
        "deleted": _f(
            "boolean", "True when every delete command ran; check exit_status per entry."
        ),
        "phases": _f(
            "array",
            "The pod phases that were cleaned up.",
            items={"type": "string", "enum": ["Failed", "Succeeded"]},
        ),
        "namespace": _f(["string", "null"], "The namespace; null for the default namespace."),
        "deletes": _f(
            "array",
            "One entry per phase.",
            items=_obj(
                "One kubectl delete run.",
                {
                    "phase": _f("string", "The pod phase."),
                    "command": _COMMAND,
                    "stdout": _STDOUT,
                    "stderr": _STDERR,
                    "exit_status": _EXIT,
                },
            ),
        ),
        "error": _ERROR,
    }
)

_BACKUPS_PRUNE = _envelope(
    {
        "pruned": _f("boolean", "True when the prune command ran; check exit_status."),
        "directory": _f("string", "The directory that was pruned."),
        "keep_newest": _f("integer", "How many newest files were kept."),
        "command": _COMMAND,
        "stdout": _STDOUT,
        "stderr": _STDERR,
        "exit_status": _EXIT,
        "error": _ERROR,
    }
)

_IMAGES_IMPORT = _envelope(
    {
        "imported": _f("boolean", "True when the import command ran; check exit_status."),
        "tar_path": _f("string", "The image file that was imported."),
        "command": _COMMAND,
        "stdout": _STDOUT,
        "stderr": _STDERR,
        "exit_status": _EXIT,
        "error": _ERROR,
    }
)

_CONFIG_APPLY = _envelope(
    {
        "applied": _f(
            "boolean",
            "True only when the cmdlet returned a config id and the config file exists.",
        ),
        "config_id": _f(["string", "null"], "The new config id; pass it to instance.start."),
        "deploy": _obj(
            "The non-secret parameters of the call, echoed back.",
            {
                "version": _f("string", "VCF version."),
                "variant": _f("string", "Deploy profile."),
                "external_ip": _f("string", "Target vCenter host."),
                "master_cidr": _f("string", "Echoed only."),
                "vlan_range": _f("string", "Echoed only."),
                "placement": _f("object", "Echoed only.", additionalProperties=True),
                "instance_id": _f("string", "Echoed only."),
                "depot": _obj(
                    "Echoed depot address; no credential.",
                    {
                        "host": _f("string", "Depot host."),
                        "port": _f("integer", "Depot port."),
                        "protocol": _f("string", "http or https."),
                        "username": _f("string", "Depot user name."),
                    },
                ),
            },
        ),
        "error": _ERROR,
    }
)

_INSTANCE_START = _envelope(
    {
        "started": _f("boolean", "True when the deploy was started in the background."),
        "detached": _f("boolean", "True: the deploy runs on after this call returns."),
        "instance_id": _f("string", "The instance id."),
        "version": _f("string", "The VCF version."),
        "config_id": _f("string", "The config id used."),
        "precheck": _obj(
            "Result of the one-time vCenter login check.",
            {
                "ok": _f("boolean", "True when the login worked."),
                "server": _f("string", "The vCenter name, when the login worked."),
                "message": _f("string", "Why the login failed."),
            },
            additionalProperties=True,
        ),
        "start_marker": _obj(
            "Where the background deploy runs.",
            {
                "tmux_session": _f("string", "Name of the tmux session on the appliance."),
                "log_path": _f("string", "Deploy log file on the appliance."),
            },
        ),
        "error": _ERROR,
    }
)

_INSTANCE_STATUS = _envelope(
    {
        "status": _f(
            "string",
            "Deploy state.",
            enum=["running", "completed", "failed", "unknown", "removed"],
        ),
        "current_step": _f(["string", "null"], "The step the deploy is on."),
        "started_at": _f(["string", "null"], "When the deploy started."),
        "updated_at": _f(["string", "null"], "When the deploy state last changed."),
        "instance_id": _f(["string", "null"], "The instance id."),
        "notes": _f(
            "array",
            "Notes, for example the known harmless VCF 5.2 last-step message.",
            items={"type": "string"},
        ),
        "leftover_vms": _f(
            ["array", "null"],
            "After holodeck.instance.remove: the VMs whose name still starts with "
            "'<instance-id>-'. Null when MEHO could not check. Only with instance_id.",
            items={"type": "string"},
        ),
        "removal": _obj(
            "Progress of the last holodeck.instance.remove of this instance. Only "
            "with instance_id, and only after a remove was started.",
            {
                "state": _f(
                    "string",
                    "running, done or failed. interrupted: the script ended (for "
                    "example by the router reboot) before it finished; call "
                    "holodeck.instance.remove again to finish it.",
                ),
                "started_at": _f(["string", "null"], "When the remove started (UTC)."),
                "finished_at": _f(["string", "null"], "When the remove ended (UTC)."),
                "reset_router": _f("boolean", "True when the HoloRouter was reset."),
                "resumed": _f("boolean", "True when a second call finished an interrupted remove."),
                "error": _f(["string", "null"], "Error text of Remove-HoloDeckInstance."),
                "leftover_error": _f(["string", "null"], "Why MEHO could not list VMs."),
                "moved_to": _f(["string", "null"], "Folder the state files moved to."),
                "moved_files": _f("array", "Files that moved.", items={"type": "string"}),
                "move_errors": _f(
                    "array", "Files that did not move, with the error.", items={"type": "string"}
                ),
            },
        ),
        "error": _ERROR,
    },
    required=["status", "notes"],
)

_INSTANCE_REMOVE = _envelope(
    {
        "status": _f(
            "string",
            "started: the delete runs in the background. unchanged: no such "
            "instance, nothing deleted. refused: see 'reason'.",
            enum=["started", "unchanged", "refused"],
        ),
        "instance_id": _f("string", "The instance id."),
        "reason": _f("string", "Why the call was refused or changed nothing."),
        "config_id": _f("string", "The config MEHO loaded before the delete."),
        "reset_router": _f("boolean", "True when the HoloRouter is reset and rebooted."),
        "move_state_files": _f("boolean", "True when the state files are moved aside."),
        "resumed": _f(
            "boolean",
            "True when the call only finishes an interrupted remove: no VM is deleted, "
            "the leftover VMs are listed and the files move.",
        ),
        "vms": _f(
            "array",
            "VM names that Get-HoloDeckInstance or the instance's state file names.",
            items={"type": "string"},
        ),
        "files_to_move": _f(
            "array",
            "Files under /holodeck-runtime/config, output and state that move.",
            items={"type": "string"},
        ),
        "start_marker": _obj(
            "Where the background delete runs.",
            {
                "tmux_session": _f("string", "Name of the tmux session on the appliance."),
                "log_path": _f("string", "Output of the delete on the appliance."),
                "record_path": _f("string", "Progress file that holodeck.instance.status reads."),
            },
        ),
    },
    required=["status", "instance_id"],
)

_ROUTER_PATCH = _envelope(
    {
        "patched": _f("boolean", "True when at least one patch was written in this run."),
        "verify_only": _f("boolean", "True on a 9.1 appliance: MEHO only checked, wrote nothing."),
        "hr_version": _f(["string", "null"], "The appliance version MEHO used to decide."),
        "patches": _f(
            "array",
            "One entry per patch (C1, C2, C3).",
            items=_obj(
                "One patch.",
                {
                    "patch_id": _f("string", "C1, C2 or C3."),
                    "label": _f("string", "What the patch fixes."),
                    "state": _f(
                        "string",
                        "Patch state after the run.",
                        enum=["already_applied", "applied", "not_applicable"],
                    ),
                },
            ),
        ),
    }
)


#: Response schema per op id. Every Holodeck op has an entry.
HOLODECK_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "holodeck.about": _ABOUT,
    "holodeck.config.show": _CONFIG_SHOW,
    "holodeck.pod.list": _POD_LIST,
    "holodeck.pod.info": _POD_INFO,
    "holodeck.service.list": _SERVICE_LIST,
    "holodeck.k8s.exec": _K8S_EXEC,
    "holodeck.logs.tail": _LOGS_TAIL,
    "holodeck.networking.show": _NETWORKING_SHOW,
    "holodeck.disk.usage": _DISK_USAGE,
    "holodeck.backups.list": _BACKUPS_LIST,
    "holodeck.k8s.pods.gc": _PODS_GC,
    "holodeck.backups.prune": _BACKUPS_PRUNE,
    "holodeck.images.import": _IMAGES_IMPORT,
    "holodeck.config.apply": _CONFIG_APPLY,
    "holodeck.instance.start": _INSTANCE_START,
    "holodeck.instance.status": _INSTANCE_STATUS,
    "holodeck.instance.remove": _INSTANCE_REMOVE,
    "holodeck.router.patch": _ROUTER_PATCH,
}
