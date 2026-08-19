# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# code-quality-allow: the four deploy-lifecycle ops each carry a JSON-Schema
# parameter block + curated llm_instructions + a focused handler; keeping the
# op metadata beside its handler (the ops_write.py / ops_read.py precedent in
# this same connector) is more cohesive than splitting the tuple into a second
# module purely to sit under the 600-line file bound.

"""Deploy-lifecycle typed ops for :class:`HolodeckConnector` (#2908, Initiative #2907).

G3.8 shipped identity + read ops; G3.18-T2 (#2154) shipped three narrow
remediation writes. This module closes the last hand-run gap on the
fresh-deploy path: the consumer's ``New-HoloDeckConfig`` /
``New-HoloDeckInstance`` / per-appliance-patch procedure ran by hand in a
detached tmux on the HoloRouter and could not be encoded as governed
runbook/workflow steps. Four typed ops make the full lifecycle
dispatchable:

============================  ===========  ===================================
op_id                         safety       action
============================  ===========  ===================================
``holodeck.config.apply``     dangerous    ``New-HoloDeckConfig`` (NEVER
                                           ``-Default``) + ``Import-HoloDeckConfig``
``holodeck.instance.start``   dangerous    fail-fast ``Connect-VIServer``
                                           precheck, then detached
                                           ``New-HoloDeckInstance``
``holodeck.instance.status``  safe         ``Get-HoloDeckInstance`` -> envelope
``holodeck.router.patch``     dangerous    idempotent verify-then-apply of the
                                           three per-appliance patches
============================  ===========  ===================================

Secret hygiene (#1503)
======================

None of these ops carries credential material in its *params*. The
deploy-target vCenter SSO credentials and the Broadcom build token live in
the HoloRouter's own deploy environment (staged by the consumer's
Vault-to-env discipline) and in the on-appliance persisted config
(``/holodeck-runtime/config/<config-id>.json``, written by
``New-HoloDeckConfig``). MEHO's ops *orchestrate* the cmdlets that read
those on-appliance secrets; MEHO never handles the secret bytes, so nothing
credential-shaped can reach the params-echo, audit, or broadcast surfaces.

The PowerShell scripts these handlers compose reference the credentials only
by variable **name** (``$env:VC_USER`` / ``$env:VC_PW``, the persisted
``$config.Target.password``) -- never an interpolated value -- honouring the
``_pwsh`` safety contract that the encoded script body (visible on the remote
argv) must carry no credential material.

``holodeck.router.patch`` is additionally pinned into
:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS` as
defence-in-depth: should a future param-shape change ever place credential
material in its params, the broadcast collapses to aggregate-only rather than
shipping it on the feed (the ``rke2.node.config.update`` / ``keycloak.*``
precedent). ``holodeck.config.apply`` is deliberately NOT pinned -- it carries
only non-secret config + Vault *refs*, so it keeps its generic params-echo
preview (a param-echo the credential-class suppression would otherwise remove);
its hygiene holds by construction because no secret is ever a param.

References
----------

* Task: https://github.com/evoila/meho/issues/2908
* Parent initiative (backplane-first register): #2907
* Op-identity envelope: #2681 (stamped by the dispatcher, not here).
* Secret hygiene: #1503; credential-write classification #1401.
* Consumer ground truth: evoila-bosnia/claude-rdc-hetzner-dc
  ``kb/holodeck-9.0-deploy-runbook.md`` (7-phase procedure),
  ``.claude/skills/deploy-holodeck-instance/SKILL.md`` (patch details +
  precheck lesson), ``kb/holodeck-9.0-overview.md`` (5.2 divergences).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.holodeck._pwsh import PwshRunError, pwsh_run
from meho_backplane.connectors.holodeck.ops import SSH_TRANSPORT_NOTE, HolodeckOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "DEPLOY_OPS",
    "HOLODECK_DEPLOY_CREDENTIAL_OPS",
    "HOLODECK_WHEN_TO_USE_DEPLOY_BY_GROUP",
    "ROUTER_PATCHES",
    "RouterPatchSpec",
    "holodeck_config_apply",
    "holodeck_instance_start",
    "holodeck_instance_status",
    "holodeck_router_patch",
    "map_instance_status_envelope",
    "target_hr_version",
    "validate_config_apply_params",
]

# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

#: The VCF version enum ``holodeck.config.apply`` / ``holodeck.instance.start``
#: accept. Mirrors the consumer overview's supported-version set plus the 9.1
#: line the fresh-deploy runbook now covers. ``5.2.x`` selects the Cloud
#: Builder path (no offline depot); ``9.x`` selects the VCF Installer +
#: offline-depot path.
_DEPLOY_VERSION_ENUM: tuple[str, ...] = (
    "5.2",
    "5.2.1",
    "5.2.2",
    "9.0.0.0",
    "9.0.1.0",
    "9.0.2.0",
    "9.1.0.0",
)


def _is_cloud_builder_version(version: str) -> bool:
    """Return ``True`` when *version* is a VCF 5.2.x line (Cloud Builder, no depot).

    VCF 5.2.x deploys via Cloud Builder and has no offline-depot concept, so
    depot params are invalid for it (the ``holodeck.config.apply`` refusal).
    9.x lines deploy via VCF Installer + an offline depot.
    """
    return version.startswith("5.2")


def target_hr_version(target: Any) -> str | None:
    """Resolve the HoloRouter's Holodeck-toolkit version from *target*.

    Prefers the operator-asserted ``Target.version`` column, falling back to
    the probe-derived ``fingerprint.version``. Used by
    :func:`holodeck_router_patch` to select the verify-only posture on a 9.1
    HoloRouter (where the three per-appliance patches are already fixed
    upstream). ``None`` when the target carries neither -- treated as a
    non-9.1 (patchable) appliance so the verify-then-apply path still runs.
    """
    version = getattr(target, "version", None)
    if isinstance(version, str) and version.strip():
        return version.strip()
    fingerprint = getattr(target, "fingerprint", None)
    if isinstance(fingerprint, dict):
        fp_version = fingerprint.get("version")
        if isinstance(fp_version, str) and fp_version.strip():
            return fp_version.strip()
    return None


def _is_ninety_one(version: str | None) -> bool:
    """Return ``True`` when *version* names a 9.1.x HoloRouter (verify-only)."""
    return bool(version) and version.startswith("9.1")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# config.apply -- param validation (depot refusal for 5.2.x)
# ---------------------------------------------------------------------------


def validate_config_apply_params(params: dict[str, Any]) -> list[str]:
    """Return semantic-validation errors for ``holodeck.config.apply`` params.

    The one domain rule the JSON Schema also enforces (via ``if``/``then``)
    but that is surfaced here as a clear message for the direct-call and
    handler belt-and-braces paths: **depot params are invalid for a VCF
    5.2.x deploy** -- 5.2 uses Cloud Builder and has no offline depot. The
    schema-level ``if``/``then`` rejects the combination pre-park (before the
    approval queue), so an operator never approves a config that would fail;
    this function is the same rule expressed as an operator-actionable string
    for tests and for a handler called directly (bypassing the dispatcher's
    schema validation). Returns ``[]`` when the params are consistent.
    """
    errors: list[str] = []
    version = params.get("version")
    if isinstance(version, str) and _is_cloud_builder_version(version) and "depot" in params:
        errors.append(
            f"depot params are not valid for VCF {version} (5.2.x deploys via "
            "Cloud Builder and has no offline depot); omit 'depot' for a 5.2.x target"
        )
    return errors


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def holodeck_config_apply(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Apply a Holodeck deploy config: ``New-HoloDeckConfig`` + ``Import-HoloDeckConfig``.

    Op-id: ``holodeck.config.apply``. Composes the canonical deploy-config
    step from the consumer runbook's Phase F wrapper:

    * ``New-HoloDeckConfig`` **without** ``-Default`` (the ``-Default`` flag
      triggers a buggy internal ``Set-HoloDeckConfig`` call -- the storm-lock
      lesson from consumer #258). The vCenter deploy-target SSO credentials
      are read on the appliance from ``$env:VC_USER`` / ``$env:VC_PW`` (staged
      by the consumer's Vault-to-env discipline); this handler references them
      by name only, never a value.
    * ``$null = Import-HoloDeckConfig -ConfigID <id>`` -- required to set
      ``$Global:config`` for the downstream ``New-HoloDeckInstance``; the
      ``$null =`` suppresses the auto-print that would dump the persisted
      cleartext password to stdout.

    The depot-vs-5.2.x contradiction is rejected pre-park by the parameter
    schema; :func:`validate_config_apply_params` re-checks it here as
    belt-and-braces for a direct call that bypassed the dispatcher.

    Runs only on the ``_approved=True`` resume path (``requires_approval``).
    Returns ``{applied, config_id, deploy: {version, variant, ...}}`` -- a
    param-echo of the non-secret applied config; no credential material is
    read or echoed.
    """
    semantic_errors = validate_config_apply_params(params)
    if semantic_errors:
        return {"applied": False, "error": "; ".join(semantic_errors)}

    version = str(params["version"])
    variant = str(params["variant"])
    description = str(params.get("description") or f"MEHO holodeck deploy ({version} {variant})")
    external_ip = params.get("external_ip")

    # New-HoloDeckConfig reads the deploy-target creds on the appliance from
    # $env:VC_USER / $env:VC_PW (staged out-of-band per the runbook). NEVER
    # -Default. The description is the only operator-supplied string
    # interpolated into the script body, and it carries no credential.
    quoted_desc = _pwsh_single_quote(description)
    quoted_host = _pwsh_single_quote(str(external_ip)) if external_ip else "$env:VC_HOST"
    script = (
        "$Global:ProgressPreference = 'SilentlyContinue'; "
        f"$cfg = New-HoloDeckConfig -Description {quoted_desc} "
        f"-TargetHost {quoted_host} -UserName $env:VC_USER -Password $env:VC_PW; "
        "$null = Import-HoloDeckConfig -ConfigID $cfg.configID; "
        "@{ configID = $cfg.configID } | ConvertTo-Json -Compress"
    )

    try:
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"applied": False, "error": str(exc)}

    config_id = payload.get("configID") if isinstance(payload, dict) else None
    return {
        "applied": True,
        "config_id": config_id,
        "deploy": _echo_deploy_config(params),
    }


