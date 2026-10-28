# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Shared settings and helpers of build-download.sh and check-download.sh.
# This file is sourced, not run. The files in packaging/ build the download;
# they are not part of it.
#
# The constants below are used by the two scripts and by packaging/images.sh,
# so shellcheck cannot see their use from this file alone.
# shellcheck disable=SC2034

# Pinned inputs. A change here changes every later download.
readonly RKE2_VERSION=v1.37.1+rke2r1
# The sha256 of sha256sum-amd64.txt of that release. That file holds the
# sha256 of every other RKE2 file, so this one value pins all of them.
readonly RKE2_CHECKSUMS_SHA256=bf32819ee12342c4982a7417312be1128a0d91938a3dcf6abd41834d77feb1d4
# RKE2's installer as it is in the RKE2 repository at the release tag.
# get.rke2.io serves the newest copy from the main branch, which changes
# between RKE2 releases, so the download takes the copy of the tag.
readonly RKE2_INSTALLER_SHA256=42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b
readonly RKE2_FILES=(rke2.linux-amd64.tar.gz rke2-images.linux-amd64.tar.zst)
# The Helm version that the chart's CI uses (.github/workflows/chart.yml).
readonly HELM_VERSION=v3.16.4
readonly HELM_SHA256=fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179

readonly MEHO_IMAGE_REPOSITORY=ghcr.io/evoila/meho
readonly MEHO_CHART_REPOSITORY=ghcr.io/evoila/meho-chart
readonly SIGNER_PREFIX=https://github.com/evoila/meho/.github/workflows
readonly OIDC_ISSUER=https://token.actions.githubusercontent.com

# GitHub accepts release files under 2 GiB. The build stops at 1.9 GiB.
readonly DEFAULT_MAX_BYTES=$((19 * 1024 * 1024 * 1024 / 10))

# RKE2 reads a zstd archive with at most 32 MiB of decoder memory
# (rancher/wharfie v0.7.1, pkg/tarfile, which RKE2 v1.37.1 uses), so the
# window of the image archive must fit in that.
readonly ZSTD_DECODER_MEMORY=32MiB

readonly IMAGE_PLATFORM=linux/amd64
readonly IMAGE_ARCHIVE=images/meho-images.linux-amd64.tar.zst
readonly VERSION_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

ok() { printf '[ok]   %s\n' "$*"; }

note() { printf '[note] %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

# fail <problem> <fix>: print one [FAIL] line that says what is wrong and
# what to do about it, then stop with exit code 1.
fail() {
    printf '[FAIL] %s Fix: %s\n' "$1" "$2" >&2
    exit 1
}

# on_error <exit code> <line> <file>: the ERR trap. A command substitution
# inherits the trap (set -E), so a failure inside one would be reported twice.
# The subshell only passes its exit code on, and the main shell reports once.
on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    printf '[FAIL] %s stopped at %s line %s: a command failed with exit code %s. Fix: read the lines above, correct the cause and run it again.\n' \
        "${0##*/}" "$3" "$2" "$1" >&2
    exit 1
}

# usage_error <message>: wrong arguments; stop with exit code 2.
usage_error() {
    printf '%s: %s\n' "${0##*/}" "$1" >&2
    printf "Run '%s --help' to see how to use it.\n" "${0##*/}" >&2
    exit 2
}

