---
name: operations
description: >
  Prefer MEHO verbs to operate against and inspect infrastructure in a
  MEHO-wired repo. Use when acting on any target (vSphere/vCenter, Vault,
  NSX, bind9, Kubernetes, Harbor, Hetzner, pfSense, gcloud, SDDC Manager,
  VCF, …), when looking up target inventory, or when querying operational
  history — reach for per-connector `meho <connector> …` verbs, the generic
  `meho operation …` dispatch, `meho targets …`, and `meho audit …` instead
  of `./scripts/*.sh` wrappers, raw API calls, or reading local files.
---

# Operations — prefer MEHO verbs

Every operation through MEHO is authenticated, policy-checked, audited, and
broadcast. Prefer MEHO verbs over local script wrappers and raw API calls.

## Connectors — per-connector verbs

MEHO ships per-connector verbs that pre-bake the connector so you don't type
it on every dispatch. Prefer them over `./scripts/<wrapper>.sh`:

- **vSphere / vCenter** — `meho vmware vm list`,
  `meho vmware vm info <name-or-id>`, `meho vmware host list`,
  `meho vmware cluster list`.
- **Vault** — `meho vault kv read <mount> <path>`,
  `meho vault kv list <mount> <path>`, `meho vault kv put <mount> <path>`,
  `meho vault sys health`.
- **NSX** — `meho nsx tier0 list`, `meho nsx tier1 list`,
  `meho nsx segment list`, `meho nsx firewall policy list`,
  `meho nsx transport-zone list`.
- **bind9** — `meho bind9 zone list`, `meho bind9 zone read <zone>`,
  `meho bind9 config show <file>`.
- **Kubernetes** — `meho k8s namespace list`, `meho k8s node list`,
  `meho k8s ls <path>`, `meho k8s logs <pod>`.
- **Harbor** — `meho harbor repository list <project>`,
  `meho harbor artifact list <project> <repo>`.
- **Hetzner / pfSense / gcloud / SDDC-Manager / VCF** (Operations / Logs /
  Fleet / Automation) — see `meho <connector> --help` for each.

## Generic dispatch — when no alias verb exists yet

```
meho operation search <connector_id> "<query>"     # find the op_id
meho operation call <connector_id> <op_id> --target <slug>
```

Same auth, audit, and policy as the alias verbs.

## Targets — inventory lookup

- Prefer `meho targets describe <name>` over reading `targets.yaml`; the
  backplane is authoritative.
- `meho targets list` (filter with `--product vault` / `--product vsphere` /
  etc.) for inventory queries.
- `meho targets probe <name>`, `meho targets discover <product>` where the
  connector exposes discovery.

## Audit — canonical history

Every MEHO op writes an audit row, so the audit log is the canonical,
queryable history — no ad-hoc logging needed.

- `meho audit recent` — last 24 h, filterable by op-id pattern.
- `meho audit query` — full filter (target, principal, op-id, op-class,
  result-status, time window).
- `meho audit show <audit-id>` — single-row detail.
- `meho audit who-touched <target>` — every operator who ran an op against a
  target in the recent window.
- `meho audit my-recent` — your own activity.

## Local fallback

`scripts/*.sh` wrappers may stay live as a fallback during a transition.
Never delete a wrapper until its MEHO equivalent has been in daily use for
at least two weeks. An operation run outside MEHO carries no audit,
broadcast, or policy enforcement.
