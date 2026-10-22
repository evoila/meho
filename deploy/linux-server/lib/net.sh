# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# IPv4 helpers for install.sh: the format of an address or a range, and
# whether two ranges share an address. This file is sourced, not run.

readonly IPV4_OCTET_RE='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
readonly IPV4_RE="^(${IPV4_OCTET_RE}\\.){3}${IPV4_OCTET_RE}\$"
readonly PREFIX_RE='^([0-9]|[12][0-9]|3[0-2])$'

# is_ipv4 <text>: true when the text is an IPv4 address such as 10.0.0.5.
is_ipv4() {
    [[ $1 =~ $IPV4_RE ]]
}

# is_ipv4_cidr <text>: true when the text is an IPv4 range such as 10.42.0.0/16.
is_ipv4_cidr() {
    [[ $1 == */* ]] && is_ipv4 "${1%/*}" && [[ ${1#*/} =~ $PREFIX_RE ]]
}

ipv4_to_int() {
    local a b c d
    IFS=. read -r a b c d <<<"$1"
    printf '%d' "$(((10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d))"
}

# cidrs_overlap <range> <range>: true when the two ranges share at least one
# address. An address without a prefix counts as a range of one address.
cidrs_overlap() {
    local left=$1 right=$2
    [[ $left == */* ]] || left+=/32
    [[ $right == */* ]] || right+=/32
    local left_prefix=${left#*/} right_prefix=${right#*/}
    local prefix=$((left_prefix < right_prefix ? left_prefix : right_prefix))
    local mask=$((prefix == 0 ? 0 : (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF))
    local left_int right_int
    left_int=$(ipv4_to_int "${left%/*}")
    right_int=$(ipv4_to_int "${right%/*}")
    (((left_int & mask) == (right_int & mask)))
}
