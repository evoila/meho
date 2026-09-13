# VCF API compatibility evidence and connector boundaries

Task [#3555](https://github.com/evoila/meho/issues/3555) establishes the
qualification record for the VCF 5.x through 9.1.1 vendor API surface. It does
not add a connector, change resolver selection, ingest a catalog, or perform a
live probe.

The machine-readable source is
[`../compatibility/vcf-api-contract-manifest.yaml`](../compatibility/vcf-api-contract-manifest.yaml).
It enumerates every VCF BOM row and records component version/build as known or
`unknown`; an unknown field is an acquisition task, never a compatible default.
The VCD and Holodeck entries are independent adjacent products, not VCF BOM
claims.

## Evidence discipline

`registered` says only that a current connector class advertises a version
range. `spec-fixture-qualified` says a pinned catalog supports a named catalog
contract. `live-verified` requires a safe observed call. `unqualified` is used
where no such claim is defensible. These levels do not imply one another.

Raw vendor documents are not committed here. The private consumer spec shelf
holds restricted vSphere, VI JSON, VIM, and manager-served NSX documents; the
public manifest carries their source, hash, counts, and provenance. This follows
the [spec-reconcile guard standard](../decisions/spec-reconcile-guards-standard.md)
and its [CI provisioning decision](../decisions/vendor-spec-ci-provisioning.md).
Public Apache-2.0 `vmware/vcf-api-specs` documents are pinned by commit and
hash, but are also summarized instead of copied.

The exact 9.0 and 9.1 tags are `85151f6b` and `3949fc33`. The official
`31b01a6` 9.1.1 upstream snapshot is untagged; it is catalog evidence only,
until it is matched to a release ZIP or a live appliance. There is no static
8.0 U1/U2/U3 vSphere or NSX 4.1/4.2 catalog pin yet, so their full 8-to-9 and
4-to-9 deltas remain explicit gaps.

## Catalog comparison

Run the offline comparator only on two artifacts known to be from the same
service lineage:

```bash
cd backend
uv run python scripts/diff_vcf_api_contract_catalogs.py \
  --before <pinned-9.0-spec> --after <pinned-9.1-spec> --same-lineage \
  --output /tmp/<service>-9.0-to-9.1.json
```

It resolves transitive local `$ref` values, path-level inherited parameters,
inherited security, request/response closures, deprecated/vendor feature
markers, and service base paths. It separates description-only churn from
contract change. Without `--same-lineage`, it returns
`artifact-lineage-unknown` and does not call a route removed. That distinction
is required for the 9.0 Logs API v2 versus 9.1 log-management artifact.

Raw path or operation-id additions/removals are candidates for review, not an
automatic wire incompatibility. The SDDC Manager API independently versions
resources; VCF 5.0 changed Swagger path-variable names, and VCF 5.2 deprecated
`ipAddress` in favor of `fqdn`. A semantic alias or parameter migration needs
the comparator output plus vendor documentation before it becomes a resolver or
operation gate.

Known catalog facts are recorded in the manifest. Examples: SDDC 9.0 to 9.1
adds 48 operations and removes none; vSphere REST adds 101 and removes 9 while
VI JSON adds 48 and removes none; the untagged 9.1.1 snapshot adds 6 SDDC,
31 Operations, and 1 Installer operation. Schema counts and raw named changes
are not a substitute for transitive closure comparison.

## Descriptor and target contract

The dispatcher obtains an operation descriptor from caller-supplied
`connector_id` before it resolves a target's execution class. A resolver-only
change cannot prove that the descriptor's catalog, parameter schema, auth, and
feature preconditions match the resolved class. The future guard therefore
must evaluate the selected descriptor against the resolved target/profile before
the connector handler runs.

For vSphere this creates two catalog profiles: normalized `8.0.1`, `8.0.2`,
and `8.0.3` (VCF 5's U1/U2/U3 floor) and 9.x. Coarse `8.0` remains
unqualified until a target update/capability supplies the update level. The
guard must permit a shared handler where its operation contract is compatible;
it must not be an exact version-label ban. It must reject a known 8.x target
using a 9.x-only descriptor. VI JSON itself is supported from vCenter 8.0 U1
at `/sdk/vim25/<release>`; the known 8.x failure is MEHO's `/api` endpoint
assumption, so the future gate must model service base and release separately.
`vmware_rest/_mount.py` already records that `/api` is a 9.x convenience, and
the typed vmomi path already adapts in `connector.py`. The gate must also keep
vCenter and standalone ESXi target flavor separate: standalone ESXi does not
serve VI JSON. That is a capability boundary, not a patch-specific class fork.

NSX needs separate complete Manager and Policy profiles for 4.1/4.2 and 9.
KB 377083 documents removal of deprecated MP Logical APIs in NSX 9. The ten
current typed GETs do not consume those APIs, which narrows the immediate
regression subset only. It does not qualify the rest of either catalog or erase
the future profile boundary.

All connector classes still expose the stable MEHO meta-tool surface. Generic
and typed operations remain first-class `endpoint_descriptor` rows and retain
policy, audit, approval, and JSONFlux handling.

## Version and feature boundaries

Keep the five existing legacy/modern pairs: SDDC 5/9, VCFA 8/9, Fleet legacy
8 plus modern 9 overlap, vROps 8/9, and vRLI 8/9. Fleet specificity resolves
before preference. vROps 8 uses `vRealizeOpsToken`; 9 uses `OpsToken`. VCF
Operations for Logs uses the same Bearer family across the documented classes,
but the 9.0 and 9.1 documents are differently named service artifacts, so no
removal is inferred without lineage evidence.

The Installer currently advertises `>=9.1,<10.0`. A 9.0 spec exists, but that
does not lower the connector floor until a focused contract test proves it.
VCF Automation Native Public Cloud endpoints in 9.1.1 are disabled by default;
that is a target capability/configuration gate, not a class fork. VCD API 38.0
is evidenced only for 10.5/10.6 and needs negotiation or a narrow range.
Holodeck has no VCF range.

Feature-state gates must be concrete. For example, an SDDC 9.1.0.0400 precheck
could not parse a decoupled-UI target reporting `9.1.1.0.25713928` (KB 455225),
while 9.1.1 fixes that format edge; this is not grounds for a global build
selector. VCF Operations 9.1.1 password-policy behavior depends on Salt
infrastructure, licensing, and integration state (KB 452461). SDDC 9.1.1
Microsoft CA integration requires a SAN certificate (KB 454242).

## Sequenced implementation packages

1. **Catalog acquisition and diff artifacts.** Pin exact 8.x and NSX 4.x
   documents on the private shelf, run the comparator for each same-lineage
   pair, and record the generated output beside shelf manifests. Depend on no
   runtime task. Verify with the comparator and
   `uv run python scripts/validate_vcf_api_contract_manifest.py --manifest ../docs/compatibility/vcf-api-contract-manifest.yaml`.
2. **Descriptor-to-target contract check.** Add catalog profile metadata and
   pre-dispatch compatibility tests for a caller descriptor and target class.
   Prove 8.0.1/2/3 versus 9.x accepts compatible shared handlers and rejects
   incompatible descriptor paths. This is independent of #3047 and #3518.
   Verify with `uv run pytest tests/test_connectors_resolver.py -q` and focused
   dispatcher tests.
3. **Versioned catalog profiles and operation gates.** Implement vSphere
   REST/VI JSON 8.x/9.x and NSX 4.x/9 profiles, then add operation/parameter/
   capability gates from the comparator output. Keep shared transport and
   handlers unless a proved auth, transport, or semantic boundary requires a
   class fork. Verify focused profile, ingest, and dispatcher suites.
4. **Management-service evidence and narrow deltas.** Add per-service 9.0,
   9.1, and matched 9.1.1 profiles for SDDC, Automation, Fleet, Operations,
   Logs, and Installer. Coordinate SDDC 9.1 reads with #3518 and modern Fleet
   authentication/ingest with #3047; neither task is reopened. Verify with
   `uv run pytest tests/test_connectors_fleet_dual_impl_resolution.py tests/test_connectors_sddc_vcf5_dual_impl_resolution.py -q` plus service-specific suites.
5. **Estate packaging hand-off.** Place estate catalog fixtures and generated
   artifacts according to #3345 and the connector placement rubric. Keep this
   public manifest technical and free of customer, commercial, or raw licensed
   material. Verify public/private package boundaries and the corresponding
   shelf reconcile lanes.

## References

- [Broadcom KB 314608](https://knowledge.broadcom.com/external/article/314608),
  [KB 327207](https://knowledge.broadcom.com/external/article/327207),
  [KB 377083](https://knowledge.broadcom.com/external/article/377083),
  [KB 455225](https://knowledge.broadcom.com/external/article/455225),
  [KB 452461](https://knowledge.broadcom.com/external/article/452461), and
  [KB 454242](https://knowledge.broadcom.com/external/article/454242).
- [vSphere 9.1.1 changelog](https://developer.broadcom.com/xapis/vsphere-automation-api/9.1.1/changelog/)
  and [VCF Operations 9.1.1 changelog](https://developer.broadcom.com/xapis/vcf-operations-api/9.1.1/changelog/).
- Existing connector documents: [vSphere](connectors-vmware-rest.md),
  [NSX](connectors-nsx.md), [SDDC](connectors-sddc-manager.md),
  [Automation](connectors-vcf-automation.md), [Fleet](connectors-fleet-lcm.md),
  [Operations](connectors-vcf-operations.md), [Logs](connectors-vcf-logs.md),
  and [Installer](connectors-vcf-installer.md).