# need_tools <tool>...: stop when a tool is not on PATH.
need_tools() {
    local tool missing=()
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if ((${#missing[@]} > 0)); then
        fail "These tools are missing: ${missing[*]}." "install them and run ${0##*/} again."
    fi
}

sha256_of() { sha256sum -- "$1" | awk '{ print $1 }'; }

# join_lines <separator>: join the lines of stdin into one line. When there
# are more than 5 lines, only the first 5 are shown, and the total.
join_lines() {
    awk -v separator="$1" '
        NR <= 5 { joined = joined (NR > 1 ? separator : "") $0 }
        END { if (NR > 5) joined = joined separator "and " NR - 5 " more"; printf "%s", joined }'
}

# check_sha256 <file> <expected sha256> <what>
check_sha256() {
    local actual
    actual=$(sha256_of "$1")
    if [[ $actual != "$2" ]]; then
        fail "$3 has the sha256 $actual, but $2 is pinned." \
            "download it again. If the pinned value is out of date, check the new file and change the pin in deploy/linux-server/packaging/common.sh."
    fi
}

bytes_to_gib() { awk -v bytes="$1" 'BEGIN { printf "%.2f", bytes / 1073741824 }'; }

# check_size <file> <limit in bytes>: the download must stay below GitHub's
# limit for one release file.
check_size() {
    local file=$1 limit=$2 size
    size=$(stat -c %s -- "$file")
    if ((size > limit)); then
        fail "${file##*/} is $size bytes ($(bytes_to_gib "$size") GiB). The limit is $limit bytes ($(bytes_to_gib "$limit") GiB)." \
            "make the download smaller, or split it into two release files."
    fi
    ok "${file##*/} is $size bytes ($(bytes_to_gib "$size") GiB), below the limit of $(bytes_to_gib "$limit") GiB"
}

download_name() { printf 'meho-linux-%s-amd64' "$1"; }

# expected_layout <chart version>: every file of the download, relative to
# its top folder, one per line. It is the layout of the form-2 design,
# section 4, and it holds every file that install.sh reads
# (DOWNLOAD_FILES in lib/common.sh).
expected_layout() {
    local chart_version=$1 file
    printf '%s\n' install.sh meho-host VERSION
    for file in "$LINUX_SERVER_DIR"/lib/*.sh; do
        printf 'lib/%s\n' "${file##*/}"
    done
    printf '%s\n' \
        storage/local-path-storage.yaml \
        rke2/install.sh \
        rke2/sha256sum-amd64.txt
    printf 'rke2/%s\n' "${RKE2_FILES[@]}"
    printf '%s\n' \
        "$IMAGE_ARCHIVE" \
        "chart/meho-$chart_version.tgz" \
        chart/values-single-node.yaml \
        bin/helm \
        bin/meho \
        LICENSES/Apache-2.0.txt \
        LICENSES/MEHO-NOTICE.txt \
        LICENSES/README.txt
    for file in "$PACKAGING_DIR"/licenses/*.txt; do
        printf 'LICENSES/%s\n' "${file##*/}"
    done
}

# The files of the download that must be executable.
readonly EXECUTABLE_FILES=(install.sh meho-host rke2/install.sh bin/helm bin/meho)

# check_layout <download tar> <top folder> <chart version>: the tar holds
# exactly the files of expected_layout below one top folder, owned by root,
# with no file that others can change, and no links or special files.
check_layout() {
    local tar_file=$1 top=$2 chart_version=$3 actual expected missing extra bad
    actual=$(tar -tf "$tar_file" | grep -v '/$' | LC_ALL=C sort)
    expected=$(expected_layout "$chart_version" | sed "s|^|$top/|" | LC_ALL=C sort)
    missing=$(LC_ALL=C comm -13 <(printf '%s\n' "$actual") <(printf '%s\n' "$expected") | join_lines ' ')
    extra=$(LC_ALL=C comm -23 <(printf '%s\n' "$actual") <(printf '%s\n' "$expected") | join_lines ' ')
    if [[ -n $missing || -n $extra ]]; then
        fail "The download does not have the layout of the form-2 design. Missing: ${missing:-none}. Not expected: ${extra:-none}." \
            "build the download again with deploy/linux-server/build-download.sh."
    fi
    bad=$(tar -tvf "$tar_file" --numeric-owner | awk -v top="$top" -v executables="${EXECUTABLE_FILES[*]}" '
        BEGIN { n = split(executables, list, " "); for (i = 1; i <= n; i++) exe[top "/" list[i]] = 1 }
        {
            mode = $1; owner = $2; name = $6
            if (substr(mode, 1, 1) != "-" && substr(mode, 1, 1) != "d") { print name " (not a file or folder)"; next }
            if (owner != "0/0") { print name " (owner " owner ", not 0/0)"; next }
            if (substr(mode, 6, 1) == "w" || substr(mode, 9, 1) == "w") { print name " (" mode ", others can change it)"; next }
            if ((name in exe) && substr(mode, 4, 1) != "x") { print name " (" mode ", not executable)" }
        }' | join_lines '; ')
    if [[ -n $bad ]]; then
        fail "Some files of the download have the wrong owner or mode: $bad." \
            "build the download again with deploy/linux-server/build-download.sh, which sets owner root and safe modes."
    fi
    ok "the download has the layout of the form-2 design; every file is owned by root and only root can change it"
}
