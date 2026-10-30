#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# build-download.sh: build meho-linux-<version>-amd64.tar, the one download
# of install form 2. It holds install.sh, RKE2, the chart, every image,
# helm, the meho CLI and the licence texts, so the install needs no
# internet. The layout is the one of the form-2 design, section 4, in
# meho-internal docs/design/2026-10-10-install-forms/.
#
# The release workflow .github/workflows/linux-download.yml runs it, then
# check-download.sh. See docs/codebase/linux-server-download.md.
#
# Exit codes:
#   0  the download is built
#   1  a step failed; the [FAIL] line says what to fix
#   2  wrong usage

set -Eeuo pipefail

LINUX_SERVER_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PACKAGING_DIR=$LINUX_SERVER_DIR/packaging
REPO_ROOT=$(cd -- "$LINUX_SERVER_DIR/../.." && pwd -P)
readonly LINUX_SERVER_DIR PACKAGING_DIR REPO_ROOT

# shellcheck source=packaging/common.sh
source "$PACKAGING_DIR/common.sh"
# shellcheck source=packaging/images.sh
source "$PACKAGING_DIR/images.sh"
# shellcheck source=packaging/parts.sh
source "$PACKAGING_DIR/parts.sh"

usage() {
    cat <<'HELP'
build-download.sh: build the form-2 download meho-linux-<version>-amd64.tar.

Usage:
  build-download.sh <version> <out-dir>

  <version>  The MEHO version without the leading "v", for example 0.36.0.
  <out-dir>  The folder for the tar. It is created when it is missing.

Settings, from the environment:
  MEHO_CLI_BINARY          Required. The meho CLI for Linux amd64 of this
                           version. "meho version" must print v<version>.
  MEHO_CHART_VERSION       The chart version on oci://ghcr.io/evoila/meho-chart.
                           Default: <version>.
  MEHO_IMAGE_TAG           The tag of ghcr.io/evoila/meho. Default: v<version>.
  MEHO_SIGNER_REF          The git ref of the workflows that signed the chart
                           and the image. Default: refs/tags/v<version>.
  MEHO_WAIT_SECONDS        How long to wait until the chart and the image are
                           published and signed. Default: 0.
  MEHO_DOWNLOAD_MAX_BYTES  The size limit of the tar. Default: 2040109465
                           (1.9 GiB).

An overlay image instead of ghcr.io/evoila/meho (all empty by default):
  MEHO_OVERLAY_IMAGE       The image repository, for example
                           registry.example.com/team/meho-overlay. Its tag
                           is MEHO_IMAGE_TAG.
  MEHO_OVERLAY_SIGNER      Required with an overlay. The cosign certificate
                           identity that signed it. No other signer passes.
  MEHO_OVERLAY_OIDC_ISSUER The OIDC issuer of that identity. Default:
                           https://token.actions.githubusercontent.com.
  MEHO_OVERLAY_LICENCE     Required with an overlay. Its line for
                           LICENSES/README.txt: "<licence files> <source>",
                           in the format of packaging/image-sources.txt.

A login for a private registry (all three or none):
  MEHO_REGISTRY            The registry, for example registry.example.com.
  MEHO_REGISTRY_USERNAME   The user name.
  MEHO_REGISTRY_PASSWORD   The password or token. It goes to regctl and
                           cosign on stdin, never on a command line.

Tools on PATH: curl, tar, zstd, jq, sha256sum, regctl and cosign.
HELP
}

main() {
    case ${1-} in
        -h | --help)
            usage
            exit 0
            ;;
    esac
    (($# == 2)) || usage_error "it needs two arguments: <version> <out-dir>."
    VERSION=$1
    local out_dir=$2 name
    [[ $VERSION =~ $VERSION_PATTERN ]] ||
        usage_error "the version '$VERSION' is not like 0.36.0 (no leading v)."
    CHART_VERSION=${MEHO_CHART_VERSION:-$VERSION}
    IMAGE_TAG=${MEHO_IMAGE_TAG:-v$VERSION}
    SIGNER_REF=${MEHO_SIGNER_REF:-refs/tags/v$VERSION}
    WAIT_SECONDS=${MEHO_WAIT_SECONDS:-0}
    CLI_BINARY=${MEHO_CLI_BINARY:-}
    local max_bytes=${MEHO_DOWNLOAD_MAX_BYTES:-$DEFAULT_MAX_BYTES}
    [[ $WAIT_SECONDS =~ ^[0-9]+$ ]] || usage_error "MEHO_WAIT_SECONDS must be a number of seconds."
    [[ $max_bytes =~ ^[0-9]+$ ]] || usage_error "MEHO_DOWNLOAD_MAX_BYTES must be a number of bytes."
    [[ -n $CLI_BINARY && -f $CLI_BINARY && -x $CLI_BINARY ]] ||
        usage_error "MEHO_CLI_BINARY must name the executable meho CLI for Linux amd64."
    read_image_settings
    check_registry_login
    need_tools curl tar zstd jq sha256sum regctl cosign
    trap 'on_error "$?" "$LINENO" "${BASH_SOURCE[0]##*/}"' ERR

    mkdir -p "$out_dir"
    WORK=$(mktemp -d "$out_dir/.build.XXXXXX")
    trap 'rm -rf "$WORK"' EXIT
    # Helm keeps its cache and registry settings in the build folder, so the
    # build neither reads nor changes the settings of the user who runs it.
    export HELM_CACHE_HOME=$WORK/helm-home/cache HELM_CONFIG_HOME=$WORK/helm-home/config \
        HELM_DATA_HOME=$WORK/helm-home/data
    registry_login "$WORK/registry-login" regctl cosign
    name=$(download_name "$VERSION")
    STAGE=$WORK/$name
    mkdir -p "$STAGE"/{lib,storage,rke2,images,chart,bin,LICENSES}

    step "Build $name.tar"
    add_scripts
    add_helm
    add_cli
    wait_until_published
    add_chart
    add_rke2
    add_images
    add_licences
    write_version_file

    step "Pack and check the size"
    pack "$WORK/$name.tar"
    check_size "$WORK/$name.tar" "$max_bytes"
    mv -f "$WORK/$name.tar" "$out_dir/$name.tar"
    printf '\nbuild-download.sh: wrote %s\n' "$out_dir/$name.tar"
}

main "$@"
