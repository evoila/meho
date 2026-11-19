#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# upload-release.sh: put the files of an appliance release into Google
# Cloud Storage, one folder per release: gs://<bucket>/<version>/.
#
# Usage: upload-release.sh <version> <dir>
#
# <dir> holds meho-appliance-<version>.ova,
# meho-appliance-<version>-os-packages.tar, SHA256SUMS and
# SHA256SUMS.cosign.bundle. The bucket name is in MEHO_RELEASE_BUCKET.
#
# The script never creates a bucket and never changes who can read it.
# It never overwrites a file. A second run of the same release with the
# same content does nothing. A second run with other content stops.
# Any gcloud error other than "no such file" stops the script, and so does
# a missing bucket. gcloud's own error text is not printed: it can name the
# service account, and the job log is public.
#
# Exit codes:
#   0  the files are in the folder
#   1  the bucket is not set up or does not exist, gcloud failed, or the
#      upload was refused
#   2  wrong usage

set -Eeuo pipefail

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    [[ -z ${2:-} ]] || printf '       To fix: %s\n' "$2" >&2
    exit 1
}

usage_error() {
    printf 'upload-release.sh: %s\nUsage: upload-release.sh <version> <dir>\n' "$1" >&2
    exit 2
}

# in_bucket <gs-path>: 0 when the file is there, 1 when it is not. Any
# other answer of gcloud stops the script.
in_bucket() {
    local err
    if err=$(gcloud storage ls "$1" 2>&1 >/dev/null); then
        return 0
    fi
    case $err in
        *"matched no objects"*) return 1 ;;
        *"gs://$bucket not found"* | *"bucket does not exist"*)
            fail "The bucket gs://$bucket does not exist. Nothing was uploaded." \
                "ask the release owner to check the variable MEHO_RELEASE_BUCKET." ;;
    esac
    fail "gcloud could not read $1. Nothing more was uploaded." \
        "check the Google Cloud sign-in step and the service account's access to the bucket."
}

# upload <file> <gs-path>: copy one file, never over an existing one.
upload() {
    local err
    err=$(gcloud storage cp --no-clobber "$1" "$2" 2>&1 >/dev/null) ||
        fail "gcloud could not upload ${1##*/} to $2." \
            "check the service account's access to the bucket, then run the step again."
    if [[ $err == *"Skipping existing"* ]]; then
        fail "$2 was already there and was not replaced. The release is not complete." \
            "another run uploaded at the same time; the release owner looks at the folder."
    fi
}

main() {
    [[ $# -eq 2 ]] || usage_error "expected 2 arguments."
    local version=$1 dir=$2
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] ||
        usage_error "the version '$version' is not like 0.36.0."
    # Not local: in_bucket reads it.
    bucket=${MEHO_RELEASE_BUCKET:-}
    [[ -n $bucket ]] ||
        fail "The bucket is not set up. The release owner creates it." \
            "ask the release owner to create the bucket and set the variable MEHO_RELEASE_BUCKET."
    [[ $bucket =~ ^[a-z0-9][a-z0-9._-]*$ ]] || usage_error "the bucket name is not valid."
    command -v gcloud >/dev/null || fail "gcloud is not installed." "install the Google Cloud CLI on the runner."

    local files=("meho-appliance-$version.ova" "meho-appliance-$version-os-packages.tar" SHA256SUMS.cosign.bundle)
    local file
    for file in "${files[@]}" SHA256SUMS; do
        [[ -f $dir/$file ]] || fail "$dir/$file is missing." "run the build and the signing steps first."
    done
    (cd "$dir" && sha256sum --check --quiet SHA256SUMS) ||
        fail "A file does not match SHA256SUMS." "make the checksums again from the files you upload."

    local dest=gs://$bucket/$version
    # Not local: the EXIT trap runs after main returns.
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT

    if in_bucket "$dest/SHA256SUMS"; then
        gcloud storage cp "$dest/SHA256SUMS" "$work/published" >/dev/null 2>&1 ||
            fail "gcloud could not read $dest/SHA256SUMS." \
                "check the service account's access to the bucket."
        cmp -s "$work/published" "$dir/SHA256SUMS" ||
            fail "Release $version is already published with other content. Nothing was uploaded." \
                "never replace a published release; make a new version."
        printf '[ok]   release %s is already published with the same content\n' "$version"
        return 0
    fi
    for file in "${files[@]}"; do
        if in_bucket "$dest/$file"; then
            fail "$dest holds $file, but no SHA256SUMS: an earlier upload stopped half way. Nothing was uploaded." \
                "the release owner looks at the folder and clears it."
        fi
    done
    # SHA256SUMS goes last: when it is there, the release is complete.
    for file in "${files[@]}" SHA256SUMS; do
        upload "$dir/$file" "$dest/$file"
    done
    printf '[ok]   uploaded %d files to %s/\n' "$((${#files[@]} + 1))" "$dest"
}

main "$@"
