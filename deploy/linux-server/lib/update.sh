# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The update (form-2 design, section 7): a run of install.sh on a machine
# where MEHO is installed updates it. This file finds the install, reads its
# record, checks the version step, asks for the backup and installs the OS
# packages of the appliance. install.sh runs the rest of the update with the
# functions of the other files. This file is sourced, not run.
#
# install.sh reads UPDATE, so shellcheck cannot see its use from this file
# alone.
# shellcheck disable=SC2034

# The public key of evoila's signed apt repository for the appliance's OS
# packages. The appliance build puts it there (#4814). A form-2 machine
# does not have it, because its customer keeps the OS up to date.
readonly MEHO_OS_PACKAGES_KEYRING=/etc/apt/keyrings/meho-os-packages.gpg
# The OS-package file is unpacked here for one run and removed after it.
readonly OS_PACKAGES_WORK_DIR=/var/tmp
# Ubuntu writes this file when an installed package needs a restart of the
# machine, for example a new kernel.
readonly REBOOT_REQUIRED_FILE=/run/reboot-required
readonly RKE2_VERSION_RE='^v([0-9]+)\.([0-9]+)\.([0-9]+)\+rke2r[0-9]+$'
readonly MEHO_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

# Set by find_install and read_install_record.
UPDATE=0
declare -A RECORD=()
OS_PACKAGES_WORK=""

