# Shipped-spec provenance — derivative-work / interoperability record (decision)

**Status:** facts recorded; rationale stated; **decision routed for human legal/maintainer signoff** (see "Signoff of record" below — deliberately not filled by this ADR)
**Date:** 2026-06-22
**Goal:** [#1964](https://github.com/evoila/meho/issues/1964) — config-driven executable connectors
**Surfaced by:** [#1976](https://github.com/evoila/meho/issues/1976) (PR [#2023](https://github.com/evoila/meho/pull/2023)), per the [#1966](https://github.com/evoila/meho/issues/1966) initiative body's request to "flag for a SECURITY/legal glance rather than assert as a pure non-issue"
**Task:** [#2036](https://github.com/evoila/meho/issues/2036) (this ADR)

## The matter this records

[#1976](https://github.com/evoila/meho/issues/1976) (PR [#2023](https://github.com/evoila/meho/pull/2023))
ships MEHO-authored minimal OpenAPI specs and `ExecutionProfile`s for the
`vmware/9.0` and `sddc/9.0` catalog rows as Apache-2.0 package data, so the
catalog-driven ingest path works without an operator fetching a raw spec off a
live appliance and uploading it. To dispatch against the real appliance, those
specs **copy the vendor's API path / parameter / field names verbatim**. That
raises a derivative-work / copyright question.

This ADR is the **documentation + routing** deliverable. It records the
technical facts and the interoperability rationale and routes the question to a
human (legal / maintainer) for the signoff of record. **It does not assert a
legal conclusion** — that conclusion is the human signoff line below. The repo
already has the right pattern for exactly this kind of provenance record:
[`docs/decisions/jsonflux-license.md`](jsonflux-license.md) (a license-chain
provenance doc carrying a "signoff of record") plus the top-level
[`NOTICE`](../../NOTICE).

> **For the MEHO-authored minimal specs/profiles, what vendor-originated
> material is and is not reproduced, why the reproduced part is reproduced, and
> under what license do the MEHO-authored files ship — and from whom does the
> derivative-work / interoperability call require signoff?**

## What is copied, and what is not

The distinction the per-file headers already draw (and which this ADR
consolidates rather than re-states) is between **functional interface elements**
and **expressive content**.

### Copied — functional interface elements only

- **API path templates** (e.g. `/api/vcenter/cluster`, `/v1/releases/system`).
- **Parameter names** (query / path / header parameters the appliance accepts).
- **Field names**, including the response envelope keys the dispatcher must
  read by name (e.g. the SDDC Manager `elements[]` pagination envelope key).

These are copied **verbatim** for one reason only: the dispatcher must address
the real appliance using the appliance's own names. A renamed path or field
would not reach the appliance. See the per-file rationale in
[`vmware_rest_minimal.yaml`](../../backend/src/meho_backplane/operations/ingest/specs/vmware_rest_minimal.yaml)
("SCOPE … Paths and params copy the vendor's verbatim names because the
dispatcher must address the real appliance with them") and
[`sddc_manager_minimal.yaml`](../../backend/src/meho_backplane/operations/ingest/specs/sddc_manager_minimal.yaml)
("Paths, params and the `elements[]` pagination envelope copy the vendor's
verbatim names because the dispatcher must address the real appliance").

### Not copied — vendor expressive content and full surface

- **No vendor spec prose** — the `description` / `summary` text on every
  operation is **vendor-neutral, MEHO-authored** prose, not the vendor's.
- **No vendor examples.**
- **No full vendor surface.** The shipped specs are the read-only (GET) subset
  MEHO actually surfaces — vCenter ~9 inventory reads under `/api`; SDDC Manager
  ~9 inventory + lifecycle reads under `/v1` — mirroring the curated ops in the
  typed connectors (`connectors/vmware_rest/`, `connectors/sddc_manager/`). The
  full vendor specs (vSphere `vcenter.yaml` ~961 paths + `vi-json.yaml` ~2,195
  paths; the full SDDC Manager API) are **not redistributed**. They stay
  `upstream` provenance pointers in the catalog row `notes`
  ([`catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml))
  for a full-surface re-ingest off the appliance via the explicit-quadruple
  `--spec` shape.

The "MEHO does not redistribute" stance in the catalog comments is scoped to the
`upstream` field — it is a **vendor-spec-redistribution** stance (MEHO points at
where the vendor hosts the full spec; operators/CLI fetch it directly), not a
no-MEHO-specs stance. The MEHO-authored minimal specs are a separate, additive
on-ramp and are governed by this ADR.

## The interoperability rationale

The reproduced material is the **minimal extent necessary** for
interoperability:

1. **Necessity.** A connector that dispatches REST calls to a specific vendor
   appliance must use that appliance's real path templates, parameter names, and
   response field names. These are not a design choice MEHO is free to make
   differently — they are the appliance's published interface contract. The
   dispatcher cannot address the appliance by any other names.
2. **Minimality.** Only the names of the read operations MEHO surfaces are
   reproduced, mirroring the already-curated typed-connector op sets. The
   expressive layer a spec normally carries — operation prose, examples, the
   full operation surface — is either MEHO-authored (descriptions) or omitted
   (examples, full surface). Nothing beyond the functional interface elements is
   reproduced.
3. **Provenance preserved.** The full vendor specs remain attributed `upstream`
   pointers, so the relationship between the minimal MEHO-authored subset and the
   authoritative vendor source stays auditable and the full surface is never
   redistributed.

## License posture on the MEHO-authored files

Every shipped spec and profile carries the repo-standard two-line header:

```yaml
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
```

This is consistent with the rest of `evoila/meho` (the same header convention
verified across the source tree). The four files in scope:

- `backend/src/meho_backplane/operations/ingest/specs/vmware_rest_minimal.yaml`
- `backend/src/meho_backplane/operations/ingest/specs/sddc_manager_minimal.yaml`
- `backend/src/meho_backplane/connectors/profiles/vmware_rest_minimal.yaml`
- `backend/src/meho_backplane/connectors/profiles/sddc_manager_minimal.yaml`

The Apache-2.0 SPDX header + evoila copyright on these files asserts authorship
of the **MEHO-authored content** (the file structure, the vendor-neutral
descriptions, the `ExecutionProfile` declarations). It does **not** purport to
claim copyright over the vendor's underlying API names. The headers are correct
and are out of scope for this ADR (no SPDX-header edits) — they are recorded here
only so the posture is on the record.

## Signoff of record

**This section is deliberately left for a human (legal / maintainer) to
complete.** This ADR records the facts above and routes the derivative-work /
interoperability question; it does **not** decide it. The matter is **not**
asserted here to be legally settled.

A maintainer or legal reviewer who has reviewed the facts and rationale above
fills in the attestation below:

- **Reviewer:** _(name / GitHub handle — to be completed)_
- **Date:** _(to be completed)_
- **Determination:** _(to be completed — e.g. an attestation that the
  verbatim reproduction of the functional interface elements, at the minimal
  extent recorded above, is acceptable for the shipped Apache-2.0 specs/profiles;
  or a request for changes)_
- **Attestation link:** _(comment / issue URL — to be completed)_

Until this section is completed, the question stands as **recorded and routed**,
not resolved.

## Consequences

- The provenance of the MEHO-authored shipped specs/profiles is consolidated in
  one place, cross-linking the per-file rationale rather than duplicating it.
- The [`NOTICE`](../../NOTICE) file points here for the shipped-spec provenance,
  the same way it points at [`jsonflux-license.md`](jsonflux-license.md) for the
  vendored-reducer license chain.
- No code, spec, or profile content changes; the SPDX/copyright headers on the
  shipped files are unchanged (they are correct); the "MEHO does not
  redistribute" `upstream`-field stance is unchanged.
- The legal/maintainer signoff is a separate human action, tracked by the
  "Signoff of record" section above; it is not asserted by this ADR.

## References

- Parent Goal [#1964](https://github.com/evoila/meho/issues/1964) — config-driven
  executable connectors.
- Surfaced by [#1976](https://github.com/evoila/meho/issues/1976) (PR
  [#2023](https://github.com/evoila/meho/pull/2023)); the
  [#1966](https://github.com/evoila/meho/issues/1966) initiative body called for
  a "SECURITY/legal glance".
- Precedent ADR: [`docs/decisions/jsonflux-license.md`](jsonflux-license.md).
- Top-level attribution file: [`NOTICE`](../../NOTICE).
- Per-file rationale (cross-linked, not duplicated):
  - [`operations/ingest/specs/vmware_rest_minimal.yaml`](../../backend/src/meho_backplane/operations/ingest/specs/vmware_rest_minimal.yaml)
    (header: WHY / SCOPE / CONSTRAINTS).
  - [`operations/ingest/specs/sddc_manager_minimal.yaml`](../../backend/src/meho_backplane/operations/ingest/specs/sddc_manager_minimal.yaml).
  - [`connectors/profiles/vmware_rest_minimal.yaml`](../../backend/src/meho_backplane/connectors/profiles/vmware_rest_minimal.yaml).
  - [`connectors/profiles/sddc_manager_minimal.yaml`](../../backend/src/meho_backplane/connectors/profiles/sddc_manager_minimal.yaml).
- Redistribution stance (scoped to the `upstream` field):
  [`operations/ingest/catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml)
  comments.
