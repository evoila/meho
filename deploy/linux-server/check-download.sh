#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# check-download.sh: check a form-2 download that build-download.sh built.
# It reads only the tar and the registries, not the files of the build:
#
#   - the size is below the limit for one GitHub release file;
#   - the layout is the one of the form-2 design, section 4, and every file
#     is owned by root and can be changed only by root;
#   - VERSION, the RKE2 files, bin/helm and bin/meho are the pinned ones;
#   - the image archive is an OCI image layout that RKE2 can read, and it
#     holds exactly the images of the chart in the download (rendered with
#     the core on) plus the two images of the storage manifest. Each image
#     is there under the digest the chart pins, or, for an image the chart
#     names by tag only, under the digest the registry has for that tag.
#
# Exit codes: 0 every check passed, 1 a check failed, 2 wrong usage.

set -Eeuo pipefail

LINUX_SERVER_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PACKAGING_DIR=$LINUX_SERVER_DIR/packaging
readonly LINUX_SERVER_DIR PACKAGING_DIR

# shellcheck source=packaging/common.sh
source "$PACKAGING_DIR/common.sh"
# shellcheck source=packaging/images.sh
source "$PACKAGING_DIR/images.sh"
# shellcheck source=packaging/parts.sh
source "$PACKAGING_DIR/parts.sh"

usage() {
    cat <<'HELP'
check-download.sh: check a form-2 download.

Usage:
  check-download.sh <path to meho-linux-<version>-amd64.tar>

Settings, from the environment:
  MEHO_DOWNLOAD_MAX_BYTES  The size limit of the tar. Default: 2040109465
                           (1.9 GiB).
  MEHO_REGISTRY, MEHO_REGISTRY_USERNAME, MEHO_REGISTRY_PASSWORD
                           A login for a private registry, for a download
                           with an overlay image (all three or none). The
                           password goes to regctl on stdin.

Tools on PATH: tar, zstd, jq, sha256sum and regctl. The check runs bin/helm
and bin/meho of the download, so it runs on Linux amd64.
HELP
}

# version_value <VERSION file> <key>
version_value() {
    awk -v key="$2" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$1"
}

check_version_file() {
    local file=$STAGE/VERSION key missing=()
    for key in MEHO_VERSION MEHO_IMAGE_TAG CHART_VERSION RKE2_VERSION HELM_VERSION BUILD_DATE; do
        [[ -n $(version_value "$file" "$key") ]] || missing+=("$key")
    done
    ((${#missing[@]} == 0)) ||
        fail "VERSION has no value for: ${missing[*]}." "build the download again."
    [[ $(version_value "$file" MEHO_VERSION) == "$VERSION" ]] ||
        fail "VERSION says MEHO_VERSION=$(version_value "$file" MEHO_VERSION), but the file name says $VERSION." \
            "build the download again."
    [[ $(version_value "$file" RKE2_VERSION) == "$RKE2_VERSION" && $(version_value "$file" HELM_VERSION) == "$HELM_VERSION" ]] ||
        fail "VERSION names other RKE2 or Helm versions than the pinned $RKE2_VERSION and $HELM_VERSION." \
            "check the download with the check-download.sh of the same commit as build-download.sh."
    IMAGE_TAG=$(version_value "$file" MEHO_IMAGE_TAG)
    ok "VERSION: MEHO $VERSION, image tag $IMAGE_TAG, chart $CHART_VERSION, RKE2 $RKE2_VERSION, Helm $HELM_VERSION"
}

check_rke2_files() {
    local file expected
    check_sha256 "$STAGE/rke2/sha256sum-amd64.txt" "$RKE2_CHECKSUMS_SHA256" "rke2/sha256sum-amd64.txt"
    for file in "${RKE2_FILES[@]}"; do
        expected=$(awk -v name="$file" '$2 == name { print $1 }' "$STAGE/rke2/sha256sum-amd64.txt")
        check_sha256 "$STAGE/rke2/$file" "$expected" "rke2/$file"
    done
    check_sha256 "$STAGE/rke2/install.sh" "$RKE2_INSTALLER_SHA256" "rke2/install.sh"
    ok "rke2/: the files of RKE2 $RKE2_VERSION, checked against the pinned sha256sum-amd64.txt"
}

check_binaries() {
    local reported
    reported=$("$STAGE/bin/helm" version --short)
    [[ $reported == "$HELM_VERSION+"* ]] ||
        fail "bin/helm reports $reported, not $HELM_VERSION." "build the download again."
    reported=$("$STAGE/bin/meho" version)
    [[ $reported == "meho v$VERSION "* ]] ||
        fail "bin/meho reports '$reported', not v$VERSION." "build the download again with the CLI of this version."
    ok "bin/helm $HELM_VERSION and bin/meho v$VERSION"
}

check_images() {
    local refs=$WORK/image-refs.txt lock=$WORK/images.lock
    {
        chart_image_refs "$STAGE/bin/helm" "$STAGE/chart/meho-$CHART_VERSION.tgz" \
            "$STAGE/chart/values-single-node.yaml" "$IMAGE_TAG"
        images_in_manifests <"$STAGE/storage/local-path-storage.yaml"
    } | LC_ALL=C sort -u >"$refs"
    lock_images "$refs" "$lock"
    verify_image_archive "$STAGE/$IMAGE_ARCHIVE" "$lock" "$WORK"
}

main() {
    case ${1-} in
        -h | --help)
            usage
            exit 0
            ;;
    esac
    (($# == 1)) || usage_error "it needs one argument: the path of the download tar."
    local file=$1 base top max_bytes=${MEHO_DOWNLOAD_MAX_BYTES:-$DEFAULT_MAX_BYTES}
    [[ -f $file ]] || usage_error "$file is not a file."
    [[ $max_bytes =~ ^[0-9]+$ ]] || usage_error "MEHO_DOWNLOAD_MAX_BYTES must be a number of bytes."
    base=${file##*/}
    VERSION=""
    [[ $base =~ ^meho-linux-(.+)-amd64\.tar$ ]] && VERSION=${BASH_REMATCH[1]}
    [[ $VERSION =~ $VERSION_PATTERN ]] ||
        usage_error "$base is not named like meho-linux-0.36.0-amd64.tar."
    top=$(download_name "$VERSION")
    check_registry_login
    need_tools tar zstd jq sha256sum regctl
    trap 'on_error "$?" "$LINENO" "${BASH_SOURCE[0]##*/}"' ERR

    step "Check $base"
    check_size "$file" "$max_bytes"
    CHART_VERSION=$(tar -xOf "$file" "$top/VERSION" 2>/dev/null | awk -F= '$1 == "CHART_VERSION" { print $2; exit }') ||
        CHART_VERSION=""
    [[ -n $CHART_VERSION ]] ||
        fail "$base has no $top/VERSION with a CHART_VERSION line." "build the download again."
    check_layout "$file" "$top" "$CHART_VERSION"

    WORK=$(mktemp -d "$(dirname -- "$file")/.check.XXXXXX")
    trap 'rm -rf "$WORK"' EXIT
    export HELM_CACHE_HOME=$WORK/helm-home/cache HELM_CONFIG_HOME=$WORK/helm-home/config \
        HELM_DATA_HOME=$WORK/helm-home/data
    registry_login "$WORK/registry-login" regctl
    tar -xf "$file" -C "$WORK"
    STAGE=$WORK/$top
    check_version_file
    check_rke2_files
    check_binaries
    check_images
    printf '\ncheck-download.sh: %s passed every check.\n' "$base"
}

main "$@"
