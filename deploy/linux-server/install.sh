#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# install.sh: install form 2 of MEHO on a plain Linux server, with no
# internet. It runs as root from the unpacked download (form-2 design,
# section 4, in meho-internal docs/design/2026-10-10-install-forms/).
#
# On a machine without MEHO it runs the six steps of the design (section 5):
#   1. check the machine                        lib/checks.sh, lib/firewall.sh
#   2. prepare the machine: the host            lib/firewall.sh, lib/prepare.sh
#      firewall first, then RKE2's files and
#      meho-host
#   3. install RKE2, prepare the host for its   lib/rke2.sh, lib/cis.sh
#      CIS profile, start RKE2, then wait
#   4. install the MEHO core with Helm          lib/core.sh
#   5. write the two one-time files             lib/core.sh
#   6. install the meho command, write the
#      install record and print the result      lib/core.sh
#
# RKE2 runs with its CIS hardening profile from the first install (ADR 0009).
#
# Before step 1 it finds the install (lib/update.sh). On a machine where MEHO
# is installed, the run is an update (section 7): it keeps the answers of the
# install, checks the machine and the version step, asks for a backup,
# installs the appliance's OS packages with --os-packages, updates RKE2 and
# the core, and leaves the firewall, the one-time files, the data and the
# Secrets alone. On a half-installed machine it stops and changes nothing.
#
# --from-disk runs only the steps that need the host name: RKE2's settings,
# the start of RKE2, and steps 4 to 6. Before the start it also runs the
# host steps of the CIS profile, which change nothing where a full install
# ran before. It leaves the firewall alone and does not look for an install.
# The appliance's first start uses it.
#
# Later Tasks add their steps in main() below: the first-start list to
# step 6 (#4042).
#
# Exit codes:
#   0  MEHO is installed or updated
#   1  a check or a step failed; the [FAIL] line says what to fix
#   2  wrong usage: an unknown option or a bad value
#
# The tests in tests/ run this script against a fake machine. See
# docs/codebase/linux-server-install.md.

# RKE2's own installer runs with "sh", so an admin may start this script the
# same way. It needs bash, so it stops at once with a clear message.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "install.sh: run it with bash, for example: sudo ./install.sh" >&2
    exit 2
fi

set -Eeuo pipefail

DOWNLOAD_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly DOWNLOAD_DIR

# shellcheck source=lib/common.sh
source "$DOWNLOAD_DIR/lib/common.sh"
# shellcheck source=lib/net.sh
source "$DOWNLOAD_DIR/lib/net.sh"
# shellcheck source=lib/checks.sh
source "$DOWNLOAD_DIR/lib/checks.sh"
# shellcheck source=lib/firewall.sh
source "$DOWNLOAD_DIR/lib/firewall.sh"
# shellcheck source=lib/cis.sh
source "$DOWNLOAD_DIR/lib/cis.sh"
# shellcheck source=lib/prepare.sh
source "$DOWNLOAD_DIR/lib/prepare.sh"
# shellcheck source=lib/rke2.sh
source "$DOWNLOAD_DIR/lib/rke2.sh"
# shellcheck source=lib/core.sh
source "$DOWNLOAD_DIR/lib/core.sh"
# shellcheck source=lib/update.sh
source "$DOWNLOAD_DIR/lib/update.sh"

HOSTNAME_FQDN=""
PUBLIC_ADDRESS=""
FIRST_TENANT_ID=""
ADMIN_USER="admin"
APPROVER_USER="approver"
CLUSTER_CIDR="10.42.0.0/16"
SERVICE_CIDR="10.43.0.0/16"
JOIN_ADDRESS=""
JOIN_TOKEN_FILE=""
ADMIN_NETWORK=""
FROM_DISK=0
CONFIRM_BACKUP=0
OS_PACKAGES_FILE=""
RECORD_FROM_DISK=0
# The options of a first install that this run got. An update refuses them.
INSTALL_OPTIONS_GIVEN=()
UPDATE_STEP=0

readonly UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# The user names go into Helm values and the install record as they are,
# so they take only characters that need no escaping there. Keycloak keeps
# user names in lower case, so set_option lowers them first.
readonly USER_NAME_RE='^[a-z0-9][a-z0-9@._-]*$'
readonly JOIN_ADDRESS_RE='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'

