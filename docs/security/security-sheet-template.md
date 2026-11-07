# MEHO {{ version }}: security sheet

This sheet is for the security team that checks MEHO before it goes in.
It holds measured facts about the release `{{ tag }}` (commit
`{{ commit }}`), not promises. The release job made it on {{ made_on }}.

Every MEHO install is closed by default and hardened by us. You never
have to lower a security setting for MEHO.

The files named below are on the release page:
https://github.com/evoila/meho/releases/tag/{{ tag }}

## 1. Open ports

The install sets a host firewall (nftables, the firewall built into
Linux). These are the rules of this release, read from its install
script. "open" means every address may connect. "admin-network" means
only the admin network you give at install time may connect. "closed"
means no other machine gets an answer.

| Port | Who may connect from another machine |
|---|---|
{{ port_rows }}

Port 80 answers only to send you to port 443.

**Port scan of a test install of this release:**

{{ lab_ports }}

## 2. CIS results

CIS benchmarks are common hardening standards.

### The built-in Kubernetes (RKE2, CIS Kubernetes Benchmark)

The install turns on RKE2's CIS profile. Kubernetes' "restricted" pod
security standard applies in every namespace: it refuses pods that run
as root, ask for extra rights or use folders of the host. There is one
exception: the namespace of the storage helper (Rancher's local-path
provisioner) may use host folders for the volumes. No MEHO namespace has
an exception.

**Result on a test install of this release:**

{{ lab_rke2_cis }}

### The appliance OS (CIS Ubuntu Linux 24.04, Level 1 Server)

This part is for the appliance (the ready-made virtual machine). On a
Linux server that you install yourself, you own the OS.

**Scan result of the appliance of this release:**

{{ lab_ubuntu_cis }}

The scan leaves out the rules that cannot apply to a machine that runs
Kubernetes. Each one has its reason in
https://github.com/evoila/meho/blob/{{ tag }}/deploy/appliance/cis/not-applicable.md

**Rules that apply but fail, each with why (the exception list of this
release, approved once per release):**

{{ cis_exceptions }}

## 3. Vulnerability scan of the MEHO image

The release job scanned the MEHO image with Trivy, by digest:

`ghcr.io/evoila/meho@{{ image_digest }}`

It scanned the image for linux/amd64. It counts the known security bugs
(CVEs) of the OS packages and the Python packages in the image.

{{ vuln_counts }}

Before the image job publishes an image, the same scan must find no
CRITICAL or HIGH finding that has a fix. Otherwise the image is not
published.

**CRITICAL and HIGH findings:**

{{ vuln_findings }}

**Findings we accepted for a time (the scan exception list):**

{{ vuln_exceptions }}

The full scan report: `{{ vuln_report_file }}`.

## 4. What is inside (SBOM)

The list of every package in the MEHO image (an SBOM, a software bill of
materials): `{{ sbom_file }}`.

- Format: {{ sbom_format }}
- Packages listed: {{ sbom_package_count }}
- Made by: {{ sbom_tool }}

The image job signs the same list and attaches it to the image. You can
read it from there and check its signature yourself:

```bash
cosign verify-attestation --type spdxjson \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/image.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "ghcr.io/evoila/meho@{{ image_digest }}" \
  | jq -r '.payload | @base64d | fromjson | .predicate' > sbom.spdx.json
```

## 5. How to check the signature of each download

Every download is signed with cosign (Sigstore). There is no key to
fetch: the check compares the signature with the GitHub workflow that
made the file. The release job ran each check below for this release,
and each one passed.

The MEHO image (pin it by this digest):

```bash
cosign verify \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/image.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "ghcr.io/evoila/meho@{{ image_digest }}"
```

The Helm chart (`oci://ghcr.io/evoila/meho-chart`, version {{ version }}):

```bash
cosign verify \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/chart.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "ghcr.io/evoila/meho-chart@{{ chart_digest }}"
```

The files on the release page. Check the signature of the checksum file
first, then the files against it:

```bash
BASE=https://github.com/evoila/meho/releases/download/{{ tag }}
ISSUER=https://token.actions.githubusercontent.com
WORKFLOWS=https://github.com/evoila/meho/.github/workflows

# The CLI files: SHA256SUMS
curl -fLO "$BASE/SHA256SUMS"
curl -fLO "$BASE/SHA256SUMS.cosign.bundle"
cosign verify-blob --certificate-identity "$WORKFLOWS/cli-release.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer "$ISSUER" --bundle SHA256SUMS.cosign.bundle SHA256SUMS

# The Claude Desktop bundle: SHA256SUMS-mcpb
curl -fLO "$BASE/SHA256SUMS-mcpb"
curl -fLO "$BASE/SHA256SUMS-mcpb.cosign.bundle"
cosign verify-blob --certificate-identity "$WORKFLOWS/cli-release.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer "$ISSUER" --bundle SHA256SUMS-mcpb.cosign.bundle SHA256SUMS-mcpb

# The download for a Linux server without internet: SHA256SUMS-linux
curl -fLO "$BASE/SHA256SUMS-linux"
curl -fLO "$BASE/SHA256SUMS-linux.cosign.bundle"
cosign verify-blob --certificate-identity "$WORKFLOWS/linux-download.yml@refs/tags/{{ tag }}" \
  --certificate-oidc-issuer "$ISSUER" --bundle SHA256SUMS-linux.cosign.bundle SHA256SUMS-linux

# Then check each file you downloaded against its checksum file, for example:
sha256sum -c --ignore-missing SHA256SUMS
```
