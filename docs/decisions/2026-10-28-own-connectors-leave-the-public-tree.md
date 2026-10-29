# evoila's own connectors leave the public repository (decision)

**Status:** decided by the program owner on 28 October 2026
**Date:** 2026-10-28
**Applies to:** the removal Task [#3345](https://github.com/evoila/meho/issues/3345)
and the connector packages Initiative [#5060](https://github.com/evoila/meho/issues/5060)
**Builds on:** the start-up check of connector folders
([#5034](https://github.com/evoila/meho/issues/5034),
[connector-packages.md](../codebase/connector-packages.md))

## The decision in short

- evoila's own connectors leave this public repository. They move to a
  private connectors repository. Each connector family there is one
  wheel (a built Python package file).
- This repository keeps the backplane, the connector kit, the core
  connectors and the commodity connectors.
- **First wave:** the 19 VMware connector folders, plus `salt`.
  **Second wave:** `azure`. **Fortinet** connectors are built only in
  the private repository.
- The public image stays. It runs without these connectors. evoila builds
  an overlay image: the public image, pinned by digest, plus the private
  wheels. evoila signs it and runs it in the lab and in the appliance.
- Public history stays. One plain commit removes the folders. No history
  rewrite, no force-push, and the tags stay as they are.
- Module paths do not change. A wheel adds folders under
  `meho_backplane/connectors/`, as described in
  [connector-packages.md](../codebase/connector-packages.md).

## What stays public

- **The backplane and the connector kit.** This is the `meho-backplane`
  package itself: `connectors/base.py`, `_shared/` without the
  VMware-only modules, `adapters/`, the allowlist check, the overlay
  build and its docs.
- **Core connectors.** The backplane imports them, or they are MEHO's own:
  `vault`, `keycloak`, `secret`, `mail`, `net`, `targets`, `topology`,
  `audit`, `migration` and `meho_automation`.
  - Vault stays public because core code imports it in many places: the
    dispatcher, the preview, the API, the login and the console.
  - Keycloak stays public because it is MEHO's own login.
- **Commodity connectors** (common products many operators run):
  `argocd`, `bind9`, `gcloud`, `github`, `harbor`, `hetzner_robot`,
  `hyperv`, `kubernetes`, `linux`, `loki`, `mongodb`, `msad`, `mssql`,
  `pfsense`, `postgres`, `prometheus`, `proxmox`, `rabbitmq`, `rke2`,
  `tempo`, `windows_dns`, `winsrv` and `wsfc`.

## What moves

| Wave | Folders | Notes |
|---|---|---|
| 1 (VMware wheel) | `vmware_rest`, `nsx`, `sddc_manager`, `sddc_vcf5`, `vcf_automation`, `vra8`, `vcf_fleet`, `fleet_lcm`, `vcf_installer`, `vcf_logs`, `vrli8`, `vcf_operations`, `vrops8`, `vcd`, `holodeck`, `hcx`, `srm`, `vr`, `vro` | Plus one VMware shared folder for the VMware-only modules of `_shared/`, the three VMware profile files, their tests and their docs pages. |
| 1 (VMware wheel) | `salt` | `salt-raas` is the API of VMware Aria Automation Config, so it moves with VMware. |
| 2 | `azure` | After the VMware wave. |
| Private only | Fortinet | Never built in this repository. The open FortiGate pull request #5169 is not merged here. |

## The order

The order is forced by two checks:

- The start-up check (#5034) stops the start when a folder was not
  installed by a listed wheel. So copying folders into an image does not
  work. Only wheels work.
- The overlay build (#5035) refuses a wheel that writes into MEHO's own
  files. Today the VMware folders are MEHO's own files.

So the steps are:

1. **Prepare the public tree.** Core code stops importing the VMware
   REST module (`operations/_errors.py` imports `vmware_rest.soap`). The
   VMware-only modules leave `_shared/`. Examples in core code use public
   connectors.
2. **Copy into the private repository** at one pinned public commit. Its
   CI tests against a pinned public tag.
3. **Hold VMware connector work** from the start of the copy until the
   removal merges. Otherwise each change must be made twice.
4. **Remove on `main`** (#3345). Every push to `main` publishes a signed
   `:sha-<commit>` image.
5. **Build the overlay** from that `:sha-<commit>` image. Start it, check
   that it lists all VMware connector ids, sign it, and run it in the lab.
6. **Release.** The release tag comes only after the overlay starts in the
   lab.

**Timing:** the removal merges right after a Friday release. It ships in
the next Friday release, once the overlay image starts in the lab. So the
overlay and the lab roll have one week.

## Who builds, signs and reviews

- The private repository's CI builds the overlay image. It signs it with
  its own workflow identity and pushes it to a private registry.
- The lab session rolls the lab to the overlay image.
- The appliance and the Linux download take the overlay image and its
  signer as an input.
- A person reviews each package version before it is signed (#5039): the
  program owner plus one colleague (the name follows). A bot approval
  does not count.

## The two tiers

These are the tiers of #5060:

- **Tier 1, open source:** anyone builds connectors against the kit and
  runs them in their own build of MEHO.
- **Tier 2, the appliance:** only evoila's own connectors, reviewed by a
  person and signed. VMware, Azure and Fortinet are tier 2.

## The placement rule

The internal connector placement rule changes to match:

- A hyperscaler API stays public, except Azure. Azure is evoila's own and
  moves after VMware. Google Cloud stays public.
- The VMware list has 19 folders now: `hcx`, `srm`, `vr` and `vro` join.
  `salt` moves with them.
- Fortinet connectors are evoila's own.

## What this changes

- The earlier plan to copy connector folders into the image is replaced by
  the wheel and overlay way.
- The plain public image no longer has the VMware connectors after the
  removal release. Who needs them runs the overlay image. The CHANGELOG
  entry of the removal release carries the steps.
- The Fortinet issues of this repository move to the private repository.

## Not decided here

- The CLI verb folders in `cli/internal/cmd/` (for example `vmware`,
  `nsx`, `holodeck`) stay public for now. That is its own later decision.
- `entra` (Microsoft Entra ID) landed on 28 October, after this decision
  was taken. Where it goes is still open.

## Not proven yet

- Whether the whole public test setup runs from a pinned public tag in the
  private CI. A source checkout counts every folder inside it as built in,
  so the copied folders load there.

## Words used here

- **Wheel:** a built Python package file.
- **Overlay image:** a new image built from the public MEHO image, with
  connector packages added.
- **Allowlist file:** a file the overlay build writes. It lists the
  connector packages the image may load.