usage() {
    cat <<'HELP'
install.sh: install MEHO on this Linux server, with no internet (form 2).

It checks the machine, sets the host firewall, prepares the machine,
installs and starts RKE2 (the built-in Kubernetes) with its CIS hardening
profile, installs the MEHO core, writes the first-login passwords and the
unseal key to two files for root only, and installs the meho command.

Run it as root from the folder of the unpacked download:
  sudo ./install.sh --hostname meho.example.com --admin-network 10.0.50.0/24

On a machine where MEHO is installed, the same command without options
updates MEHO to the version of this download. The update keeps the answers
of the install, the firewall, the data, the passwords and the keys. Take a
VM snapshot or a backup first:
  sudo ./install.sh --confirm-backup

Options:
  --hostname <fqdn>          The full DNS name of this machine. It must
                             resolve through DNS, not only through /etc/hosts.
                             Default: the machine's own full name
                             (hostname -f).
  --public-address <ipv4>    Only behind address translation inside your
                             network: the address that the DNS name points
                             to, when this machine does not have it.
  --admin-network <range>    The IPv4 addresses that may use SSH: a range
                             such as 10.0.50.0/24, one address, or "none"
                             to close SSH. From every other address, only
                             ports 80 and 443 answer. Default: only the
                             address of the SSH session that runs
                             install.sh. On the machine's console the
                             option is required.
  --first-tenant-id <uuid>   The id of the first tenant. Default: a new
                             random id. Give the old id to keep it on a
                             move to another machine.
  --admin-user <name>        The user name of the first admin, stored in
                             lower case. Default: admin.
  --approver-user <name>     The user name of the second person, who
                             approves, stored in lower case.
                             Default: approver.
  --cluster-cidr <cidr>      The address range of the pods.
                             Default: 10.42.0.0/16.
  --service-cidr <cidr>      The address range of the services.
                             Default: 10.43.0.0/16.
  --join-address <host>      Join the RKE2 server at this address. Use it
                             together with --join-token-file. Not tested yet.
  --join-token-file <path>   A file that holds the join token. The token is
                             copied to /etc/meho/join-token and never printed.
  --from-disk                Only on a machine where RKE2's own state was
                             removed, but its files, the chart and the
                             images are still on the disk: write RKE2's
                             settings, start RKE2 and install the core
                             again. It needs no download and does not check
                             the machine. The appliance's first start uses it.
  --confirm-backup           Only for an update: a VM snapshot or a backup
                             of this machine exists. Without it, install.sh
                             asks on the terminal, and stops when there is
                             no terminal.
  --os-packages <file>       Only for an update of the MEHO appliance: first
                             install the OS updates from the appliance's
                             OS-package file, with no network.
  -h, --help                 Show this help and stop.

Each option with a value also takes the form --option=value.

Exit codes:
  0  MEHO is installed or updated
  1  a check or a step failed; the [FAIL] line says what to fix
  2  wrong usage: an unknown option or a bad value
HELP
}

# set_option <name> <value>
set_option() {
    case $1 in
        --hostname) HOSTNAME_FQDN=${2,,} ;;
        --public-address) PUBLIC_ADDRESS=$2 ;;
        --first-tenant-id) FIRST_TENANT_ID=${2,,} ;;
        --admin-user) ADMIN_USER=${2,,} ;;
        --approver-user) APPROVER_USER=${2,,} ;;
        --cluster-cidr) CLUSTER_CIDR=$2 ;;
        --service-cidr) SERVICE_CIDR=$2 ;;
        --join-address) JOIN_ADDRESS=$2 ;;
        --join-token-file) JOIN_TOKEN_FILE=$2 ;;
        --admin-network) ADMIN_NETWORK=$2 ;;
        --os-packages) OS_PACKAGES_FILE=$2 ;;
        *) usage_error "unknown option: $1" ;;
    esac
    [[ -n $2 && $2 != --* ]] || usage_error "$1 needs a value."
    if [[ $1 != --os-packages ]]; then
        INSTALL_OPTIONS_GIVEN+=("$1")
    fi
}

parse_options() {
    local name value
    while (($# > 0)); do
        case $1 in
            -h | --help)
                usage
                exit 0
                ;;
            --from-disk)
                FROM_DISK=1
                shift
                continue
                ;;
            --confirm-backup)
                CONFIRM_BACKUP=1
                shift
                continue
                ;;
            --*=*)
                name=${1%%=*}
                value=${1#*=}
                shift
                ;;
            --*)
                name=$1
                value=${2-}
                shift
                if (($# > 0)); then
                    shift
                fi
                ;;
            *) usage_error "unknown argument: $1" ;;
        esac
        set_option "$name" "$value"
    done
}

