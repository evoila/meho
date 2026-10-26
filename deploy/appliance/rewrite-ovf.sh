#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# rewrite-ovf.sh: step 5 of the appliance build (form-1 design, section 1).
# In an exported .ovf, replace every product section with ours
# (product-section.xml, with @VERSION@ filled in), and declare the OVF
# transports com.vmware.guestInfo and iso on the virtual hardware section,
# so the appliance can read the wizard values through VMware Tools or from
# the ovf-env.xml CD-ROM. The file is changed in place.
#
# Usage:
#   rewrite-ovf.sh <file.ovf> <product-section.xml> <version>
#
# Exit codes:
#   0  the file is rewritten
#   1  the file has no virtual hardware section, or more than one
#   2  wrong usage

set -Eeuo pipefail

readonly TRANSPORTS="com.vmware.guestInfo iso"

usage_error() {
    printf 'rewrite-ovf.sh: %s\nUsage: rewrite-ovf.sh <file.ovf> <product-section.xml> <version>\n' "$1" >&2
    exit 2
}

main() {
    (($# == 3)) || usage_error "it needs three arguments."
    local ovf=$1 section=$2 version=$3 rendered temporary count
    [[ -f $ovf ]] || usage_error "$ovf is not a file."
    [[ -f $section ]] || usage_error "$section is not a file."
    count=$(grep -c '<VirtualHardwareSection' "$ovf" || true)
    if ((count != 1)); then
        printf '[FAIL] %s has %s virtual hardware sections, not one. Fix: export the VM again; one VM gives one section.\n' \
            "$ovf" "$count" >&2
        exit 1
    fi
    rendered=$(mktemp)
    temporary=$(mktemp)
    trap 'rm -f "$rendered" "$temporary"' EXIT
    # Only the section itself goes into the .ovf, not the comment above it.
    sed -n '/<ProductSection/,/<\/ProductSection>/p' "$section" | sed "s/@VERSION@/$version/g" >"$rendered"
    awk -v section_file="$rendered" -v transports="$TRANSPORTS" '
        /<ProductSection/ { skipping = 1 }
        skipping { if (/<\/ProductSection>/) skipping = 0; next }
        /<VirtualHardwareSection/ {
            while ((getline line < section_file) > 0) print line
            sub(/ ovf:transport="[^"]*"/, "")
            sub(/<VirtualHardwareSection/, "<VirtualHardwareSection ovf:transport=\"" transports "\"")
        }
        { print }
    ' "$ovf" >"$temporary"
    mv -f "$temporary" "$ovf"
    trap - EXIT
    rm -f "$rendered"
}

main "$@"