async def holodeck_instance_start(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Launch ``New-HoloDeckInstance`` detached, after a fail-fast auth precheck.

    Op-id: ``holodeck.instance.start``. Two phases, ordered so the
    retry-storm never fires against a bad credential:

    1. **Single-shot fail-fast precheck.** Reads the persisted config on the
       appliance (``/holodeck-runtime/config/<config_id>.json``), builds a
       ``PSCredential`` from ``$config.Target``, and makes exactly **one**
       ``Connect-VIServer ... -ErrorAction Stop`` attempt. This catches a
       stale / locked deploy-target credential *before* Holodeck's
       4-retry-in-13s pattern storm-locks the SSO account (consumer #258
       lesson). On any precheck failure the handler returns immediately and
       the instance is **never** launched.

    2. **Detached launch.** On a green precheck, ``New-HoloDeckInstance`` is
       started under ``tmux new-session -d`` so it survives the dispatch
       returning / the SSH channel closing, and the handler returns at once
       with the instance id + a start marker (the tmux session + log path).

    Runs only on the ``_approved=True`` resume path (``requires_approval``).
    The precheck reads the persisted password on the appliance and returns
    only ``ok`` / a sanitised message -- the credential never crosses back to
    MEHO.
    """
    config_id = str(params["config_id"])
    instance_id = str(params["instance_id"])
    version = str(params["version"])

    precheck = await _run_connect_precheck(self, target, config_id, operator)
    if not precheck.get("ok"):
        return {
            "started": False,
            "precheck": precheck,
            "error": "fail-fast Connect-VIServer precheck failed; instance not launched",
        }

    session_name = f"holodeck-deploy-{instance_id}"
    log_path = f"/holodeck-runtime/logs/{instance_id}-deploy.log"
    launch_cmd = _compose_detached_launch(params, session_name=session_name, log_path=log_path)
    try:
        await self._run_command(target, launch_cmd, operator=operator)
    except Exception as exc:
        return {"started": False, "precheck": precheck, "error": str(exc)}

    return {
        "started": True,
        "detached": True,
        "instance_id": instance_id,
        "version": version,
        "config_id": config_id,
        "precheck": precheck,
        "start_marker": {"tmux_session": session_name, "log_path": log_path},
    }


async def holodeck_instance_status(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Read ``Get-HoloDeckInstance`` into a structured deploy-status envelope.

    Op-id: ``holodeck.instance.status``. Runs ``Get-HoloDeckInstance``
    (optionally scoped to ``instance_id``) and maps the cmdlet output into a
    flat, deterministic envelope -- ``status`` (``running`` | ``completed`` |
    ``failed`` | ``unknown``), ``current_step``, ``started_at``,
    ``updated_at``, plus a ``notes`` list. The flat ``status`` field is what a
    runbook ``OperationCallVerify`` step matches against and what a deploy-in-
    progress Sensor asserts on, so the op is ``safe`` / no-approval and its
    shape never depends on approval state.

    The known-benign VCF 5.2.x terminal quirk -- the final
    ``Sync-HolodeckComponents`` step throwing *"No route to host"* because VCF
    Operations is not deployed in 5.2 -- is surfaced as a structured
    ``notes`` entry with ``status="completed"``, **not** a failure, so a
    verify/Sensor built on this op does not read a fully-deployed 5.2.x lab as
    broken.
    """
    instance_id = params.get("instance_id")
    if instance_id:
        select = f"Get-HoloDeckInstance -InstanceID {_pwsh_single_quote(str(instance_id))}"
    else:
        select = "Get-HoloDeckInstance"
    script = f"{select} | ConvertTo-Json -Depth 6 -Compress"

    try:
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"status": "unknown", "error": str(exc), "notes": []}

    return map_instance_status_envelope(payload, hr_version=target_hr_version(target))