validate_options() {
    if [[ -n $PUBLIC_ADDRESS ]] && ! is_ipv4 "$PUBLIC_ADDRESS"; then
        usage_error "--public-address must be an IPv4 address, for example 192.0.2.10."
    fi
    if [[ -n $FIRST_TENANT_ID && ! $FIRST_TENANT_ID =~ $UUID_RE ]]; then
        usage_error "--first-tenant-id must be a UUID, for example 3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c."
    fi
    [[ $ADMIN_USER =~ $USER_NAME_RE ]] ||
        usage_error "--admin-user must be a user name of letters, digits and . _ - @, for example admin."
    [[ $APPROVER_USER =~ $USER_NAME_RE ]] ||
        usage_error "--approver-user must be a user name of letters, digits and . _ - @, for example approver."
    [[ $ADMIN_USER != "$APPROVER_USER" ]] ||
        usage_error "--admin-user and --approver-user must be two different names."
    is_ipv4_cidr "$CLUSTER_CIDR" ||
        usage_error "--cluster-cidr must be an IPv4 range, for example 10.42.0.0/16."
    is_ipv4_cidr "$SERVICE_CIDR" ||
        usage_error "--service-cidr must be an IPv4 range, for example 10.43.0.0/16."
    if [[ -n $ADMIN_NETWORK && $ADMIN_NETWORK != none ]] && ! is_ipv4 "$ADMIN_NETWORK"; then
        is_ipv4_cidr "$ADMIN_NETWORK" ||
            usage_error "--admin-network must be an IPv4 range such as 10.0.50.0/24, one IPv4 address, or none."
        is_ipv4_network "$ADMIN_NETWORK" ||
            usage_error "--admin-network $ADMIN_NETWORK does not start at the first address of its range. Give the first address, for example 10.0.50.0/24, or one address without a prefix."
    fi
    if [[ -n $JOIN_ADDRESS || -n $JOIN_TOKEN_FILE ]]; then
        [[ -n $JOIN_ADDRESS && -n $JOIN_TOKEN_FILE ]] ||
            usage_error "--join-address and --join-token-file go together. Give both, or neither."
        [[ $JOIN_ADDRESS =~ $JOIN_ADDRESS_RE ]] ||
            usage_error "--join-address must be a host name or an IPv4 address."
        [[ -f $JOIN_TOKEN_FILE && -r $JOIN_TOKEN_FILE && -s $JOIN_TOKEN_FILE ]] ||
            usage_error "--join-token-file $JOIN_TOKEN_FILE is not a readable file with a token in it."
    fi
    if ((FROM_DISK)) && [[ -n $PUBLIC_ADDRESS ]]; then
        usage_error "--from-disk runs no DNS check, so --public-address has no effect. Leave it out."
    fi
    if ((FROM_DISK)) && { ((CONFIRM_BACKUP)) || [[ -n $OS_PACKAGES_FILE ]]; }; then
        usage_error "--from-disk installs the core again and updates nothing, so --confirm-backup and --os-packages have no effect. Leave them out."
    fi
    if [[ -n $OS_PACKAGES_FILE ]]; then
        [[ -f $OS_PACKAGES_FILE && -r $OS_PACKAGES_FILE ]] ||
            usage_error "--os-packages $OS_PACKAGES_FILE is not a readable file. Give the OS-package file of the MEHO appliance."
    fi
}

# on_error <exit code> <line> <file>: the ERR trap. A command substitution
# inherits the trap (set -E), so a failure inside one would be reported twice.
# The subshell only passes its exit code on, and the main shell reports once.
on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    printf '[FAIL] install.sh stopped at %s line %s: a command failed with exit code %s. Fix: read the lines above, correct the cause and run install.sh again.\n' \
        "$3" "$2" "$1" >&2
    exit 1
}

