# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The target networks (#6828): the networks of the systems that MEHO
# manages. MEHO's target guard refuses a target on a non-public address,
# such as a vCenter on 10.20.0.5, unless MEHO_TARGET_SSRF_ALLOWLIST (the
# chart value config.targetSsrfAllowlist) lists it
# (backend/src/meho_backplane/targets/ssrf_guard.py). install.sh
# --target-networks and meho-host target-networks set that list, and both
# check it here, with one rule. install.sh sources this file; meho-host
# sources its copy in /usr/local/lib/meho-host. This file is sourced, not
# run.
#
# It needs is_ipv4, is_ipv4_network, cidrs_overlap and PREFIX_RE of the
# script that sources it: lib/net.sh in install.sh, meho-host's own copies.
#
# The rule:
# - an entry is an IPv4 range that starts at its first address
#   (10.20.0.0/16), one IPv4 address (10.20.0.5), or a host name
#   (vcenter.example.com). An address or a host name may carry a port
#   (vcenter.example.com:443); the guard then admits it only on that port.
#   The guard cannot read a port on a range, and form 2 reaches the systems
#   it manages only over IPv4, so neither is accepted;
# - no entry opens MEHO itself: not the pod range or the service range of
#   the built-in Kubernetes, not the addresses of the meho pod itself
#   (0.0.0.0/8 and 127.0.0.0/8 reach its own ports), not the link-local
#   range (a cloud's metadata service), and no name of the built-in
#   Kubernetes or of the pod itself (cluster.local, localhost);
# - "none" is the empty list: MEHO accepts no target on a private address.
#
# install.sh and meho-host read TARGET_NETWORKS and TARGET_NETWORKS_PROBLEM,
# so shellcheck cannot see their use from this file alone.
# shellcheck disable=SC2034

readonly TARGET_PORT_RE='^[1-9][0-9]{0,4}$'
readonly TARGET_HOST_RE='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
# The ranges no entry may overlap, besides the two ranges of the install.
readonly TARGET_CLOSED_RANGES=(0.0.0.0/8 127.0.0.0/8 169.254.0.0/16)

# Set by parse_target_networks.
TARGET_NETWORKS=""
TARGET_NETWORKS_PROBLEM=""

# target_problem <text>: keep the problem of the list for the caller's
# message.
target_problem() {
    TARGET_NETWORKS_PROBLEM=$1
}

# target_closed_range <range> <pod range> <service range>: print the first
# range that no entry may overlap and that the range overlaps, with what it
# is. False when there is none.
target_closed_range() {
    local range=$1 closed
    if cidrs_overlap "$range" "$2"; then
        printf '%s, the pod range of the built-in Kubernetes' "$2"
        return 0
    fi
    if cidrs_overlap "$range" "$3"; then
        printf '%s, the service range of the built-in Kubernetes' "$3"
        return 0
    fi
    for closed in "${TARGET_CLOSED_RANGES[@]}"; do
        cidrs_overlap "$range" "$closed" || continue
        case $closed in
            169.254.0.0/16) printf '%s, the link-local range' "$closed" ;;
            *) printf '%s, the addresses of the MEHO pod itself' "$closed" ;;
        esac
        return 0
    done
    return 1
}

# check_target_entry <entry> <pod range> <service range>: true when the
# entry follows the rule above. Otherwise false, with the problem in
# TARGET_NETWORKS_PROBLEM.
check_target_entry() {
    local entry=$1 host=$1 port="" range closed
    local others="Name only the networks of the systems that MEHO manages, for example 10.20.0.0/16."
    if [[ $entry == *[[:space:]]* ]]; then
        target_problem "'$entry' holds a space. Put a comma between two entries, for example 10.20.0.0/16,10.30.0.0/16."
        return 1
    fi
    if [[ $entry == \[* || $entry == *:*:* ]]; then
        target_problem "$entry is not an IPv4 entry. MEHO on a Linux server reaches the systems it manages only over IPv4."
        return 1
    fi
    if [[ $entry == */* ]]; then
        if [[ $entry == *:* ]]; then
            target_problem "$entry has a port, which only one address or a host name can have, for example 10.20.0.5:443."
            return 1
        fi
        if ! is_ipv4 "${entry%/*}" || ! [[ ${entry#*/} =~ $PREFIX_RE ]]; then
            target_problem "$entry is not an IPv4 range such as 10.20.0.0/16."
            return 1
        fi
        if ! is_ipv4_network "$entry"; then
            target_problem "$entry does not start at the first address of its range. Give the first address, for example 10.20.0.0/16."
            return 1
        fi
        range=$entry
    else
        if [[ $entry == *:* ]]; then
            host=${entry%:*}
            port=${entry##*:}
            if ! [[ $port =~ $TARGET_PORT_RE ]] || ((port > 65535)); then
                target_problem "$entry has a port that is not a number from 1 to 65535."
                return 1
            fi
        fi
        if ! [[ $host =~ ^[0-9.]+$ ]]; then
            if ! [[ $host =~ $TARGET_HOST_RE ]] || ((${#host} > 253)); then
                target_problem "$entry is not an IPv4 range, an IPv4 address or a host name."
                return 1
            fi
            case $host in
                localhost | *.localhost | cluster.local | *.cluster.local)
                    target_problem "$host is a name of the MEHO pod itself or of the built-in Kubernetes. $others"
                    return 1
                    ;;
            esac
            return 0
        fi
        if ! is_ipv4 "$host"; then
            target_problem "$host is not an IPv4 address such as 10.20.0.5."
            return 1
        fi
        range=$host
    fi
    if closed=$(target_closed_range "$range" "$2" "$3"); then
        target_problem "$entry overlaps $closed. $others"
        return 1
    fi
}

# parse_target_networks <list> <pod range> <service range>: check a
# comma-separated list against the rule above. True with the list in
# TARGET_NETWORKS, in lower case, without spaces and empty entries; "none"
# gives the empty list. Otherwise false, with the problem of the first
# wrong entry in TARGET_NETWORKS_PROBLEM. It sets the two variables, so it
# runs in the caller's shell, not in $(...).
parse_target_networks() {
    local list=${1,,} entries=() kept=() entry
    TARGET_NETWORKS=""
    TARGET_NETWORKS_PROBLEM=""
    [[ $list != none ]] || return 0
    IFS=, read -r -a entries <<<"$list"
    for entry in "${entries[@]}"; do
        entry=${entry#"${entry%%[![:space:]]*}"}
        entry=${entry%"${entry##*[![:space:]]}"}
        [[ -n $entry ]] || continue
        if [[ $entry == none ]]; then
            target_problem "none stands alone: it empties the list."
            return 1
        fi
        check_target_entry "$entry" "$2" "$3" || return 1
        kept+=("$entry")
    done
    if ((${#kept[@]} == 0)); then
        target_problem "the list is empty. Give at least one network, address or host name, or none."
        return 1
    fi
    TARGET_NETWORKS=$(
        IFS=,
        printf '%s' "${kept[*]}"
    )
}
