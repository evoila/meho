# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Shared paths and output helpers for install.sh. This file is sourced, not run.
#
# The constants below are used by install.sh and the other files in lib/,
# so shellcheck cannot see their use from this file alone.
# shellcheck disable=SC2034

# Tests set MEHO_TEST_ROOT to a temporary folder. Every path on the machine is
# then read and written below that folder, so a test runs without root and
# changes nothing outside it. On a real machine the variable is not set.
HOST_ROOT="${MEHO_TEST_ROOT:-}"

# Paths as they are on the machine. Code that reads or writes them adds
# HOST_ROOT in front; messages and RKE2's settings use them as they are.
readonly MEHO_STATE_DIR=/var/lib/meho
readonly MEHO_CONFIG_DIR=/etc/meho
readonly MEHO_JOIN_TOKEN_FILE=/etc/meho/join-token
readonly RKE2_CONFIG_FILE=/etc/rancher/rke2/config.yaml
readonly RKE2_KUBECONFIG=/etc/rancher/rke2/rke2.yaml
readonly RKE2_KUBECTL=/var/lib/rancher/rke2/bin/kubectl
readonly RKE2_IMAGES_DIR=/var/lib/rancher/rke2/agent/images
readonly RKE2_MANIFESTS_DIR=/var/lib/rancher/rke2/server/manifests
readonly TRAEFIK_CONFIG_MANIFEST=rke2-traefik-config.yaml

# Files of the download that steps 1 to 3 use, relative to the folder of
# install.sh. The layout is the one of the form-2 design, section 4.
readonly MEHO_IMAGES_ARCHIVE=images/meho-images.linux-amd64.tar.zst
readonly STORAGE_MANIFEST=storage/local-path-storage.yaml
readonly DOWNLOAD_FILES=(
    rke2/install.sh
    rke2/rke2.linux-amd64.tar.gz
    rke2/rke2-images.linux-amd64.tar.zst
    rke2/sha256sum-amd64.txt
    "$MEHO_IMAGES_ARCHIVE"
    "$STORAGE_MANIFEST"
)

readonly RKE2_LOG_COMMAND="journalctl -u rke2-server -e"

ok() { printf '[ok]   %s\n' "$*"; }

warn() { printf '[WARN] %s\n' "$*" >&2; }

note() { printf '[note] %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

# fail <problem> <fix>: print one [FAIL] line that says what is wrong and
# what to do about it, then stop with exit code 1.
fail() {
    printf '[FAIL] %s Fix: %s\n' "$1" "$2" >&2
    exit 1
}

# usage_error <message>: wrong options; stop with exit code 2.
usage_error() {
    printf 'install.sh: %s\n' "$1" >&2
    printf "Run 'install.sh --help' to see the options.\n" >&2
    exit 2
}

# write_root_file <path on the machine> <mode>: write stdin to the file in
# one step, so a reader never sees half a file.
write_root_file() {
    local target="${HOST_ROOT}$1" mode=$2 temporary
    mkdir -p "${target%/*}"
    temporary=$(mktemp "${target}.XXXXXX")
    cat >"$temporary"
    chmod "$mode" "$temporary"
    mv -f "$temporary" "$target"
}

# join_by <separator> <item>...: print the items with the separator between them.
join_by() {
    local separator=$1 joined
    shift
    joined=${1-}
    shift || true
    local item
    for item in "$@"; do
        joined+="${separator}${item}"
    done
    printf '%s' "$joined"
}
