# The security sheet of a release

Every release gets one short Markdown file for the customer's security
team: `meho-security-sheet-<tag>.md` on the GitHub Release (#4639, ADR
0009 "closed by default"). It holds measured facts, not promises:

1. the open ports, and who may talk to them;
2. the CIS results: RKE2's CIS profile and the appliance's CIS Ubuntu
   Level 1 scan;
3. the vulnerability scan of every image of the release;
4. the SBOM (the list of every package in the image);
5. how to check the signature of each download.

Two more files go next to it on the release: the SBOM
(`meho-sbom-<tag>.spdx.json`) and the full scan reports of every image,
as one JSON list (`meho-vulnerability-scan-<tag>.json`).

## Key files

| File | What it holds |
| --- | --- |
| `docs/security/security-sheet-template.md` | The text of the sheet, with `{{ name }}` places for the measured parts. |
| `scripts/release/security_sheet.py` | Fills the template from a data folder. Plain Python, no packages. |
| `.github/workflows/security-sheet.yml` | Collects the data of a tag, writes the sheet and attaches the three files to the release. |
| `.github/workflows/cli-release.yml` | Calls `security-sheet.yml` on every `v*` tag, in the job `security-sheet`, after `linux-download`. |
| `backend/tests/test_release_security_sheet.py` | Tests of the script with the real template. |

## Control flow

On a tag, `cli-release.yml` runs `release`, then `linux-download`, then
`security-sheet`. `linux-download` waits until `image.yml` and `chart.yml`
have published and signed the image and the chart, so they are there
when the sheet job starts. The job:

1. reads the digests of `ghcr.io/evoila/meho:<tag>` and
   `ghcr.io/evoila/meho-chart:<version>` with `regctl`;
2. checks with `cosign verify` that `image.yml` and `chart.yml` at the tag
   signed them, and with `cosign verify-blob` that `cli-release.yml`
   signed `SHA256SUMS` and `SHA256SUMS-mcpb` and `linux-download.yml`
   signed `SHA256SUMS-linux`. These are the commands the sheet gives the
   reader. A failed check stops the job, so no sheet says "checked" for a
   bad signature;
3. reads the SBOM from the image's signed SPDX attestation
   (`cosign verify-attestation --type spdxjson`), which `image.yml` made
   with syft;
4. lists every image of the release in `images.txt`, the same list as
   the form-2 download of the tag: it pulls the signed chart (and checks
   that its digest is the one cosign checked), renders it with
   `chart_image_refs`, adds the images of the storage helper's manifest
   and locks each image to a digest with `lock_images`. These functions
   come from `deploy/linux-server/packaging/` of the tag, with the Helm
   version pinned there. So the list holds the MEHO image, PostgreSQL,
   Keycloak, OpenBao, Valkey and the storage helper's images;
5. scans each image once, by digest, with Trivy: every severity, the scan
   exception file `.trivyignore` of the tag, and it never fails on a
   finding. Two tags of one image (the download also names a digest-pinned
   image `digest-<hex>`) are one scan. The sheet shows the totals, one row
   per image, and every CRITICAL and HIGH finding with its image. The
   MEHO image job's own scan already refused every CRITICAL or
   HIGH finding that has a fix;
6. writes `facts.json`, the port list (`firewall_port_list` of
   `deploy/linux-server/lib/firewall.sh` at the tag), and copies
   `.trivyignore` and `deploy/appliance/cis/exceptions.md` of the tag;
7. runs `security_sheet.py`, keeps the data folder as the workflow
   artifact `security-sheet-data` (90 days), and attaches the sheet, the
   SBOM and the scan report with `gh release upload --clobber`.

The job checks out the workflow's own commit for the script and the
template, and the tag for the data. So `workflow_dispatch` with an older
tag uses the newest template.

### The parts measured on a test install