# find_the_install: the checks every run needs first, then find_install of
# lib/update.sh, which sets UPDATE.
find_the_install() {
    local update_options=0
    if ((CONFIRM_BACKUP)) || [[ -n $OS_PACKAGES_FILE ]]; then
        update_options=1
    fi
    check_root
    check_download "$DOWNLOAD_DIR" "${DOWNLOAD_FILES[@]}"
    read_download_version "$DOWNLOAD_DIR"
    find_install "$DOWNLOAD_DIR" "$update_options"
}

check_machine() {
    check_os
    check_cpus
    check_memory
    check_disk
    check_no_other_runtime
    check_ports
    check_clock
    check_hostname "$HOSTNAME_FQDN"
    check_dns "$HOSTNAME_FQDN" "$PUBLIC_ADDRESS"
    check_ranges "$CLUSTER_CIDR" "$SERVICE_CIDR"
    check_nftables
    check_admin_network "$ADMIN_NETWORK"
    check_join_firewall "$JOIN_ADDRESS"
}

prepare_machine() {
    prepare_folders
    set_up_firewall "$CLUSTER_CIDR" "$SERVICE_CIDR"
    write_rke2_config "$HOSTNAME_FQDN" "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS"
    copy_join_token "$JOIN_TOKEN_FILE"
    copy_images "$DOWNLOAD_DIR"
    copy_storage_manifest "$DOWNLOAD_DIR"
    write_traefik_config
    install_meho_host "$DOWNLOAD_DIR"
}

