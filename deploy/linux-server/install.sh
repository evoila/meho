#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# install.sh: install form 2 of MEHO on a plain Linux server, with no
# internet. It runs as root from the unpacked download (form-2 design,
# section 4, in meho-internal docs/design/2026-10-10-install-forms/).
#
# This version runs steps 1 to 3 of the design (section 5):
#   1. check the machine                    lib/checks.sh
#   2. prepare the machine                  lib/prepare.sh
#   3. install and start RKE2, then wait    lib/rke2.sh
#
# Later Tasks add their steps in main() below: the host firewall to step 2
# (#4635), the CIS host steps between installing and starting RKE2 (#4636),
# and the core after step 3 (#4735).
#
# Exit codes:
#   0  RKE2 and its default storage class are ready
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

HOSTNAME_FQDN=""
PUBLIC_ADDRESS=""
CLUSTER_CIDR="10.42.0.0/16"
SERVICE_CIDR="10.43.0.0/16"
JOIN_ADDRESS=""
JOIN_TOKEN_FILE=""

usage() {
    cat <<'HELP'
install.sh: install MEHO on this Linux server, with no internet (form 2).

This version checks the machine, prepares it, and installs and starts RKE2
(the built-in Kubernetes) with its default storage class.

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
  --cluster-cidr <cidr>      The address range of the pods.
                             Default: 10.42.0.0/16.
  --service-cidr <cidr>      The address range of the services.
                             Default: 10.43.0.0/16.
  --join-address <host>      Join the RKE2 server at this address. Use it
                             together with --join-token-file. Not tested yet.
  --join-token-file <path>   A file that holds the join token. The token is
                             copied to /etc/meho/join-token and never printed.
  -h, --help                 Show this help and stop.

Each option also takes the form --option=value.

Exit codes:
  0  RKE2 and its default storage class are ready
  1  a check or a step failed; the [FAIL] line says what to fix
  2  wrong usage: an unknown option or a bad value
HELP
}

# set_option <name> <value>
set_option() {
    case $1 in
        --hostname) HOSTNAME_FQDN=${2,,} ;;
        --public-address) PUBLIC_ADDRESS=$2 ;;
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
    check_download "$DOWNLOAD_DIR"
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
    install_meho_host "$DOWNLOAD_DIR"
}

main() {
    parse_options "$@"
    validate_options
    if [[ -z $HOSTNAME_FQDN ]]; then
        HOSTNAME_FQDN=$(hostname -f 2>/dev/null || true)
        HOSTNAME_FQDN=${HOSTNAME_FQDN,,}
    fi
    trap 'on_error "$?" "$LINENO" "${BASH_SOURCE[0]##*/}"' ERR

    step "Step 1: check the machine"
    check_machine
    step "Step 2: prepare the machine"
    prepare_machine
    step "Step 3: install and start RKE2"
    install_rke2 "$DOWNLOAD_DIR"
    start_rke2
    wait_for_rke2

    printf '\ninstall.sh: RKE2 and its default storage class are ready on %s.\n' "$HOSTNAME_FQDN"
}

main "$@"