Three parts come from a test install, not from the release job: a port
scan of a form-2 install, the result of RKE2's CIS profile on it, and the
CIS scan of the appliance. Until the release tests do this by themselves
(form 2: #4742; the appliance: #4816), the release manager adds them by
hand ([RELEASING.md](../RELEASING.md), step 5): the files go into a lab
folder, and `security_sheet.py --lab <folder>` writes the sheet again from
the data artifact.

| Lab file | What it is | How the sheet shows it |
| --- | --- | --- |
| `ports.txt` | a port scan from another machine (for example `nmap`) | as it is, in a code block |
| `rke2-cis.txt` | the CIS check of RKE2 on the install (for example `meho-host check`) | as it is, in a code block |
| `ubuntu-cis.txt` | the appliance's scan result list, `meho-appliance-<version>-cis.txt` from the `appliance-build` run | counts per result, and each rule that does not pass |

A new run of the release job (for example by hand after a fix) keeps
these three parts. Before it writes the sheet, it downloads the sheet that
is already on the release and gives it to the script as `--previous`.
When the job cannot read the release's asset list, it stops: a failed
lookup does not count as "no sheet yet", because the upload would then
replace the sheet and its test-install results.
Without `--lab`, the script takes the three parts from it. Each part sits
between two HTML comments, `<!-- lab_ports -->` and `<!-- end lab_ports -->`
(a Markdown page does not show them), so the script finds them even when
the template text changed. A previous sheet without these marks stops the
job, and the release manager gives the results again with `--lab`. The
previous sheet also goes into the `security-sheet-data` artifact.

A missing or empty lab file gives "Not measured for this release yet."
The script replaces each IPv4 address in the shown text with
`<address>`, because the sheet is public. Host names are not found by the
script: the release manager removes them before the run.

### Fail closed

The script stops with exit code 2, and writes no sheet, when a data file
is missing or has the wrong shape: an image of `images.txt` without its
Trivy report (the script matches each report by its `ArtifactName`), a
report for an image that is not in the list, `images.txt` without the
MEHO image of the tag, a Trivy report without scanned targets that
does not show the image was read, an SBOM without packages, an empty port
list, a scan result list without rules, a tag or digest of the wrong form.
So a sheet never says "no findings" for data it could not read. The
template and the script must agree: a place without a value, or a value
without a place, stops it too.

An image with nothing to check is a passed scan, not a failed one (#6082).
Some images, like busybox, have no operating system and no package files.
For them Trivy writes a report without `Results`. The script accepts such a
report only when its `Metadata` has the image ID (`ImageID`) and the layers
(`DiffIDs`), so Trivy did read the image, and has no `OS`. The image gets
a row of zeros, and a line under the table names it: Trivy found nothing
in it to check. A report without `Results` and without the image ID or
layers still stops the script. So does one that names an OS but has no
`Results`: then Trivy did not check that OS's packages. When Trivy itself
fails, it exits non-zero and the scan step stops before the script runs.

## Dependencies

- cosign v3 (`sigstore/cosign-installer` v4.1.2, as in the other release
  workflows). The SBOM is `.payload | @base64d | fromjson | .predicate` of
  the attestation line; checked against the v0.45.0 image.
- Trivy v0.70.0, installed with `aquasecurity/setup-trivy` v0.2.6: the
  version and the setup action that `aquasecurity/trivy-action` v0.36.0
  of `image.yml` uses. The JSON report has `SchemaVersion` 2, leaves out
  `Vulnerabilities` for a target without findings (Trivy
  `pkg/types/report.go`), and its `ArtifactName` is the reference that was
  scanned (`pkg/fanal/image/remote.go`). It leaves out `Results` when the
  image has no OS and no package files (seen with busybox 1.37 on v0.46.0).
- Helm, pinned by version and sha256 in
  `deploy/linux-server/packaging/common.sh` of the tag, to pull and render
  the chart.
- regctl v0.11.6, the same pin as `linux-download.yml`.
- `gh` on the GitHub-hosted runner, for `gh release download` and
  `gh release upload --clobber`.

## Known issues

- Trivy scans the linux/amd64 image of each multi-arch image.
- The SBOM covers only the MEHO image. The other images are listed with
  their digests and scanned, but the sheet has no SBOM of them.

## References

- ADR 0009 "Closed by default".
- Trivy JSON report: https://trivy.dev/latest/docs/configuration/reporting/
- cosign verify: https://docs.sigstore.dev/cosign/verifying/verify/
- [appliance-cis.md](appliance-cis.md), [linux-server-install.md](linux-server-install.md#the-host-firewall),
  [devops.md](devops.md) (image signing and SBOM).
