# The security sheet of a release

Every release gets one short Markdown file for the customer's security
team: `meho-security-sheet-<tag>.md` on the GitHub Release (#4639, ADR
0009 "closed by default"). It holds measured facts, not promises:

1. the open ports, and who may talk to them;
2. the CIS results: RKE2's CIS profile and the appliance's CIS Ubuntu
   Level 1 scan;
3. the vulnerability scan of the MEHO image;
4. the SBOM (the list of every package in the image);
5. how to check the signature of each download.

Two more files go next to it on the release: the SBOM
(`meho-sbom-<tag>.spdx.json`) and the full scan report
(`meho-vulnerability-scan-<tag>.json`).

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
4. scans the image by digest with Trivy: every severity, the scan
   exception file `.trivyignore` of the tag, and it never fails on a
   finding. The image job's own scan already refused every CRITICAL or
   HIGH finding that has a fix;
5. writes `facts.json`, the port list (`firewall_port_list` of
   `deploy/linux-server/lib/firewall.sh` at the tag), and copies
   `.trivyignore` and `deploy/appliance/cis/exceptions.md` of the tag;
6. runs `security_sheet.py`, keeps the data folder as the workflow
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

A missing or empty lab file gives "Not measured for this release yet."
The script replaces each IPv4 address in the shown text with
`<address>`, because the sheet is public. Host names are not found by the
script: the release manager removes them before the run.

### Fail closed

The script stops with exit code 2, and writes no sheet, when a data file
is missing or has the wrong shape: a Trivy report without scanned targets,
an SBOM without packages, an empty port list, a scan result list without
rules, a tag or digest of the wrong form. So a sheet never says "no
findings" for data it could not read. The template and the script must
agree: a place without a value, or a value without a place, stops it too.

## Dependencies

- cosign v3 (`sigstore/cosign-installer` v4.1.2, as in the other release
  workflows). The SBOM is `.payload | @base64d | fromjson | .predicate` of
  the attestation line; checked against the v0.45.0 image.
- Trivy through `aquasecurity/trivy-action` v0.36.0, the same commit as
  `image.yml`. The JSON report has `SchemaVersion` 2 and leaves out
  `Vulnerabilities` for a target without findings (Trivy
  `pkg/types/report.go`).
- regctl v0.11.6, the same pin as `linux-download.yml`.
- `gh` on the GitHub-hosted runner, for `gh release download` and
  `gh release upload --clobber`.

## Known issues

- Only the MEHO image is scanned. The chart's other images (Postgres,
  Keycloak, OpenBao, Valkey, and the storage helper's) are not in the
  sheet.
- Trivy scans the linux/amd64 image of the multi-arch image.
- A rerun with `--clobber` replaces the files on the release. The
  `security-sheet-data` artifact of the first run is still the one to use
  for the lab step, unless the rerun changed the data.

## References

- ADR 0009 "Closed by default".
- Trivy JSON report: https://trivy.dev/latest/docs/configuration/reporting/
- cosign verify: https://docs.sigstore.dev/cosign/verifying/verify/
- [appliance-cis.md](appliance-cis.md), [linux-server-install.md](linux-server-install.md#the-host-firewall),
  [devops.md](devops.md) (image signing and SBOM).
