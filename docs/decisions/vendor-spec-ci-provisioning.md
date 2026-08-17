# Vendor vCenter spec-shelf in CI — licensing record + provisioning runbook (decision)

**Status:** facts recorded; rationale stated; **decision routed for human
legal/maintainer signoff** (see "Signoff of record" below — deliberately not
filled by this ADR). The CI wiring ships **disarmed**: nothing is fetched until
a repository admin provisions the `SPEC_SHELF_TOKEN` secret, and provisioning
is gated on the signoff below.
**Date:** 2026-08-17
**Task:** [#2949](https://github.com/evoila/meho/issues/2949) (split out of
[#2944](https://github.com/evoila/meho/issues/2944) precisely because this is a
licensing + secret-provisioning decision, not a code-only change)

## The matter this records

Five CI test lanes validate MEHO's OpenAPI ingest and op_id reconciliation
against the **real** pinned vSphere OpenAPI specs:

- `backend/tests/acceptance/test_g07_vsphere_canary.py` — the G0.7 vSphere
  ingest + dispatch canary
- `backend/tests/integration/test_operations_ingest_vcenter.py` — real-spec
  `vcenter.yaml` ingest
- `backend/tests/integration/test_operations_ingest_vi_json.py` — real-spec
  `vi-json.yaml` ingest
- `backend/tests/acceptance/test_portgroup_audit_op_id_reconcile.py` — the
  #1602 read-side audit reconcile
- `backend/tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`
  — the #2944 write-composite `_SUB_OPS_*` real-spec reconcile guard

The specs (`vcenter.yaml` ~961 paths, `vi-json.yaml` ~2,195 paths) are
**Broadcom/VMware vendor-licensed** and deliberately **not** committed to this
public repo — they live in the operator's private spec-shelf (the
`evoila-bosnia/claude-rdc-hetzner-dc` consumer repo, `docs/vcenter-9.0/`; see
`backend/tests/acceptance/_vcenter_spec.py`). Consequence: every one of the
five lanes `pytest.skip()`s in CI, so a shape-correct-but-nonexistent path (a
typo, a renamed endpoint, a wrong API version) sails through CI and only fails
against a live vCenter.

#2949 wires a **secret-gated fetch** of the spec-shelf into
`.github/workflows/ci.yml` so the lanes run on same-repo PRs. Whether fetching
the vendor-licensed specs into an ephemeral CI job is within the license is a
**legal/maintainer call**. This ADR records the technical facts and rationale
and **routes** that question — it does not decide it. Precedent for this
pattern: [`shipped-spec-provenance.md`](shipped-spec-provenance.md) and
[`jsonflux-license.md`](jsonflux-license.md).

## What CI does with the specs (technical facts)

- **Source.** The private repo `evoila-bosnia/claude-rdc-hetzner-dc`
  (verified private), directory `docs/vcenter-9.0/` only, fetched via
  `actions/checkout` with a cone-mode sparse checkout + `filter: blob:none` —
  the runner materialises the shelf directory (~18 MB), not the whole repo.
- **Destination.** The ephemeral runner workspace of an ARC (Actions Runner
  Controller) pod on the internal `rke2-ci` cluster. The pod — including the
  workspace and the fetched specs — is destroyed when the job ends.
- **Use.** Read-only input to pytest: the lanes parse the specs
  (`operations.ingest.parse_openapi`) and assert that every declared op_id /
  sub-op path exists in the real spec. Nothing transforms or re-publishes the
  content.
- **Never redistributed.** The specs are never committed to the public repo
  tree; never uploaded as build artifacts (the `upload-artifact` steps in
  `ci.yml` enumerate exact coverage files only — `backend/coverage.xml`,
  `cli/coverage.out`); never printed to logs (the workflow's verify step
  prints file *presence* only; the tests print pass/skip/fail counts).
  `persist-credentials: false` keeps the checkout token out of the clone's
  git config.
- **Audience.** Same-repo PRs, pushes to `main`, and merge-queue runs only.
  Fork PRs never execute these jobs (job-level
  `head.repo.full_name == github.repository` guard) **and** GitHub withholds
  Actions secrets from fork-PR runs regardless — two independent layers.
  Dependabot-triggered runs read the separate Dependabot secret store, where
  this secret is deliberately not mirrored, so the fetch step skips there too.
