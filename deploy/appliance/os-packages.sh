#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# os-packages.sh: make the OS-package file of an appliance release,
# meho-appliance-<version>-os-packages.tar, and sign its repository.
#
# Usage: os-packages.sh <version> <deb-dir> <out-dir>
#
# The file is a flat apt repository, the format that
# deploy/linux-server/install.sh --os-packages reads: the .deb files from
# <deb-dir>, an uncompressed Packages file, and InRelease, which lists
# Packages with its checksum and is signed with the key.
#
# The key comes from one of these (the key text is never printed):
#   MEHO_OS_PACKAGES_SIGNING_KEY       the secret key, armored text
#   MEHO_OS_PACKAGES_SIGNING_KEY_FILE  the path of a file with the secret key
# The key must have no passphrase.
#
# MEHO_RELEASE=1 marks a release run. A release run needs a key, and it
# refuses a test key: a key whose user id holds "TEST KEY". The release
# workflow sets MEHO_RELEASE=1 when it publishes.
#
# Exit codes:
#   0  the file is made
#   1  the key or the packages are wrong; the [FAIL] line says what to fix
#   2  wrong usage

set -Eeuo pipefail

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    [[ -z ${2:-} ]] || printf '       To fix: %s\n' "$2" >&2
    exit 1
}

usage_error() {
    printf 'os-packages.sh: %s\nUsage: os-packages.sh <version> <deb-dir> <out-dir>\n' "$1" >&2
    exit 2
}

main() {
    [[ $# -eq 3 ]] || usage_error "expected 3 arguments."
    local version=$1 deb_dir=$2 out_dir=$3
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] ||
        usage_error "the version '$version' is not like 0.36.0."
    [[ -d $deb_dir ]] || usage_error "$deb_dir is not a folder."
    local tool
    for tool in gpg apt-ftparchive tar; do
        command -v "$tool" >/dev/null || fail "$tool is not installed." "install it (apt-utils has apt-ftparchive)."
    done

    local key=${MEHO_OS_PACKAGES_SIGNING_KEY:-}
    if [[ -z $key && -n ${MEHO_OS_PACKAGES_SIGNING_KEY_FILE:-} ]]; then
        [[ -r $MEHO_OS_PACKAGES_SIGNING_KEY_FILE ]] ||
            fail "The key file $MEHO_OS_PACKAGES_SIGNING_KEY_FILE cannot be read." "check the path."
        key=$(<"$MEHO_OS_PACKAGES_SIGNING_KEY_FILE")
    fi
    if [[ -z $key ]]; then
        if [[ ${MEHO_RELEASE:-} == 1 ]]; then
            fail "The signing key is not set up. The project owner adds it." \
                "ask the project owner to add the secret MEHO_OS_PACKAGES_SIGNING_KEY to the release workflow."
        fi
        usage_error "give a signing key in MEHO_OS_PACKAGES_SIGNING_KEY or MEHO_OS_PACKAGES_SIGNING_KEY_FILE."
    fi

    local debs=("$deb_dir"/*.deb)
    [[ -e ${debs[0]} ]] || fail "There is no .deb file in $deb_dir." "put the OS package files there."

    # Not local: the EXIT trap runs after main returns.
    work=$(mktemp -d)
    # The key lives only in this home folder. gpg-agent is stopped and the
    # folder is deleted when the script ends.
    export GNUPGHOME=$work/gnupg
    trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
    mkdir -m 700 "$GNUPGHOME"
    printf '%s\n' "$key" | gpg --batch --quiet --import 2>/dev/null ||
        fail "gpg cannot read the signing key." "give an armored secret key without a passphrase."
    key=
    local uids
    uids=$(gpg --batch --list-secret-keys --with-colons | awk -F: '$1 == "uid" { print $10 }')
    [[ -n $uids ]] || fail "The signing key has no secret part." "give the secret key, not the public key."
    if [[ ${MEHO_RELEASE:-} == 1 ]] && grep -qi 'TEST KEY' <<<"$uids"; then
        fail "This is a test key. A release is never signed with a test key." \
            "put the real signing key into the secret MEHO_OS_PACKAGES_SIGNING_KEY."
    fi

    local repo=$work/repo
    mkdir "$repo"
    cp -- "${debs[@]}" "$repo/"
    (
        cd "$repo"
        apt-ftparchive packages . >Packages 2>/dev/null
        # InRelease is the clear-signed Release file. It must list the
        # uncompressed Packages file: apt skips a repository without an
        # error when it lists only Packages.gz.
        apt-ftparchive release . >"$work/Release" 2>/dev/null
        gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
            --clearsign --output InRelease "$work/Release"
    )
    grep -q ' Packages$' "$repo/InRelease" || fail "InRelease does not list Packages." "look at apt-ftparchive's output."

    mkdir -p "$out_dir"
    local name=meho-appliance-$version-os-packages.tar
    local files=(./InRelease ./Packages)
    local deb
    for deb in "${debs[@]}"; do files+=("./${deb##*/}"); done
    tar --format=ustar --owner=0 --group=0 --numeric-owner -C "$repo" -cf "$out_dir/$name" "${files[@]}"
    printf '[ok]   wrote %s/%s with %d .deb files\n' "$out_dir" "$name" "${#debs[@]}"
}

main "$@"