# use_install_answers: the answers of the install, from its record, in the
# same names as the options of a first install. The record is install.sh's
# own file, so a value that breaks the rules of its option means that the
# file was changed by hand. A record of an older install.sh has no
# join_address; the install then had no join options.
use_install_answers() {
    local key bad=()
    for key in meho_version rke2_version installed_at hostname first_tenant_id \
        admin_user approver_user cluster_cidr service_cidr; do
        [[ -n $(record_value "$key") ]] || bad+=("$key")
    done
    ((${#bad[@]} == 0)) ||
        fail "The install record $MEHO_INSTALL_RECORD has no value for: $(join_by ', ' "${bad[@]}")." \
            "run 'meho-host support-bundle' and send the file to support."
    HOSTNAME_FQDN=$(record_value hostname)
    PUBLIC_ADDRESS=$(record_value public_address)
    FIRST_TENANT_ID=$(record_value first_tenant_id)
    ADMIN_USER=$(record_value admin_user)
    APPROVER_USER=$(record_value approver_user)
    CLUSTER_CIDR=$(record_value cluster_cidr)
    SERVICE_CIDR=$(record_value service_cidr)
    JOIN_ADDRESS=$(record_value join_address)
    if [[ $(record_value from_disk) == true ]]; then
        RECORD_FROM_DISK=1
    fi
    is_fqdn "$HOSTNAME_FQDN" || bad+=(hostname)
    [[ -z $PUBLIC_ADDRESS ]] || is_ipv4 "$PUBLIC_ADDRESS" || bad+=(public_address)
    [[ $FIRST_TENANT_ID =~ $UUID_RE ]] || bad+=(first_tenant_id)
    [[ $ADMIN_USER =~ $USER_NAME_RE ]] || bad+=(admin_user)
    [[ $APPROVER_USER =~ $USER_NAME_RE ]] || bad+=(approver_user)
    is_ipv4_cidr "$CLUSTER_CIDR" || bad+=(cluster_cidr)
    is_ipv4_cidr "$SERVICE_CIDR" || bad+=(service_cidr)
    [[ -z $JOIN_ADDRESS || $JOIN_ADDRESS =~ $JOIN_ADDRESS_RE ]] || bad+=(join_address)
    ((${#bad[@]} == 0)) ||
        fail "The install record $MEHO_INSTALL_RECORD has values that install.sh cannot use: $(join_by ', ' "${bad[@]}")." \
            "run 'meho-host support-bundle' and send the file to support."
}

update_step() {
    UPDATE_STEP=$((UPDATE_STEP + 1))
    step "Update step $UPDATE_STEP: $1"
}

# update_meho: the update of form-2 design, section 7, on a machine where
# find_install found all three signs of an install. It leaves alone the
# firewall (its SSH range changes only with meho-host admin-network), the
# two one-time files, the volumes, OpenBao's data and the Secrets: the
# chart's secrets Job creates only the Secrets that do not exist.
update_meho() {
    local installed_version rke2_run
    use_install_answers
    describe_update
    refuse_install_options "${INSTALL_OPTIONS_GIVEN[@]}"
    installed_version=$(record_value meho_version)

    update_step "check the machine and the versions"
    check_os
    check_cpus
    check_memory
    check_disk
    check_clock
    check_hostname "$HOSTNAME_FQDN"
    check_dns "$HOSTNAME_FQDN" "$PUBLIC_ADDRESS"
    if [[ -n $OS_PACKAGES_FILE ]]; then
        check_os_packages "$OS_PACKAGES_FILE"
    fi
    check_rke2_step "$(record_value rke2_version)" "$RKE2_VERSION"
    check_meho_step "$installed_version" "$MEHO_VERSION"

    update_step "confirm a backup"
    confirm_backup "$CONFIRM_BACKUP"

    if [[ -n $OS_PACKAGES_FILE ]]; then
        update_step "install the OS packages of the appliance"
        install_os_packages "$OS_PACKAGES_FILE"
    fi

    update_step "update RKE2"
    write_rke2_config "$HOSTNAME_FQDN" "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS"
    copy_images "$DOWNLOAD_DIR"
    copy_storage_manifest "$DOWNLOAD_DIR"
    write_traefik_config
    install_rke2 "$DOWNLOAD_DIR"
    prepare_cis_host
    rke2_run=$(rke2_invocation_id)
    restart_rke2
    wait_for_rke2 "$rke2_run"

    update_step "update the MEHO core"
    write_place_values "$HOSTNAME_FQDN" "$FIRST_TENANT_ID" "$ADMIN_USER" "$APPROVER_USER" "$CLUSTER_CIDR"
    install_core "$DOWNLOAD_DIR"

    update_step "install the meho command and meho-host, and write the install record"
    install_cli "$DOWNLOAD_DIR"
    install_meho_host "$DOWNLOAD_DIR"
    write_install_record "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$HOSTNAME_FQDN" "$PUBLIC_ADDRESS" "$FIRST_TENANT_ID" "$ADMIN_USER" "$APPROVER_USER" \
        "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS" "$RECORD_FROM_DISK"
    print_update_result "$HOSTNAME_FQDN" "$installed_version"
}

main() {
    parse_options "$@"
    validate_options
    if [[ -z $HOSTNAME_FQDN ]]; then
        HOSTNAME_FQDN=$(hostname -f 2>/dev/null || true)
        HOSTNAME_FQDN=${HOSTNAME_FQDN,,}
    fi
    trap 'on_error "$?" "$LINENO" "${BASH_SOURCE[0]##*/}"' ERR

    if ((FROM_DISK)); then
        step "Step 1: check the host name and the files on the disk"
        check_root
        check_hostname "$HOSTNAME_FQDN"
        check_download "$DOWNLOAD_DIR" "${CORE_DOWNLOAD_FILES[@]}"
        read_download_version "$DOWNLOAD_DIR"
        step "Step 2: write RKE2's settings"
        write_rke2_config "$HOSTNAME_FQDN" "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS"
        step "Step 3: start RKE2"
    else
        step "Find the install"
        find_the_install
        if ((UPDATE)); then
            update_meho
            return 0
        fi
        step "Step 1: check the machine"
        check_machine
        step "Step 2: prepare the machine"
        prepare_machine
        step "Step 3: install and start RKE2"
        install_rke2 "$DOWNLOAD_DIR"
    fi
    prepare_cis_host
    start_rke2
    wait_for_rke2

    step "Step 4: install the MEHO core"
    if [[ -z $FIRST_TENANT_ID ]]; then
        FIRST_TENANT_ID=$(tenant_id)
    fi
    write_place_values "$HOSTNAME_FQDN" "$FIRST_TENANT_ID" "$ADMIN_USER" "$APPROVER_USER" "$CLUSTER_CIDR"
    install_core "$DOWNLOAD_DIR"

    step "Step 5: write the two one-time files"
    copy_first_login
    local unseal_key_written=1
    copy_unseal_key || unseal_key_written=0

    step "Step 6: install the meho command and write the install record"
    install_cli "$DOWNLOAD_DIR"
    write_install_record "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$HOSTNAME_FQDN" "$PUBLIC_ADDRESS" "$FIRST_TENANT_ID" "$ADMIN_USER" "$APPROVER_USER" \
        "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS" "$FROM_DISK"
    print_result "$HOSTNAME_FQDN" "$unseal_key_written"
}

main "$@"