- **Degraded mode.** When the secret is absent — the shipped state until the
  signoff below is completed — the fetch step skips and the five lanes
  `pytest.skip()` exactly as they did before #2949. The wiring is inert
  without a human provisioning act.

## Rationale (why fetch at all)

1. **The guard only has teeth against the real spec.** The in-tree fixture
   variants of these lanes prove op_id *shape*, not real-spec *path
   existence* (the gap #2944 documented). Only the vendor's own
   `vcenter.yaml` / `vi-json.yaml` can answer "does this path actually exist
   in vCenter 9.0?" on every PR.
2. **Strictly weaker exposure than the already-recorded shipped-spec
   question.** [`shipped-spec-provenance.md`](shipped-spec-provenance.md)
   records the reproduction of vendor *interface names* inside shipped
   Apache-2.0 files. This ADR's mechanism reproduces **nothing**: the vendor
   content stays in the private shelf and an ephemeral runner; the public
   repo gains only a fetch instruction and a secret name.
3. **Minimal credential.** The secret is a fine-grained PAT scoped to
   read-only Contents on the single private shelf repo — it can read that
   repo and do nothing else anywhere.

## Known findings at recording time (activation prerequisite)

Running the five lanes locally against the real shelf (2026-08-17, the
#2949 verification run) surfaced real findings — the exact defect class the
guard exists to catch, anticipated verbatim by #2944 ("a shelf-backed red is
the guard surfacing a real finding … not a reason to withhold the guard"):

- `test_portgroup_audit_op_id_reconcile.py` — **red**: `_OP_LIST_DVS =
  "GET:/vcenter/network/distributed-switches"`
  (`connectors/vmware_rest/composites/_read.py`) is not served by the real
  `vcenter.yaml` (the only distributed-switch paths are under
  `/vcenter/namespace-management/…`).
- `test_connectors_vmware_rest_composites_l2_ingest_reconcile.py` — **red**:
  ten `_SUB_OPS_*` REST op_ids in `composites/_write.py` reference paths the
  real `vcenter.yaml` does not serve:
  `GET:/vcenter/cluster/{cluster}/drs/recommendations`,
  `GET:/vcenter/cluster/{cluster}/host`, `GET:/vcenter/vm/{vm}/snapshot`,
  `PATCH:/vcenter/host/{host}/maintenance?action=enter`,
  `PATCH:/vcenter/host/{host}/maintenance?action=exit`,
  `PATCH:/vcenter/vm/{vm}/network`, `POST:/vcenter/host/{host}?action=patch`,
  `POST:/vcenter/network/dvs/{dvs}?action=remove_host`,
  `POST:/vcenter/vm-template/library-items?action=deploy`,
  `POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert`.
- The other three lanes are green against the real shelf (the vi-json
  ingest lane after #2949's one-line lane repair — its pre-existing
  `Path.startswith` bug had made it un-runnable, masked by the permanent
  skip).

**Why this gates provisioning:** both red lanes run inside **required**
merge-gate jobs (`Python (ruff + mypy + pytest)` /
`Python (integration testcontainers)`). Provisioning the secret before the
product-code findings are fixed would turn CI red for **every** PR. The
follow-up fix (re-point the affected composites at op_ids the pinned specs
actually serve — likely their vi-json / vim analogues) must merge first.

## Provisioning runbook (repo admin; do NOT execute before signoff)

1. **Complete the Signoff of record below** (or link the attestation
   comment from #2949). Provisioning the secret is the act that arms the
   fetch; it must not precede the signoff.
2. **Confirm the "Known findings" above are fixed on `main`** (the
   follow-up task re-pointing the affected `_SUB_OPS_*` / `_OP_LIST_DVS`
   op_ids), then pre-verify the **full** five-lane set locally against
   the shelf, on a machine with Docker available (the g07 canary's
   dispatch tests need containers — a sandbox without Docker
   under-verifies exactly the subset CI will run):

   ```bash
   cd backend && MEHO_CONSUMER_DOCS_ROOT=<shelf>/docs uv run pytest \
     tests/acceptance/test_g07_vsphere_canary.py \
     tests/integration/test_operations_ingest_vcenter.py \
     tests/integration/test_operations_ingest_vi_json.py \
     tests/acceptance/test_portgroup_audit_op_id_reconcile.py \
     tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py
   ```

   This must be green before the secret exists — both reconcile lanes
   sit inside required merge-gate jobs, so every PR's checks go red the
   moment the secret lands otherwise.
3. **Mint the credential** — recommended shape: a **fine-grained PAT**
   - Resource owner: `evoila-bosnia`
   - Repository access: *Only select repositories* →
     `evoila-bosnia/claude-rdc-hetzner-dc`
   - Permissions: **Contents: Read-only** (Metadata: Read is implied)
   - Expiration: per org policy; record the rotation date. On rotation,
     repeat step 4 with the new token — no workflow change needed.

   Alternative shape: a read-only **deploy key** on the shelf repo. That
   requires a one-line workflow change (swap `token:` for `ssh-key:` in the
   two "Checkout vendor vCenter spec-shelf" steps of `ci.yml`) and SSH
   egress from the runner pool; the PAT/HTTPS shape is the shipped default
   because the runners' 443 egress is already proven.
4. **Set the secret** on the public repo (interactive paste — keep the token
   out of shell history):

   ```bash
   gh secret set SPEC_SHELF_TOKEN --repo evoila/meho
   ```

5. **Verify activation.** Re-run CI on any open same-repo PR (or push a
   no-op branch). Expect:
   - the "Checkout vendor vCenter spec-shelf" and "Verify spec-shelf
     provisioned the pinned vCenter specs" steps run (not skipped) and pass
     in both `Python (ruff + mypy + pytest)` and
     `Python (integration testcontainers)`;
   - the five lanes above **run, not skip** — the skip reason string
     `"vCenter OpenAPI spec not configured"` (and the
     `test_operations_ingest_vcenter.py` variant `"vcenter.yaml
     unavailable"`) no longer appears in the pytest output.
6. **Do not mirror the secret into the Dependabot secret store.** Dependabot
   PRs gain nothing from the spec lanes; least exposure wins.

## Signoff of record

**This section is deliberately left for a human (legal / maintainer) to
complete.** This ADR records the facts above and routes the question —
*fetching the Broadcom/VMware vendor-licensed vSphere OpenAPI specs from the
operator's private spec-shelf into ephemeral CI jobs, never committed, never
in artifacts or logs* — for the signoff of record. It does **not** assert a
legal conclusion.

- **Reviewer:** _(name / GitHub handle — to be completed)_
- **Date:** _(to be completed)_
- **Determination:** _(to be completed — e.g. an attestation that the
  ephemeral CI use recorded above is acceptable under the operator's vendor
  license; or a request for changes)_
- **Attestation link:** _(comment / issue URL — to be completed)_

Until this section is completed, the question stands as **recorded and
routed**, not resolved — and the `SPEC_SHELF_TOKEN` secret must not be
provisioned.

## Consequences

- The five real-spec lanes light up together the moment the secret exists;
  until then CI behaviour is byte-for-byte today's (lanes skip). Because two
  lanes are known-red against the real shelf (see "Known findings" above),
  activation is sequenced: signoff → findings fix on `main` → secret.
- A shelf layout move or sparse-checkout typo after activation fails the job
  loudly (the verify step) instead of silently regressing to lane-skip.
- The shelf's `docs/` also carries other vendor spec directories (`nsx-9.0/`,
  `sddc-manager-9.0/`, …). The same checkout could serve future sibling
  canaries by widening the `sparse-checkout` list — deliberately not done
  here (#2949's DoD is the vCenter lanes). The G4.1 consumer-kb canary does
  **not** light up: it resolves `<docs-root>/kb`, which does not exist under
  the shelf's `docs/` (the consumer's `kb/` is a repo-root sibling).
- Rotation is a secret-value swap (runbook step 3); revocation (delete the
  secret) restores the degraded skip mode with no code change.

## References

- Task [#2949](https://github.com/evoila/meho/issues/2949); parent context
  [#2944](https://github.com/evoila/meho/issues/2944) (the reconcile guard
  whose real-spec mode this activates).
- Resolver contract: `backend/tests/acceptance/_vcenter_spec.py` (env-var
  priority; skip semantics).
- Workflow wiring: `.github/workflows/ci.yml` — the two "Checkout vendor
  vCenter spec-shelf" steps and the `SPEC_SHELF_TOKEN_PRESENT` /
  `MEHO_CONSUMER_DOCS_ROOT` job env rows; operator-facing summary in
  [`docs/codebase/devops.md`](../codebase/devops.md).
- Precedent ADRs: [`shipped-spec-provenance.md`](shipped-spec-provenance.md)
  (signoff-of-record pattern), [`jsonflux-license.md`](jsonflux-license.md).