# read_install_record: read /etc/meho/install.json into RECORD. install.sh
# writes the file itself, one key per line (render_install_record in
# lib/core.sh), so awk reads it without a JSON tool. The keys of "options"
# go into RECORD by their own names.
read_install_record() {
    local key value
    RECORD=()
    while IFS=$'\t' read -r key value; do
        RECORD[$key]=$value
    done < <(awk '
        match($0, /^[[:space:]]*"[a-z0-9_]+": /) {
            key = substr($0, RSTART, RLENGTH)
            gsub(/[[:space:]":]/, "", key)
            value = substr($0, RSTART + RLENGTH)
            sub(/,[[:space:]]*$/, "", value)
            if (value == "{") next
            if (value ~ /^".*"$/) value = substr(value, 2, length(value) - 2)
            print key "\t" value
        }' "${HOST_ROOT}${MEHO_INSTALL_RECORD}")
}

# record_value <key>: one value of the install record, or "" when the key
# is not in it.
record_value() { printf '%s' "${RECORD[$1]-}"; }

# version_order <a> <b>: print -1, 0 or 1 as version a is older than, the
# same as or newer than version b, by the precedence rules of Semantic
# Versioning 2.0.0, section 11. Both versions match MEHO_VERSION_RE.
version_order() {
    LC_ALL=C awk -v a="$1" -v b="$2" '
        function parse(version, parts,    dash, numbers) {
            dash = index(version, "-")
            split(dash ? substr(version, 1, dash - 1) : version, numbers, ".")
            parts[1] = numbers[1]
            parts[2] = numbers[2]
            parts[3] = numbers[3]
            parts["pre"] = dash ? substr(version, dash + 1) : ""
        }
        function pre_order(x, y,    xs, ys, nx, ny, i, x_number, y_number) {
            if (x == y) return 0
            if (x == "") return 1
            if (y == "") return -1
            nx = split(x, xs, ".")
            ny = split(y, ys, ".")
            for (i = 1; i <= nx && i <= ny; i++) {
                if (xs[i] == ys[i]) continue
                x_number = xs[i] ~ /^[0-9]+$/
                y_number = ys[i] ~ /^[0-9]+$/
                if (x_number && y_number) return xs[i] + 0 < ys[i] + 0 ? -1 : 1
                if (x_number) return -1
                if (y_number) return 1
                return (("" xs[i]) < ("" ys[i])) ? -1 : 1
            }
            return nx < ny ? -1 : (nx > ny ? 1 : 0)
        }
        BEGIN {
            parse(a, pa)
            parse(b, pb)
            result = 0
            for (i = 1; i <= 3 && result == 0; i++) {
                if (pa[i] + 0 != pb[i] + 0) result = (pa[i] + 0 < pb[i] + 0) ? -1 : 1
            }
            if (result == 0) result = pre_order(pa["pre"], pb["pre"])
            print result
        }'
}

# helm_release_exists <download dir>: true when Helm knows the release
# meho in the namespace meho, in any state. Exit code 2 when RKE2's API does
# not answer, so the release cannot be looked up.
helm_release_exists() {
    local releases
    releases=$("$1/$HELM_FILE" list --kubeconfig "${HOST_ROOT}${RKE2_KUBECONFIG}" \
        --namespace "$HELM_NAMESPACE" --all --filter "^${HELM_RELEASE}\$" --short 2>/dev/null) || return 2
    grep -qxF -- "$HELM_RELEASE" <<<"$releases"
}

# find_install <download dir> <update options given 0|1>: look for the three
# signs of an install (form-2 design, section 7): the install record, a
# running rke2-server and the Helm release meho. None of them: a fresh
# install. All three: an update, and UPDATE is 1. Only some: stop and change
# nothing. The release can be looked up only while RKE2 runs.
find_install() {
    local download_dir=$1 update_options=$2 record=0 rke2_runs=0 release=0 status=0
    [[ -f ${HOST_ROOT}${MEHO_INSTALL_RECORD} ]] && record=1
    systemctl is-active --quiet rke2-server.service 2>/dev/null && rke2_runs=1
    if ((rke2_runs)); then
        helm_release_exists "$download_dir" || status=$?
        ((status == 0)) && release=1
        ((status != 2)) ||
            fail "rke2-server runs, but its Kubernetes API does not answer, so install.sh cannot look for the Helm release $HELM_RELEASE. It changed nothing." \
                "wait a few minutes until RKE2 is ready, then run install.sh again. If this stays, run 'meho-host support-bundle' and send the file to support."
    fi
    if ((!record && !rke2_runs)); then
        ((!update_options)) ||
            fail "MEHO is not installed on this machine, so there is nothing to update. --confirm-backup and --os-packages are only for an update." \
                "for a first install, leave them out. For an update, run install.sh on the machine where MEHO is installed."
        ok "MEHO is not installed on this machine. This run installs MEHO $MEHO_VERSION."
        return 0
    fi
    if ((record && !rke2_runs)); then
        fail "The install record $MEHO_INSTALL_RECORD exists, but rke2-server does not run, so install.sh cannot check the install. It changed nothing." \
            "start RKE2 with 'systemctl start rke2-server', wait until 'systemctl is-active rke2-server' prints active, and run install.sh again. If RKE2 does not start, run 'meho-host support-bundle' and send the file to support."
    fi
    if ((!record || !release)); then
        local found=("rke2-server runs") missing=() verb=is
        if ((record)); then found+=("the install record $MEHO_INSTALL_RECORD exists"); else missing+=("the install record $MEHO_INSTALL_RECORD"); fi
        if ((release)); then found+=("the Helm release $HELM_RELEASE exists"); else missing+=("the Helm release $HELM_RELEASE"); fi
        ((${#missing[@]} == 1)) || verb=are
        fail "MEHO is only partly installed on this machine: $(join_by ' and ' "${found[@]}"), but $(join_by ' and ' "${missing[@]}") $verb missing. install.sh changed nothing." \
            "run 'meho-host support-bundle' and send the file to support."
    fi
    UPDATE=1
    read_install_record
}

# describe_update: the one line that says what is installed and what this
# run installs. Needs read_install_record and read_download_version.
describe_update() {
    local installed installed_at order
    installed=$(record_value meho_version)
    installed_at=$(record_value installed_at)
    [[ $installed =~ $MEHO_VERSION_RE ]] ||
        fail "The install record $MEHO_INSTALL_RECORD names MEHO version '$installed', which install.sh cannot read." \
            "run 'meho-host support-bundle' and send the file to support."
    [[ $MEHO_VERSION =~ $MEHO_VERSION_RE ]] ||
        fail "The VERSION file of the download names MEHO version '$MEHO_VERSION', which install.sh cannot read." \
            "unpack the whole download again, and run install.sh from its folder."
    order=$(version_order "$MEHO_VERSION" "$installed")
    case $order in
        1) ok "MEHO $installed is installed (${installed_at%%T*}). This run updates it to $MEHO_VERSION." ;;
        0) ok "MEHO $installed is installed (${installed_at%%T*}). This run installs the same version again." ;;
        *) ok "MEHO $installed is installed (${installed_at%%T*}). This download has the older MEHO $MEHO_VERSION." ;;
    esac
}

# refuse_install_options <option>...: an update keeps the answers of the
# install (form-2 design, section 7), so the options of a first install
# stop it.
refuse_install_options() {
    (($# > 0)) || return 0
    local unique=() option
    for option in "$@"; do
        [[ " ${unique[*]} " == *" $option "* ]] || unique+=("$option")
    done
    fail "An update keeps the install's answers. These options are only for a first install: $(join_by ', ' "${unique[@]}")." \
        "leave them out. The update reads the answers from $MEHO_INSTALL_RECORD. To change the SSH range, run 'meho-host admin-network <range>'."
}

# check_rke2_step <installed RKE2> <RKE2 of the download>: RKE2 moves at most
# one minor version per update. Kubernetes does not let the API server skip
# a minor version, and RKE2's upgrade page says the plan must not skip one.
check_rke2_step() {
    local installed=$1 new=$2 major minor new_major new_minor next
    [[ $installed =~ $RKE2_VERSION_RE ]] ||
        fail "The install record $MEHO_INSTALL_RECORD names RKE2 version '$installed', which install.sh cannot read." \
            "run 'meho-host support-bundle' and send the file to support."
    major=$((10#${BASH_REMATCH[1]}))
    minor=$((10#${BASH_REMATCH[2]}))
    [[ $new =~ $RKE2_VERSION_RE ]] ||
        fail "The VERSION file of the download names RKE2 version '$new', which install.sh cannot read." \
            "unpack the whole download again, and run install.sh from its folder."
    new_major=$((10#${BASH_REMATCH[1]}))
    new_minor=$((10#${BASH_REMATCH[2]}))
    if ((new_major != major)); then
        fail "RKE2 $installed is installed, and this download has RKE2 $new, another major version. install.sh does not update RKE2 to another major version." \
            "run 'meho-host support-bundle' and send the file to support."
    fi
    if ((new_minor < minor)); then
        fail "RKE2 $installed is installed, and this download has the older RKE2 $new. RKE2 cannot go back to an older minor version." \
            "use the download of a MEHO release with RKE2 v$major.$minor or newer."
    fi
    if ((new_minor > minor + 1)); then
        next="v$major.$((minor + 1))"
        fail "RKE2 $installed is installed, and this download has RKE2 $new. RKE2 moves only one minor version at a time, so RKE2 $next must come first." \
            "first install the MEHO release with RKE2 $next (the release notes name the RKE2 version of each MEHO release), then run this install.sh again."
    fi
    if ((new_minor == minor)); then
        ok "RKE2 $installed is installed. RKE2 $new of this download has the same minor version."
    else
        ok "RKE2 $installed is installed. RKE2 $new of this download is the next minor version."
    fi
}

# check_meho_step <installed MEHO> <MEHO of the download>: MEHO never goes
# back to an older version, because its database changes cannot be undone.
# The same version again is allowed: it repeats an update that stopped.
check_meho_step() {
    local installed=$1 new=$2
    if (($(version_order "$new" "$installed") < 0)); then
        fail "MEHO $installed is installed, and this download has the older MEHO $new. The database changes of MEHO $installed cannot be undone." \
            "use the download of MEHO $installed or newer. To go back to MEHO $new, revert the VM snapshot or restore the backup that you made before the update."
    fi
    ok "MEHO $new is not older than the installed MEHO $installed"
}

# confirm_backup <confirmed 0|1>: the update starts only after a VM snapshot
# or a backup (form-2 design, section 7). With --confirm-backup it does not
# ask. Without it, it asks on the terminal and goes on only after "yes".
# Without a terminal it stops. It takes no etcd snapshot of its own.
confirm_backup() {
    local answer=""
    if (($1)); then
        ok "--confirm-backup: a VM snapshot or a backup of this machine exists"
        return 0
    fi
    [[ -t 0 ]] ||
        fail "The update needs a VM snapshot or a backup of this machine first, and this run has no terminal to ask. Nothing was changed." \
            "take a VM snapshot or a backup, then run install.sh again with --confirm-backup."
    printf 'Did you take a VM snapshot or a backup of this machine? Type yes to go on: '
    read -r answer || printf '\n'
    [[ ${answer,,} == yes ]] ||
        fail "The update stopped, because the answer was not yes. Nothing was changed." \
            "take a VM snapshot or a backup of this machine, then run install.sh again and answer yes."
    ok "a VM snapshot or a backup of this machine exists"
}

# check_os_packages <file>: the key of the signed repository is on this
# machine, and the file is a flat apt repository (form-1 design, section 7):
# only plain files in its top folder, among them InRelease (the signed
# index), Packages (the package list that InRelease names) and the .deb
# files. apt checks the signature later. This check stops a wrong file
# before anything changes.
check_os_packages() {
    local file=$1 listing names missing=()
    [[ -s ${HOST_ROOT}${MEHO_OS_PACKAGES_KEYRING} ]] ||
        fail "--os-packages needs evoila's key for the OS packages in $MEHO_OS_PACKAGES_KEYRING, and this machine does not have it. Only the MEHO appliance has it." \
            "on the appliance, run the update with the OS-package file of the same release. On a Linux server, leave out --os-packages: you keep the OS up to date yourself."
    listing=$(LC_ALL=C tar -tvf "$file" 2>/dev/null) ||
        fail "The OS-package file $file is not a tar file that install.sh can read." \
            "use the OS-package file of the MEHO appliance release as it was published, and check its checksum."
    names=$(awk '
        $6 == "./" || $6 == "." { if (NF == 6 && $1 ~ /^d/) next }
        {
            name = $6
            sub(/^\.\//, "", name)
            if (NF != 6 || $1 !~ /^-/ || name !~ /^[A-Za-z0-9][A-Za-z0-9._+~-]*$/) { print "BAD " $6; exit }
            print name
        }' <<<"$listing")
    if [[ $names == *"BAD "* ]]; then
        fail "The OS-package file $file holds ${names##*BAD }, which is not a plain file in the top folder of a flat apt repository." \
            "use the OS-package file of the MEHO appliance release as it was published, and check its checksum."
    fi
    grep -qxF InRelease <<<"$names" || missing+=(InRelease)
    grep -qxF Packages <<<"$names" || missing+=(Packages)
    grep -qE '\.deb$' <<<"$names" || missing+=("a .deb file")
    if ((${#missing[@]} > 0)); then
        fail "The OS-package file $file is not a complete apt repository. Missing: $(join_by ', ' "${missing[@]}")." \
            "use the OS-package file of the MEHO appliance release as it was published, and check its checksum."
    fi
    ok "the OS-package file $file is a flat apt repository with $(grep -cE '\.deb$' <<<"$names") packages"
}

# remove_os_packages_work: remove the unpacked repository. It is also the
# EXIT trap while the folder exists.
remove_os_packages_work() {
    if [[ -n $OS_PACKAGES_WORK ]]; then
        rm -rf -- "$OS_PACKAGES_WORK"
        OS_PACKAGES_WORK=""
    fi
}

# unpack_os_packages <file>: unpack the repository into a new folder below
# /var/tmp, and write a sources list with only this repository, checked
# with evoila's key (signed-by, never trusted=yes). apt's download helper
# runs as the user _apt, so the folder and its files are readable for all.
unpack_os_packages() {
    local file=$1 work
    mkdir -p "${HOST_ROOT}${OS_PACKAGES_WORK_DIR}"
    work=$(mktemp -d "${HOST_ROOT}${OS_PACKAGES_WORK_DIR}/meho-os-packages.XXXXXX")
    OS_PACKAGES_WORK=$work
    trap remove_os_packages_work EXIT
    mkdir -p "$work/repo" "$work/lists/partial" "$work/sources.list.d"
    tar -xf "$file" -C "$work/repo" --no-same-owner --no-same-permissions ||
        fail "tar could not unpack the OS-package file $1 into $work. Its message is above." \
            "check the free space in $OS_PACKAGES_WORK_DIR, then run install.sh again."
    chmod 0755 "$work" "$work/repo" "$work/lists" "$work/sources.list.d"
    chmod 0644 "$work"/repo/*
    printf 'deb [signed-by=%s] file:%s ./\n' "${HOST_ROOT}${MEHO_OS_PACKAGES_KEYRING}" "$work/repo" \
        >"$work/os-packages.list"
    chmod 0644 "$work/os-packages.list"
}

# os_packages_apt_options <work dir>: apt's settings for this one run. The
# sources list names only the unpacked repository, the folder of more
# sources lists is empty, and the package lists and caches are in the work
# folder. So apt reads no other repository and needs no network, and the
# machine's own sources lists and package lists stay as they are
# (apt.conf(5): Dir::Etc::SourceList, Dir::Etc::SourceParts,
# Dir::State::Lists, Dir::Cache::pkgcache).
os_packages_apt_options() {
    printf '%s\n' \
        -o "Dir::Etc::SourceList=$1/os-packages.list" \
        -o "Dir::Etc::SourceParts=$1/sources.list.d" \
        -o "Dir::State::Lists=$1/lists" \
        -o "Dir::Cache::pkgcache=" \
        -o "Dir::Cache::srcpkgcache="
}

# read_os_packages_index <work dir>: apt reads the signed index. It stops
# when InRelease is not signed with the key, and when a file does not match
# its checksum in InRelease. apt reads a package list only when InRelease
# names it uncompressed, so a list that is only compressed is skipped with
# a warning; the check after the run finds that.
read_os_packages_index() {
    local work=$1 options
    mapfile -t options < <(os_packages_apt_options "$work")
    apt-get "${options[@]}" update --error-on=any >/dev/null ||
        fail "apt could not read the OS-package file: the signature or a checksum does not match, or a file is missing. apt's message is above." \
            "use the OS-package file of the MEHO appliance release as it was published, and check its checksum."
    compgen -G "$work/lists/*_Packages" >/dev/null ||
        fail "apt found no package list in the OS-package file: InRelease names no uncompressed Packages file." \
            "use the OS-package file of the MEHO appliance release as it was published, and check its checksum."
    ok "apt checked the signature of the OS-package file with $MEHO_OS_PACKAGES_KEYRING"
}

# upgrade_os_packages <work dir>: install the newer versions of the
# installed packages from the repository. "upgrade --with-new-pkgs" may add
# a package that an update needs, and never removes one. A configuration
# file that the hardening changed keeps its version (dpkg --force-confold).
# apt's own output is in /var/log/apt/term.log.
upgrade_os_packages() {
    local work=$1 options
    mapfile -t options < <(os_packages_apt_options "$work")
    DEBIAN_FRONTEND=noninteractive apt-get "${options[@]}" -y -q \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        upgrade --with-new-pkgs >/dev/null ||
        fail "apt could not install the OS packages. apt's message is above, and its log is /var/log/apt/term.log." \
            "revert the VM snapshot, then run 'meho-host support-bundle' and send the file to support."
    ok "installed the OS updates from the OS-package file (apt's log: /var/log/apt/term.log)"
}

# install_os_packages <file>: only on the appliance (form-2 design, section
# 7, step 3; form-1 design, section 7).
install_os_packages() {
    unpack_os_packages "$1"
    read_os_packages_index "$OS_PACKAGES_WORK"
    upgrade_os_packages "$OS_PACKAGES_WORK"
    remove_os_packages_work
    trap - EXIT
    if [[ -f ${HOST_ROOT}${REBOOT_REQUIRED_FILE} ]]; then
        note "the new OS packages need a restart of this machine. Restart it after the update; RKE2 and MEHO start again by themselves."
    fi
}

# print_update_result <hostname> <MEHO version before the update>
print_update_result() {
    printf '\ninstall.sh: MEHO %s is installed on %s. Before this run: MEHO %s.\n' "$MEHO_VERSION" "$1" "$2"
    printf '  Console:               https://%s/ui/\n' "$1"
    printf '  Kept as they were:     the firewall, the data, the passwords and the keys\n'
    printf '  First-start list:      not in this version yet. Open the console address to check that MEHO answers.\n'
}