async def holodeck_router_patch(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Idempotently verify-then-apply the three per-appliance HoloRouter patches.

    Op-id: ``holodeck.router.patch``. For each of the three patches (C1
    bundle-count ``-eq 7`` -> ``-ge 7``; C2 ``BroadcomBuildToken`` SecureString
    wrap; C3 ``Auth.psm1`` ``Connect-VIServer`` PSCredential) the handler:

    1. greps the appliance module for the **applied** marker. Present at the
       expected count -> ``already_applied`` (no write).
    2. otherwise greps for the **unpatched** marker. Present ->
       run the ``sed`` fix (after a timestamped backup), re-verify -> ``applied``.
    3. neither marker present -> ``not_applicable`` (the OVA's module shape
       drifted; nothing safe to sed).

    On a **9.1 HoloRouter** all three patches are already fixed upstream, so
    the op runs **verify-only**: it greps but never seds -- each patch reports
    ``already_applied`` (marker present) or ``not_applicable`` (absent),
    matching the runbook's "grep the sites; if the OLD pattern is absent
    you're done" posture.

    Runs only on the ``_approved=True`` resume path (``requires_approval``).
    Secret hygiene: the patch strings are fixed constants (the C2 replacement
    references ``$env:brcm_build_token`` by name, never a token value), and the
    result reports only patch id / state / grep counts -- no file content and
    no credential-shaped substring reaches any governance surface.
    """
    del params  # no operator-supplied params; the patch set is fixed in code
    hr_version = target_hr_version(target)
    verify_only = _is_ninety_one(hr_version)

    results: list[dict[str, Any]] = []
    any_applied = False
    for patch in ROUTER_PATCHES:
        outcome = await _reconcile_patch(
            self, target, patch, verify_only=verify_only, operator=operator
        )
        if outcome["state"] == "applied":
            any_applied = True
        results.append(outcome)

    return {
        "patched": any_applied,
        "verify_only": verify_only,
        "hr_version": hr_version,
        "patches": results,
    }


# ---------------------------------------------------------------------------
# Router-patch specs + reconcile
# ---------------------------------------------------------------------------

_HOLODECK_MODULES = "/root/.local/share/powershell/Modules/HoloDeck"


@dataclass(frozen=True)
class RouterPatchSpec:
    """One per-appliance HoloRouter patch: how to verify it and how to apply it.

    All fields are fixed constants -- no operator input reaches a grep or sed
    -- so the composed commands carry no injection surface and the C2 spec's
    ``$env:brcm_build_token`` reference is a PowerShell variable name, never a
    secret value.
    """

    patch_id: str
    label: str
    file_path: str
    applied_marker: str
    applied_count: int
    unpatched_marker: str
    sed_expr: str


#: The three per-appliance patches, in C1 / C2 / C3 order. Grounded on the
#: consumer runbook Phase C + ``kb/holodeck-9.0-known-issues.md`` (expected
#: grep counts: C1=1, C2=2 lines 363+542, C3=1).
ROUTER_PATCHES: tuple[RouterPatchSpec, ...] = (
    RouterPatchSpec(
        patch_id="C1",
        label="bundle-count off-by-one (-eq 7 -> -ge 7)",
        file_path=f"{_HOLODECK_MODULES}/Modules/SddcMgmtDeployment.psm1",
        applied_marker="vcf9_bundle_details.count -ge 7",
        applied_count=1,
        unpatched_marker="vcf9_bundle_details.count -eq 7",
        sed_expr="s|$vcf9_bundle_details.count -eq 7|$vcf9_bundle_details.count -ge 7|",
    ),
    RouterPatchSpec(
        patch_id="C2",
        label="BroadcomBuildToken SecureString wrap",
        file_path=f"{_HOLODECK_MODULES}/HoloDeck.psm1",
        applied_marker="ConvertTo-SecureString -AsPlainText -Force -String $env:brcm_build_token",
        applied_count=2,
        unpatched_marker="$Global:BroadcomBuildToken = $env:brcm_build_token",
        sed_expr=(
            "s|$Global:BroadcomBuildToken = $env:brcm_build_token|"
            "$Global:BroadcomBuildToken = (ConvertTo-SecureString -AsPlainText -Force "
            "-String $env:brcm_build_token)|g"
        ),
    ),
    RouterPatchSpec(
        patch_id="C3",
        label="Auth.psm1 Connect-VIServer PSCredential",
        file_path=f"{_HOLODECK_MODULES}/Modules/Auth.psm1",
        applied_marker="-Credential $_hd_cred",
        applied_count=1,
        unpatched_marker="-User $config.Target.username -Password $config.Target.password",
        sed_expr=(
            "s|Connect-VIServer -Server $config.target.hostname -User "
            "$config.Target.username -Password $config.Target.password -Force|"
            "$_hd_cred = New-Object System.Management.Automation.PSCredential("
            "$config.Target.username, (ConvertTo-SecureString -AsPlainText -Force "
            "-String $config.Target.password)); Connect-VIServer -Server "
            "$config.target.hostname -Credential $_hd_cred -Force|"
        ),
    ),
)


async def _grep_count(
    self: HolodeckConnector,
    target: Any,
    marker: str,
    file_path: str,
    operator: Operator | None,
) -> int:
    """Return the count of *marker* (fixed-string) occurrences in *file_path*.

    ``grep -cF -e`` counts fixed-string matches; ``-e`` guards a marker that
    starts with ``-``. A no-match (grep exit 1) or a missing file (exit 2)
    parses to ``0`` rather than raising -- an absent marker is a legitimate
    "not in this state" answer, not an error.
    """
    cmd = f"grep -cF -e {shlex.quote(marker)} {shlex.quote(file_path)}"
    proc = await self._run_command(target, cmd, operator=operator)
    stdout = getattr(proc, "stdout", "") or ""
    if not isinstance(stdout, str):
        return 0
    try:
        return int(stdout.strip() or "0")
    except ValueError:
        return 0


async def _reconcile_patch(
    self: HolodeckConnector,
    target: Any,
    patch: RouterPatchSpec,
    *,
    verify_only: bool,
    operator: Operator | None,
) -> dict[str, Any]:
    """Verify -> (optionally) apply -> re-verify one patch; return its state.

    States: ``already_applied`` (applied marker present at count), ``applied``
    (unpatched marker sed-fixed then re-verified), ``not_applicable`` (neither
    marker present, or 9.1 verify-only with the applied marker absent). On a
    9.1 HoloRouter (``verify_only``) the ``sed`` branch is never taken.
    """
    applied = await _grep_count(self, target, patch.applied_marker, patch.file_path, operator)
    if applied >= patch.applied_count:
        return {"patch_id": patch.patch_id, "label": patch.label, "state": "already_applied"}

    if verify_only:
        # 9.1: the patches are fixed upstream. If the applied marker is absent
        # the module shape is unexpected -- report, never sed.
        return {"patch_id": patch.patch_id, "label": patch.label, "state": "not_applicable"}

    unpatched = await _grep_count(self, target, patch.unpatched_marker, patch.file_path, operator)
    if unpatched < 1:
        return {"patch_id": patch.patch_id, "label": patch.label, "state": "not_applicable"}

    quoted_file = shlex.quote(patch.file_path)
    backup = f"cp -av {quoted_file} {quoted_file}.bak.$(date +%Y%m%d-%H%M%S)"
    sed = f"sed -i {shlex.quote(patch.sed_expr)} {quoted_file}"
    await self._run_command(target, f"{backup} && {sed}", operator=operator)

    reverified = await _grep_count(self, target, patch.applied_marker, patch.file_path, operator)
    state = "applied" if reverified >= patch.applied_count else "not_applicable"
    return {"patch_id": patch.patch_id, "label": patch.label, "state": state}


# ---------------------------------------------------------------------------
# instance.start -- precheck + detached launch
# ---------------------------------------------------------------------------

#: The single-shot fail-fast Connect-VIServer precheck. Reads the persisted
#: config on the appliance, builds a PSCredential, and makes exactly ONE
#: Connect-VIServer attempt (ErrorAction Stop). Emits ``{ok, server?, message}``
#: as JSON -- the password is read on the appliance and never returned. The
#: ``{config_json}`` placeholder is filled with the shell-quoted config path.
_PRECHECK_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$null = Set-PowerCLIConfiguration -InvalidCertificateAction Ignore "
    "-Scope User -Confirm:$false; "
    "try {{ "
    "$c = (Get-Content -Raw {config_json} | ConvertFrom-Json).Target; "
    "$pw = ConvertTo-SecureString -AsPlainText -Force -String $c.password; "
    "$cred = New-Object System.Management.Automation.PSCredential($c.username, $pw); "
    "$r = Connect-VIServer -Server $c.hostname -Credential $cred -ErrorAction Stop; "
    "Disconnect-VIServer -Server * -Confirm:$false -ErrorAction SilentlyContinue; "
    "@{{ ok = $true; server = $r.Name }} | ConvertTo-Json -Compress "
    "}} catch {{ @{{ ok = $false; message = $_.Exception.Message }} | ConvertTo-Json -Compress }}"
)


async def _run_connect_precheck(
    self: HolodeckConnector,
    target: Any,
    config_id: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run the single-shot Connect-VIServer precheck; return ``{ok, ...}``.

    A ``PwshRunError`` (the pwsh process itself failing) is folded into
    ``{ok: False, message}`` so the caller's fail-fast branch is the single
    place that decides not to launch.
    """
    config_json = shlex.quote(f"/holodeck-runtime/config/{config_id}.json")
    script = _PRECHECK_SCRIPT.format(config_json=config_json)
    try:
        payload = await pwsh_run(self, target, script, operator=operator)
    except PwshRunError as exc:
        return {"ok": False, "message": str(exc)}
    if isinstance(payload, dict):
        return {"ok": bool(payload.get("ok")), **{k: v for k, v in payload.items() if k != "ok"}}
    return {"ok": False, "message": "precheck returned an unexpected shape"}


def _compose_detached_launch(
    params: dict[str, Any],
    *,
    session_name: str,
    log_path: str,
) -> str:
    """Compose the detached ``New-HoloDeckInstance`` launch command.

    ``New-HoloDeckInstance`` is started under ``tmux new-session -d`` so it
    survives the dispatch returning. The deploy env vars (cluster, datastore,
    depot, brcm_build_token, ...) are staged on the appliance by the consumer
    tooling and read by the cmdlet from ``$env:``; this handler never
    interpolates a credential value. Non-secret deploy knobs come from
    *params* and are shell-quoted.
    """
    # Build the New-HoloDeckInstance argument list from bounded, non-secret
    # params. Each token is a fixed flag or a quoted scalar.
    args = [f"-Version {_pwsh_single_quote(str(params['version']))}"]
    args.append(f"-InstanceID {_pwsh_single_quote(str(params['instance_id']))}")
    variant = str(params.get("variant") or "management_only")
    if variant == "management_only":
        args.append("-ManagementOnly")
    args.append(f"-Site {_pwsh_single_quote(str(params.get('site') or 'a'))}")
    args.append(f"-vSANMode {_pwsh_single_quote(str(params.get('vsan_mode') or 'OSA'))}")
    version = str(params["version"])
    if not _is_cloud_builder_version(version):
        depot_type = str(params.get("depot_type") or "Offline")
        args.append(f"-DepotType {_pwsh_single_quote(depot_type)}")
    args.append("-DeveloperMode")
    inner = "New-HoloDeckInstance " + " ".join(args)
    # Import the persisted config first so $Global:config is set for the
    # cmdlet; $null = suppresses the cleartext-password auto-print.
    config_id = str(params["config_id"])
    pwsh_body = f"$null = Import-HoloDeckConfig -ConfigID {_pwsh_single_quote(config_id)}; {inner}"
    encoded = _pwsh_encoded(pwsh_body)
    remote = (
        f"pwsh -NoProfile -NonInteractive -EncodedCommand {encoded} > {shlex.quote(log_path)} 2>&1"
    )
    return f"tmux new-session -d -s {shlex.quote(session_name)} {shlex.quote(remote)}"


# ---------------------------------------------------------------------------
# instance.status -- envelope mapping
# ---------------------------------------------------------------------------

_RUNNING_TOKENS = frozenset({"inprogress", "in_progress", "running", "started", "deploying"})
_COMPLETED_TOKENS = frozenset({"completed", "complete", "succeeded", "success", "finished", "done"})
_FAILED_TOKENS = frozenset({"failed", "failure", "error", "aborted", "completed_with_failure"})

#: The benign VCF 5.2.x terminal quirk: the final Sync-HolodeckComponents step
#: throws "No route to host" because VCF Operations is not deployed in 5.2.
_SYNC_STEP = "sync-holodeckcomponents"
_NO_ROUTE = "no route to host"


def _normalise_status(raw: Any) -> str:
    """Map a cmdlet status token to ``running`` | ``completed`` | ``failed`` | ``unknown``."""
    token = str(raw).strip().lower().replace(" ", "_") if raw is not None else ""
    if token in _RUNNING_TOKENS:
        return "running"
    if token in _COMPLETED_TOKENS:
        return "completed"
    if token in _FAILED_TOKENS:
        return "failed"
    return "unknown"


def _first(record: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among *keys* (case-insensitive)."""
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _is_benign_5_2_quirk(record: dict[str, Any], hr_version: str | None) -> bool:
    """Return ``True`` when the failure is the known-benign 5.2.x Sync quirk.

    The final ``Sync-HolodeckComponents`` step reporting *"No route to host"*
    on a 5.2.x deploy is expected (VCF Operations is not deployed in 5.2) and
    is not a real failure. Detected by the step name + the message, gated on a
    5.2.x deploy version when one is discernible from the record or target.
    """
    step = str(_first(record, "current_step", "currentstep", "step", "stage") or "").lower()
    message = str(_first(record, "message", "error", "detail", "laststep") or "").lower()
    version = str(_first(record, "version") or hr_version or "")
    looks_like_sync = _SYNC_STEP in step or _SYNC_STEP in message
    no_route = _NO_ROUTE in message
    return (
        (looks_like_sync or no_route)
        and no_route
        and (_is_cloud_builder_version(version) or version == "")
    )


def map_instance_status_envelope(
    payload: Any,
    *,
    hr_version: str | None = None,
) -> dict[str, Any]:
    """Map ``Get-HoloDeckInstance`` output into the structured status envelope.

    Accepts the cmdlet's dict (single instance) or list (first element used)
    shape and returns ``{status, current_step, started_at, updated_at,
    instance_id, notes}``. ``status`` is the flat verify/Sensor target. A
    ``failed`` status that is actually the benign 5.2.x
    ``Sync-HolodeckComponents`` / "No route to host" terminal quirk is
    re-mapped to ``completed`` with an explanatory ``notes`` entry.
    """
    record = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(record, dict):
        return {"status": "unknown", "current_step": None, "notes": []}

    status = _normalise_status(_first(record, "status", "state", "deploystatus"))
    notes: list[str] = []
    if status in ("failed", "unknown") and _is_benign_5_2_quirk(record, hr_version):
        notes.append(
            "benign VCF 5.2.x terminal quirk: the final Sync-HolodeckComponents step "
            "throws 'No route to host' because VCF Operations is not deployed in 5.2 -- "
            "the VCF environment is fully deployed; not a failure"
        )
        status = "completed"

    return {
        "status": status,
        "current_step": _first(record, "current_step", "currentstep", "step", "stage"),
        "started_at": _first(record, "started_at", "starttime", "started", "createtime"),
        "updated_at": _first(record, "updated_at", "lastupdate", "updated", "modifiedtime"),
        "instance_id": _first(record, "instance_id", "instanceid", "id", "name"),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Small pwsh-composition helpers
# ---------------------------------------------------------------------------


def _pwsh_single_quote(value: str) -> str:
    """Single-quote *value* for a PowerShell string literal (doubling any ``'``).

    PowerShell single-quoted strings are literal (no ``$`` expansion), so this
    is the safe way to embed an operator-supplied non-secret scalar (a
    description, an instance id) into a composed script without it being
    parsed as a variable or subexpression.
    """
    return "'" + value.replace("'", "''") + "'"


def _pwsh_encoded(script: str) -> str:
    """Return the ``-EncodedCommand`` base64 for *script* (UTF-16LE, no BOM)."""
    from meho_backplane.connectors.holodeck._pwsh import encode_pwsh_command

    return encode_pwsh_command(script)


def _echo_deploy_config(params: dict[str, Any]) -> dict[str, Any]:
    """Return the non-secret applied-config echo for ``config.apply``'s result.

    Mirrors the parked param-echo preview so the executed result and the
    approval-time preview agree on the applied config. Carries no credential
    field (none exists in the params).
    """
    echo: dict[str, Any] = {
        "version": params.get("version"),
        "variant": params.get("variant"),
    }
    for key in ("external_ip", "master_cidr", "vlan_range", "placement", "instance_id"):
        if key in params:
            echo[key] = params[key]
    if "depot" in params:
        depot = params["depot"]
        # Echo only the non-secret depot addressing -- never a credential_ref
        # value beyond its presence is needed, and username is non-secret.
        if isinstance(depot, dict):
            echo["depot"] = {
                k: depot.get(k) for k in ("host", "port", "protocol", "username") if k in depot
            }
    return echo


#: Deploy-lifecycle ops pinned into
#: :data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS` as broadcast
#: defence-in-depth. Only ``holodeck.router.patch`` (BroadcomBuildToken
#: adjacency); ``holodeck.config.apply`` keeps its param-echo preview (no
#: secret is ever a param) and is intentionally absent. Kept in sync with the
#: literal pin in ``broadcast/events.py`` by the deploy secret-hygiene test.
HOLODECK_DEPLOY_CREDENTIAL_OPS: frozenset[str] = frozenset({"holodeck.router.patch"})


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs (one per deploy op group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_DEPLOY_CONFIG = (
    "Use to lay down a Holodeck deploy configuration before launching an "
    "instance: ``holodeck.config.apply`` runs ``New-HoloDeckConfig`` (never "
    "-Default) + ``Import-HoloDeckConfig`` on the HoloRouter, capturing the "
    "VCF version, variant, external IP, master CIDR/VLAN, placement, and (for "
    "9.x only) the offline-depot connection. Approval-gated. Depot params are "
    "refused for a VCF 5.2.x deploy (5.2 uses Cloud Builder, no offline "
    "depot). The vCenter deploy-target credentials are read on the appliance "
    "from its staged environment -- they are never a parameter. Pair with "
    "``holodeck.instance.start`` to launch. " + SSH_TRANSPORT_NOTE
)

_WHEN_TO_USE_DEPLOY_LIFECYCLE = (
    "Use to launch a nested-VCF deploy from a config already applied by "
    "``holodeck.config.apply``: ``holodeck.instance.start`` runs a single-shot "
    "fail-fast ``Connect-VIServer`` precheck (so a stale/locked deploy-target "
    "credential can't storm-lock the SSO account via Holodeck's retry loop), "
    "then launches ``New-HoloDeckInstance`` detached (survives the call "
    "returning) and returns the instance id + start marker immediately. "
    "Approval-gated. Poll progress with ``holodeck.instance.status``. " + SSH_TRANSPORT_NOTE
)

_WHEN_TO_USE_DEPLOY_STATUS = (
    "Use to read a Holodeck deploy's progress: ``holodeck.instance.status`` "
    "maps ``Get-HoloDeckInstance`` into a flat envelope (status "
    "running|completed|failed, current step, timestamps). Safe / no approval, "
    "so it doubles as a runbook OperationCallVerify target and a "
    "deploy-in-progress Sensor op. It treats the benign VCF 5.2.x terminal "
    "quirk (final Sync-HolodeckComponents 'No route to host', VCF Ops absent "
    "in 5.2) as a structured note, not a failure. " + SSH_TRANSPORT_NOTE
)

_WHEN_TO_USE_DEPLOY_PATCH = (
    "Use to bring a HoloRouter's three mandatory per-appliance patches into "
    "the applied state before a fresh deploy: ``holodeck.router.patch`` "
    "verify-then-applies C1 (bundle-count -eq 7 -> -ge 7), C2 "
    "(BroadcomBuildToken SecureString) and C3 (Auth.psm1 PSCredential), "
    "reporting per-patch state (already_applied|applied|not_applicable). It is "
    "idempotent -- a second run reports all already_applied -- and runs "
    "verify-only on a 9.1 HoloRouter (patches fixed upstream). Approval-gated; "
    "no token or credential value ever appears in the result. " + SSH_TRANSPORT_NOTE
)

#: Curated ``when_to_use`` per deploy op group. Keys carry a ``deploy-``
#: prefix so they never collide with the read/write group keys the connector's
#: ``register_operations`` walk merges alongside them.
HOLODECK_WHEN_TO_USE_DEPLOY_BY_GROUP: dict[str, str] = {
    "deploy-config": _WHEN_TO_USE_DEPLOY_CONFIG,
    "deploy-lifecycle": _WHEN_TO_USE_DEPLOY_LIFECYCLE,
    "deploy-status": _WHEN_TO_USE_DEPLOY_STATUS,
    "deploy-patch": _WHEN_TO_USE_DEPLOY_PATCH,
}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_CONFIG_APPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {
            "type": "string",
            "enum": list(_DEPLOY_VERSION_ENUM),
            "description": (
                "VCF version to deploy. 5.2.x uses Cloud Builder (no depot); "
                "9.x uses VCF Installer + offline depot."
            ),
        },
        "variant": {
            "type": "string",
            "enum": ["management_only", "vvf", "full"],
            "description": (
                "Deploy profile: management_only (mgmt domain only), vvf, or "
                "full (mgmt + workload)."
            ),
        },
        "external_ip": {
            "type": "string",
            "description": "Deploy-target vCenter host/IP (New-HoloDeckConfig -TargetHost).",
        },
        "master_cidr": {
            "type": "string",
            "description": "Master management CIDR for the nested lab network.",
        },
        "vlan_range": {
            "type": "string",
            "description": "VLAN range for the nested lab fabric (e.g. 3000-3010).",
        },
        "placement": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "cluster": {"type": "string"},
                "datastore": {"type": "string"},
            },
            "additionalProperties": False,
            "description": "vSphere placement refs for the HoloRouter + nested ESXi.",
        },
        "depot": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "minLength": 1},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "protocol": {"type": "string", "enum": ["http", "https"]},
                "username": {"type": "string"},
                "credential_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Vault path (NOT a plaintext password) for the offline-depot credential."
                    ),
                },
            },
            "required": ["host"],
            "additionalProperties": False,
            "description": (
                "Offline-depot connection (9.x only). Rejected for a 5.2.x "
                "version. Credentials are referenced by Vault path, never inline."
            ),
        },
        "instance_id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9]{1,8}$",
            "description": (
                "Optional 8-char alphanumeric instance id to embed in the config description."
            ),
        },
        "description": {
            "type": "string",
            "description": "Human description persisted onto the Holodeck config.",
        },
    },
    "required": ["version", "variant"],
    "additionalProperties": False,
    # Depot params are invalid for a VCF 5.2.x deploy (Cloud Builder, no
    # offline depot). Encoded as if/then/not so the framework validate_params
    # (Draft 2020-12) rejects the combination pre-park, before the approval
    # queue -- an operator never approves a config that would fail.
    "allOf": [
        {
            "if": {"properties": {"version": {"pattern": r"^5\.2"}}, "required": ["version"]},
            "then": {"not": {"required": ["depot"]}},
        }
    ],
}

_INSTANCE_START_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "config_id": {
            "type": "string",
            "minLength": 1,
            "description": "The New-HoloDeckConfig configID from holodeck.config.apply.",
        },
        "instance_id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9]{1,8}$",
            "description": "8-char alphanumeric InstanceID for New-HoloDeckInstance.",
        },
        "version": {"type": "string", "enum": list(_DEPLOY_VERSION_ENUM)},
        "variant": {"type": "string", "enum": ["management_only", "vvf", "full"]},
        "site": {"type": "string", "enum": ["a", "b"], "description": "Holodeck site (default a)."},
        "vsan_mode": {
            "type": "string",
            "enum": ["OSA", "ESA"],
            "description": "Nested vSAN mode (default OSA).",
        },
        "depot_type": {"type": "string", "enum": ["Offline", "Online", "CloudBuilder"]},
    },
    "required": ["config_id", "instance_id", "version"],
    "additionalProperties": False,
}

_INSTANCE_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instance_id": {
            "type": "string",
            "description": (
                "Optional InstanceID to scope Get-HoloDeckInstance to a single instance."
            ),
        },
    },
    "additionalProperties": False,
}

_ROUTER_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["running", "completed", "failed", "unknown"]},
        "current_step": {"type": ["string", "null"]},
        "started_at": {"type": ["string", "null"]},
        "updated_at": {"type": ["string", "null"]},
        "instance_id": {"type": ["string", "null"]},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "notes"],
    "additionalProperties": True,
}

_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


DEPLOY_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id="holodeck.config.apply",
        handler_attr="config_apply",
        summary=(
            "Apply a Holodeck deploy config (New-HoloDeckConfig, never -Default) "
            "+ import it (approval-gated)."
        ),
        description=(
            "Runs New-HoloDeckConfig (WITHOUT -Default -- the flag triggers a "
            "buggy internal Set-HoloDeckConfig) + Import-HoloDeckConfig on the "
            "HoloRouter to lay down a deploy configuration: VCF version, variant, "
            "external IP, master CIDR/VLAN, placement, and (9.x only) the offline "
            "depot connection. Depot params are REFUSED for a VCF 5.2.x version "
            "(5.2 uses Cloud Builder, no offline depot) -- the schema rejects the "
            "combination before the approval park. The vCenter deploy-target "
            "credentials are read on the appliance from its staged environment "
            "($env:VC_USER/$env:VC_PW); they are never a parameter and never "
            "appear in the result. safety_level=dangerous, requires_approval=True."
        ),
        parameter_schema=_CONFIG_APPLY_SCHEMA,
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="deploy-config",
        tags=("write", "deploy", "config", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_DEPLOY_CONFIG,
            "parameter_hints": {
                "version": "5.2.x -> Cloud Builder (omit depot); 9.x -> VCF Installer + depot.",
                "depot": "9.x only; credential_ref is a Vault path, never a plaintext password.",
            },
            "output_shape": (
                "{applied, config_id, deploy: {version, variant, ...non-secret echo}}. "
                "On a bound violation: {applied: false, error}."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.instance.start",
        handler_attr="instance_start",
        summary=(
            "Launch New-HoloDeckInstance detached after a fail-fast "
            "Connect-VIServer precheck (approval-gated)."
        ),
        description=(
            "Launches a nested-VCF deploy from a config already applied by "
            "holodeck.config.apply. FIRST runs a single-shot fail-fast "
            "Connect-VIServer precheck (reads the persisted config on the "
            "appliance, one attempt, ErrorAction Stop) so a stale/locked "
            "deploy-target credential can't storm-lock the SSO account via "
            "Holodeck's 4-retry-in-13s loop; on any precheck failure the "
            "instance is NEVER launched. On a green precheck, New-HoloDeckInstance "
            "runs under tmux new-session -d (survives the call returning) and the "
            "op returns immediately with the instance id + start marker. Poll with "
            "holodeck.instance.status. safety_level=dangerous, requires_approval=True."
        ),
        parameter_schema=_INSTANCE_START_SCHEMA,
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="deploy-lifecycle",
        tags=("write", "deploy", "lifecycle", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_DEPLOY_LIFECYCLE,
            "parameter_hints": {
                "config_id": "The configID returned by holodeck.config.apply.",
                "instance_id": "8-char alphanumeric; mirror the cluster name where possible.",
            },
            "output_shape": (
                "{started, detached, instance_id, config_id, precheck: {ok, ...}, "
                "start_marker: {tmux_session, log_path}}. On precheck fail: "
                "{started: false, precheck, error} and nothing is launched."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.instance.status",
        handler_attr="instance_status",
        summary="Read Get-HoloDeckInstance into a structured deploy-status envelope (safe).",
        description=(
            "Reads Get-HoloDeckInstance and maps it into a flat, deterministic "
            "envelope: status (running|completed|failed|unknown), current_step, "
            "started_at, updated_at, instance_id, notes. The flat status field is "
            "a runbook OperationCallVerify target and a deploy-in-progress Sensor "
            "assertion target. The benign VCF 5.2.x terminal quirk (final "
            "Sync-HolodeckComponents 'No route to host' -- VCF Operations absent in "
            "5.2) is surfaced as a structured note with status=completed, NOT a "
            "failure. safety_level=safe, no approval."
        ),
        parameter_schema=_INSTANCE_STATUS_SCHEMA,
        response_schema=_STATUS_RESPONSE_SCHEMA,
        group_key="deploy-status",
        tags=("read-only", "deploy", "status", "holodeck"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_DEPLOY_STATUS,
            "parameter_hints": {
                "instance_id": "Optional; omit to read the appliance's current/only instance.",
            },
            "output_shape": (
                "{status, current_step, started_at, updated_at, instance_id, notes[]}. "
                "status is the verify/Sensor target; notes[] carries the benign 5.2 quirk."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.router.patch",
        handler_attr="router_patch",
        summary=(
            "Idempotently verify-then-apply the three per-appliance HoloRouter "
            "patches (approval-gated)."
        ),
        description=(
            "Verify-then-applies the three mandatory per-appliance patches on a "
            "HoloRouter: C1 (bundle-count -eq 7 -> -ge 7), C2 (BroadcomBuildToken "
            "SecureString wrap) and C3 (Auth.psm1 Connect-VIServer PSCredential). "
            "For each patch it greps the applied marker (present -> already_applied), "
            "else greps the unpatched marker (present -> backup + sed + re-verify -> "
            "applied), else not_applicable. Idempotent: a second run reports all "
            "already_applied. On a 9.1 HoloRouter it runs VERIFY-ONLY (patches fixed "
            "upstream) -- greps but never seds. The patch strings are fixed constants "
            "(C2 references $env:brcm_build_token by NAME, never a token value) and "
            "the result carries only patch id/state/counts -- no token or credential "
            "reaches any governance surface. safety_level=dangerous, requires_approval=True."
        ),
        parameter_schema=_ROUTER_PATCH_SCHEMA,
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="deploy-patch",
        tags=("write", "deploy", "patch", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_DEPLOY_PATCH,
            "parameter_hints": {},
            "output_shape": (
                "{patched, verify_only, hr_version, patches: [{patch_id, label, "
                "state: already_applied|applied|not_applicable}]}."
            ),
        },
    ),
)
