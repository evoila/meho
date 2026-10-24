#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# install.sh: install form 2 of MEHO on a plain Linux server, with no
# internet. It runs as root from the unpacked download (form-2 design,
# section 4, in meho-internal docs/design/2026-10-10-install-forms/).
#
# It runs the six steps of the design (section 5):
#   1. check the machine                        lib/checks.sh
#   2. prepare the machine                      lib/prepare.sh
#   3. install and start RKE2, then wait        lib/rke2.sh
#   4. install the MEHO core with Helm          lib/core.sh
#   5. write the two one-time files             lib/core.sh
#   6. install the meho command, write the
#      install record and print the result      lib/core.sh
#
# --from-disk runs only the steps that need the host name: RKE2's settings,
# the start of RKE2, and steps 4 to 6. The appliance's first start uses it.
#
# Later Tasks add their steps in main() below: the host firewall to step 2
# (#4635), the CIS host steps between installing and starting RKE2 (#4636),
# meho-host (#4737), and the first-start list to step 6 (#4042).
#
# Exit codes:
#   0  MEHO is installed
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
# shellcheck source=lib/prepare.sh
source "$DOWNLOAD_DIR/lib/prepare.sh"
# shellcheck source=lib/rke2.sh
source "$DOWNLOAD_DIR/lib/rke2.sh"
# shellcheck source=lib/core.sh
source "$DOWNLOAD_DIR/lib/core.sh"

HOSTNAME_FQDN=""
PUBLIC_ADDRESS=""
FIRST_TENANT_ID=""
ADMIN_USER="admin"
APPROVER_USER="approver"
CLUSTER_CIDR="10.42.0.0/16"
SERVICE_CIDR="10.43.0.0/16"
JOIN_ADDRESS=""
JOIN_TOKEN_FILE=""
FROM_DISK=0

readonly UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# The user names go into Helm values and the install record as they are,
# so they take only characters that need no escaping there.
readonly USER_NAME_RE='^[A-Za-z0-9][A-Za-z0-9@._-]*$'

usage() {
    cat <<'HELP'
install.sh: install MEHO on this Linux server, with no internet (form 2).

It checks the machine, prepares it, installs and starts RKE2 (the built-in
Kubernetes), installs the MEHO core, writes the first-login passwords and
the unseal key to two files for root only, and installs the meho command.

Run it as root from the folder of the unpacked download:
  sudo ./install.sh --hostname meho.example.com

Options:
  --hostname <fqdn>          The full DNS name of this machine. It must
                             resolve through DNS, not only through /etc/hosts.
                             Default: the machine's own full name
                             (hostname -f).
  --public-address <ipv4>    Only behind address translation inside your
                             network: the address that the DNS name points
                             to, when this machine does not have it.
  --first-tenant-id <uuid>   The id of the first tenant. Default: a new
                             random id. Give the old id to keep it on a
                             move to another machine.
  --admin-user <name>        The user name of the first admin. Default: admin.
  --approver-user <name>     The user name of the second person, who
                             approves. Default: approver.
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
  -h, --help                 Show this help and stop.

Each option with a value also takes the form --option=value.

Exit codes:
  0  MEHO is installed
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
        --admin-user) ADMIN_USER=$2 ;;
        --approver-user) APPROVER_USER=$2 ;;
        --cluster-cidr) CLUSTER_CIDR=$2 ;;
        --service-cidr) SERVICE_CIDR=$2 ;;
        --join-address) JOIN_ADDRESS=$2 ;;
        --join-token-file) JOIN_TOKEN_FILE=$2 ;;
        *) usage_error "unknown option: $1" ;;
    esac
    [[ -n $2 && $2 != --* ]] || usage_error "$1 needs a value."
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
    if [[ -n $JOIN_ADDRESS || -n $JOIN_TOKEN_FILE ]]; then
        [[ -n $JOIN_ADDRESS && -n $JOIN_TOKEN_FILE ]] ||
            usage_error "--join-address and --join-token-file go together. Give both, or neither."
        [[ $JOIN_ADDRESS =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
            usage_error "--join-address must be a host name or an IPv4 address."
        [[ -f $JOIN_TOKEN_FILE && -r $JOIN_TOKEN_FILE && -s $JOIN_TOKEN_FILE ]] ||
            usage_error "--join-token-file $JOIN_TOKEN_FILE is not a readable file with a token in it."
    fi
    if ((FROM_DISK)) && [[ -n $PUBLIC_ADDRESS ]]; then
        usage_error "--from-disk runs no DNS check, so --public-address has no effect. Leave it out."
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

check_machine() {
    check_root
    check_os
    check_download "$DOWNLOAD_DIR" "${DOWNLOAD_FILES[@]}"
    read_download_version "$DOWNLOAD_DIR"
    check_cpus
    check_memory
    check_disk
    check_no_other_runtime
    check_ports
    check_clock
    check_hostname "$HOSTNAME_FQDN"
    check_dns "$HOSTNAME_FQDN" "$PUBLIC_ADDRESS"
    check_ranges "$CLUSTER_CIDR" "$SERVICE_CIDR"
}

prepare_machine() {
    prepare_folders
    write_rke2_config "$HOSTNAME_FQDN" "$CLUSTER_CIDR" "$SERVICE_CIDR" "$JOIN_ADDRESS"
    copy_join_token "$JOIN_TOKEN_FILE"
    copy_images "$DOWNLOAD_DIR"
    copy_storage_manifest "$DOWNLOAD_DIR"
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
        step "Step 1: check the machine"
        check_machine
        step "Step 2: prepare the machine"
        prepare_machine
        step "Step 3: install and start RKE2"
        install_rke2 "$DOWNLOAD_DIR"
    fi
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
        "$CLUSTER_CIDR" "$SERVICE_CIDR" "$FROM_DISK"
    print_result "$HOSTNAME_FQDN" "$unseal_key_written"
}

main "$@"
