# Form-2 download: `deploy/linux-server/build-download.sh` (#4734)

## Overview

Install form 2 puts MEHO on a plain Linux server that has no internet. The
customer gets one file, `meho-linux-<version>-amd64.tar`, copies it to the
server, unpacks it and runs `install.sh` as root
([linux-server-install.md](linux-server-install.md)). The file holds
everything the install needs: the script, RKE2, the chart, every container
image, `helm`, the `meho` CLI and the licence texts.

- `deploy/linux-server/build-download.sh <version> <out-dir>` builds the file.
- `deploy/linux-server/check-download.sh <tar>` checks a built file. It reads
  only the tar and the registries, not the files of the build.
- `.github/workflows/linux-download.yml` runs both. `cli-release.yml` calls it
  on every `v*` tag and attaches the file to the GitHub release.

The layout follows the form-2 design (meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`, section 4).

## The layout

The tar has one top folder, `meho-linux-<version>-amd64/`. Every file in it
is owned by root (0/0), and only root can change it. `install.sh`,
`meho-host`, `rke2/install.sh`, `bin/helm` and `bin/meho` are executable.

| Path | What | Source |
| --- | --- | --- |
| `install.sh`, `meho-host`, `lib/*.sh`, `storage/local-path-storage.yaml` | the install script and the host command ([linux-server-install.md](linux-server-install.md), [linux-server-meho-host.md](linux-server-meho-host.md)) | `deploy/linux-server/` |
| `VERSION` | `MEHO_VERSION`, `MEHO_IMAGE_TAG`, `CHART_VERSION`, `RKE2_VERSION`, `HELM_VERSION`, `BUILD_DATE`, one `KEY=value` per line | written by the build |
| `rke2/install.sh` | RKE2's installer at the RKE2 release tag | GitHub, `rancher/rke2` at `v1.37.1+rke2r1`, sha256 pinned |
| `rke2/rke2.linux-amd64.tar.gz`, `rke2/rke2-images.linux-amd64.tar.zst`, `rke2/sha256sum-amd64.txt` | RKE2 and its images | the RKE2 release; the checksum file is pinned by sha256 and checks the other two |
| `images/meho-images.linux-amd64.tar.zst` | every image of the chart, with the core on, plus the two images of the storage manifest | the registries, by digest (see below) |
| `chart/meho-<chart version>.tgz` | the MEHO chart | `oci://ghcr.io/evoila/meho-chart`, checked with `cosign verify` |
| `chart/values-single-node.yaml` | the values for one node | `deploy/linux-server/values-single-node.yaml`; its full content comes with #4735 |
| `bin/helm` | Helm v3.16.4, the version the chart's CI uses | `get.helm.sh`, sha256 pinned |
| `bin/meho` | the Linux amd64 CLI of the same version | the `release` job of `cli-release.yml` |
| `LICENSES/` | the licence texts and `README.txt`, which names each part, its licence and where its source code is | `LICENSE`, `NOTICE`, `deploy/linux-server/packaging/licenses/` |

`expected_layout` in `packaging/common.sh` is the list that
`check-download.sh` compares with `tar -tf`. It includes every file of
`DOWNLOAD_FILES` in `lib/common.sh`, the files that `install.sh` checks. A
test keeps the two in step.

## Key files

| File | What it holds |
| --- | --- |
| `build-download.sh` | Options, settings, and the order of the build steps. |
| `check-download.sh` | The checks of a built tar. |
| `packaging/common.sh` | The pinned versions and checksums, output helpers, the size check and the layout check. |
| `packaging/images.sh` | The image list, the digests, the fetch into one OCI image layout, and the archive check. |
| `packaging/parts.sh` | One function per part of the download, and the licence `README.txt`. |
| `packaging/render-values.yaml` | Values for `helm template`: the core on, and placeholders the chart's schema requires. |
| `packaging/image-sources.txt` | The licence and the source of each image repository. The build stops when an image has no line. |
| `packaging/licenses/*.txt` | Licence texts of the images that are not under Apache-2.0, as their projects publish them. |
| `values-single-node.yaml` | The values for one node, shipped as `chart/values-single-node.yaml`. |
| `tests/test_download_build.py`, `tests/test_download_check.py`, `tests/download_harness.py` | Offline tests. |

The files in `packaging/` build the download; they are not part of it.

## Control flow

### `build-download.sh <version> <out-dir>`

The version has no leading `v`, for example `0.36.0`. Settings come from the
environment:

| Setting | Default | What |
| --- | --- | --- |
| `MEHO_CLI_BINARY` | (required) | the Linux amd64 `meho` binary; `meho version` must print `v<version>` |
| `MEHO_CHART_VERSION` | `<version>` | the chart version on ghcr.io |
| `MEHO_IMAGE_TAG` | `v<version>` | the tag of `ghcr.io/evoila/meho` |
| `MEHO_SIGNER_REF` | `refs/tags/v<version>` | the git ref in the cosign identity of `chart.yml` and `image.yml` |
| `MEHO_WAIT_SECONDS` | `0` | how long to wait for the chart and the image |
| `MEHO_DOWNLOAD_MAX_BYTES` | `2040109465` (1.9 GiB) | the size limit |

Steps, in order:

1. Copy `install.sh`, `meho-host`, `lib/`, `storage/` and `values-single-node.yaml`.
2. Download Helm and check its sha256. The build uses this `helm` too.
3. Check and copy the CLI.
4. Wait until the chart and the image of the version are on ghcr.io and
   `cosign verify` passes for both, with the exact identity
   `https://github.com/evoila/meho/.github/workflows/<chart.yml|image.yml>@<MEHO_SIGNER_REF>`.
   On a tag, `chart.yml` and `image.yml` run at the same time as the build,
   so the release run waits up to 60 minutes.
5. `helm pull` the chart. The digest `helm pull` prints must be the digest
   `cosign verify` checked.
6. Download the RKE2 files and check them.
7. Build the image archive (next section).
8. Write `LICENSES/` and `VERSION`.
9. Pack the tar: `--owner=0 --group=0 --numeric-owner --sort=name`, with
   group and other write removed. Then the size check: above the limit the
   build stops with one `[FAIL]` line that names both sizes, for example
   `[FAIL] meho-linux-0.36.0-amd64.tar is 2100000000 bytes (1.96 GiB). The limit is 2040109465 bytes (1.90 GiB). Fix: ...`.

Every failure prints one `[FAIL] <what is wrong> Fix: <what to do>` line,
like `install.sh`. Exit codes: 0 built, 1 a step failed, 2 wrong usage.

### `check-download.sh <tar>`

1. The size limit.
2. The layout (`tar -tf` against `expected_layout`), the owners and the
   modes (`tar -tvf`). No links or special files.
3. `VERSION` has every key and matches the file name and the pinned RKE2
   and Helm versions.
4. The RKE2 files against the pinned `sha256sum-amd64.txt`, and the pinned
   installer.
5. `bin/helm version` and `bin/meho version`.
6. The images: it renders the chart **in the download** with the download's
   own `bin/helm` and `render-values.yaml`, adds the images of the storage
   manifest, and locks each image to a digest: the digest in the reference,
   or for a reference with a tag only, the digest the registry has for the
   tag now. Then it checks the archive (next section) against that list.

## The image archive

### Why an OCI image layout, not `docker save`

The chart and the storage manifest pin images by digest, for example
`docker.io/library/busybox:1.37@sha256:bdf5…` or
`pgvector/pgvector@sha256:ac08…`, with `imagePullPolicy: IfNotPresent`. On
the server, the pod only starts offline when containerd finds an image named
`<repository>@<that digest>`.

RKE2 imports every archive in `/var/lib/rancher/rke2/agent/images` at each
start (k3s `pkg/agent/containerd/containerd.go`, `preloadFile`, used by RKE2
v1.37.1):

- It calls containerd's `client.Import` with `WithAllPlatforms(true)` and
  `WithSkipMissing()`.
- For an archive with an `oci-layout` file, containerd keeps `index.json` and
  every manifest as they are, so each image keeps its registry digest. For an
  archive without it (the `docker save` format, `manifest.json`), containerd
  writes a new manifest from the config and the layers, with a new digest
  (`core/images/archive/importer.go`, `ImportIndex`).
- Then k3s adds the name `<repository>@<digest of the imported manifest>` to
  each image (`retagImages`). The kubelet asks for
  `repository:tag@digest`, which containerd's CRI plugin reduces to
  `repository@digest` (`LocalResolve`, `ParseDockerRef`).

So with `docker save`, the digest name does not match the pin, the kubelet
does not find the image, and the pod tries to pull it from the internet.

A local test with containerd v2.3.6 (the line RKE2 v1.37.1's containerd fork
is on) and the k3s import options confirmed it: the OCI layout gave
`docker.io/library/busybox@sha256:bdf57e52…` (the pin), and a `docker save`
style archive of the same image gave `sha256:f2d5f776…`.

### How the build writes it

- **The list**: `helm template` of the pulled chart with the core on
  (`render-values.yaml`, then `chart/values-single-node.yaml`), plus the
  images of `storage/local-path-storage.yaml`. When #4050 adds `images.txt`
  to the chart, the list can come from that file instead.
- **The lock**: each image gets one digest. A reference with a digest keeps
  it. A reference with a tag only (for example `valkey/valkey:9.0-alpine`
  and `ghcr.io/evoila/meho:v0.36.0`) is resolved once with
  `regctl image digest`. The MEHO image's digest must be the one
  `cosign verify` checked.
- **The names**: RKE2 needs a name with a tag (`retagImages` and
  `labelContent` fail on a name without one). An image referenced by tag
  keeps its tag. An image referenced by digest only gets the tag
  `digest-<hex>`, for example
  `docker.io/pgvector/pgvector:digest-ac08538c…`. RKE2 adds the digest name
  itself.
- **The fetch**: `regctl image copy --platform linux/amd64 <repository>@<digest>`
  into one OCI layout, then the top manifest of each image (often an index
  of several platforms) as the registry serves it, then `index.json` with the
  annotation `io.containerd.image.name`. Only the linux/amd64 content is in
  the archive; the index still lists the other platforms, which containerd
  skips (`WithSkipMissing`). A CRI pull leaves the same partial index.
- **The compression**: `zstd` at the default level. The layers are already
  gzip-compressed, so a higher level gains little. RKE2 reads `.tar.zst`
  with at most 32 MiB of decoder memory (rancher/wharfie v0.7.1,
  `MaxDecoderMemory = 1 << 25`), so the archive must not use `--long` above
  25 or `--ultra`. The check decompresses with `zstd --memory=32MiB`.

### What the check proves

`verify_image_archive` in `packaging/images.sh`:

- the archive decompresses with 32 MiB of decoder memory;
- it is an OCI image layout (`oci-layout` version 1.0.0 and `index.json`);
- every blob has the sha256 it is named by;
- the pairs (name, digest) in `index.json` are exactly the locked list: no
  image missing, no image extra, no image under another digest;
- each image has a linux/amd64 manifest, its config says linux/amd64, and
  its config and layers are in the archive.

## The workflow

`.github/workflows/linux-download.yml` runs on GitHub-hosted
`ubuntu-latest` (about 6 GB of disk). It has no `pull_request` trigger.

| Trigger | What is built | Result |
| --- | --- | --- |
| `workflow_call` from `cli-release.yml`, on a `v*` tag | the tag: chart `<version>`, image `v<version>`, the CLI archive that the `release` job uploads as the artifact `meho-cli-linux-amd64` (checked against GoReleaser's `SHA256SUMS`) | the job `publish` signs `SHA256SUMS-linux` (`cosign sign-blob`, keyless) and attaches the tar, `SHA256SUMS-linux` and `SHA256SUMS-linux.cosign.bundle` to the release |
| `workflow_dispatch` on `main` | the main commit: the chart `0.1.<date>-<short sha>` that `chart.yml` published for it, the image `sha-<commit>`, and a CLI built from the commit | the workflow artifact `meho-linux-download` (tar and `SHA256SUMS-linux`), kept 3 days; no release changes |

Every action is pinned by SHA. The workflow default is `contents: read`.
Only `publish` has `contents: write` and `id-token: write`, and it runs no
repository code. The calling job in `cli-release.yml` grants the two
permissions, because a called workflow can use at most what its caller
grants. GitHub allows no `timeout-minutes` on a calling job; the two jobs of
`linux-download.yml` have their own.

The signing identity is the called workflow file, not `cli-release.yml`
(Fulcio puts `job_workflow_ref` in the certificate). To check a release:

```bash
TAG=v0.36.0
BASE=https://github.com/evoila/meho/releases/download/$TAG
curl -fLO "$BASE/meho-linux-${TAG#v}-amd64.tar"
curl -fLO "$BASE/SHA256SUMS-linux"
curl -fLO "$BASE/SHA256SUMS-linux.cosign.bundle"
cosign verify-blob \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/linux-download.yml@refs/tags/$TAG" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --bundle SHA256SUMS-linux.cosign.bundle SHA256SUMS-linux
sha256sum -c SHA256SUMS-linux
```

## Dependencies

- The build host: Linux amd64 with `curl`, GNU `tar`, `zstd`, `jq`,
  `sha256sum`, `regctl` (v0.11.6 in CI) and `cosign` (v3.0.6 in CI).
- Network: GitHub (RKE2, the RKE2 installer), `get.helm.sh`, `ghcr.io`,
  Docker Hub and `quay.io`.
- Inputs that other workflows publish: the chart (`chart.yml`), the image
  (`image.yml`) and the CLI (`cli-release.yml`).

## Known issues

- **Size.** The tar is about 1.8 GiB (the build of main on 2026-10-22:
  1,910,231,040 bytes, 1.78 GiB), close to the limit of 1.9 GiB. The MEHO
  image grows with each release. When the build fails on the size, the
  design's answer is two release files (RKE2 in one, the core in the other).
- **Docker Hub without a login.** The build fetches four images from Docker
  Hub without a login. Docker Hub allows 100 pulls in 6 hours per address,
  and the build uses a few. A shared runner address can still reach the
  limit; the build then stops at the fetch, and a new run helps.
- **The licence duties.** `LICENSES/` lists the licence and the source of
  each part. Whether a link to the source is enough for the GPL software in
  the images (busybox, and the base Linux of the images) is still open in
  the design.
- **`images.txt`.** Until #4050 adds it, the image list comes from
  `helm template`.
- **The first tag run.** The release path (call from `cli-release.yml`,
  `publish`) runs for the first time on the next `v*` tag. A manual run on
  main covers the build and the checks before that.

## Testing

```bash
python3 -m unittest discover -s deploy/linux-server/tests -v
```

The tests run the functions of `packaging/` with bash, offline:

- stub commands for `regctl` and `zstd` on `PATH`, each answering from one
  file;
- small OCI image layouts built in Python (`tests/download_harness.py`): a
  sparse index of two platforms with only linux/amd64 present, as the build
  writes it.

They cover the options, the size limit (one `[FAIL]` line with both
sizes), the image references and the lock, the licence table, the layout
check (missing and extra files, owner, modes, links) and the archive check
(`docker save` format, wrong digest, missing or extra image, changed or
missing blob, other platform, decoder memory).

A full build needs the network (about 2 GB of downloads):

```bash
MEHO_CLI_BINARY=/path/to/meho MEHO_SIGNER_REF=refs/tags/v0.36.0 \
  deploy/linux-server/build-download.sh 0.36.0 /tmp/download
deploy/linux-server/check-download.sh /tmp/download/meho-linux-0.36.0-amd64.tar
```

## References

- The form-2 design: meho-internal
  `docs/design/2026-10-10-install-forms/form-2-linux-server.md`, section 4.
- RKE2 offline install: <https://docs.rke2.io/install/airgap>.
- k3s image import (RKE2 v1.37.1 uses k3s at `356c025475bb`):
  <https://github.com/k3s-io/k3s/blob/356c025475bb/pkg/agent/containerd/containerd.go>.
- containerd import: `client/import.go` and `core/images/archive/importer.go`
  in <https://github.com/k3s-io/containerd/tree/v2.3.4-k3s1.36>.
- rancher/wharfie v0.7.1, the archive reader with the 32 MiB zstd limit:
  <https://github.com/rancher/wharfie/blob/v0.7.1/pkg/tarfile/tarfile.go>.
- Fulcio puts `job_workflow_ref` in the certificate:
  <https://docs.sigstore.dev/certificate_authority/oidc-in-fulcio/>.
- GitHub release file limit (under 2 GiB):
  <https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases>.
