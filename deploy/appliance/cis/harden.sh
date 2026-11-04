#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# harden.sh: the OS hardening of the appliance build (form-1 design,
# section 1, step 3; ADR 0009 decision 2): the CIS Ubuntu Linux 24.04 LTS
# Benchmark v1.0.0, Level 1 - Server, through ComplianceAsCode and OpenSCAP,
# with the MEHO tailoring in tailoring.xml. build.sh copies this folder and
# the data stream to the build VM and runs it as root, after the form-2
# install and the appliance pieces, before the clean. In this order:
#
#   1. the packages the hardening and meho-host log-server need, and ufw goes
#   2. the scanner files under /opt/meho/appliance/cis, where meho-host check
#      and the update find them, and oscap-run-sce-script in /usr/local/bin,
#      so the script checks run from the noexec /tmp
#   3. the fixes of ComplianceAsCode for every rule of the profile
#      appliance_remediate
#   4. the settings of the design, after the fixes so they win: /dev/shm and
#      /tmp with nodev, nosuid and noexec; the AppArmor profiles Ubuntu ships
#      unconfined turned off; IPv6 off; SSH with keys only and no root;
#      the folders AIDE skips; nftables.service loads the MEHO firewall
#      too, without its flush
#   5. the MEHO firewall blocks by default: policy drop on the chains input,
#      forward and output. Last, because the fixes of step 3 install
#      packages from the internet
#   6. the scan (profile appliance). Its result list goes to the results
#      file, one line per rule: "<result> <rule>". A failing rule that is in
#      neither not-applicable.md nor exceptions.md gets a [WARN] line
#
# Usage:
#   harden.sh --content <ssg-ubuntu2404-ds.xml> --results <file>
#
# Exit codes:
#   0  hardened and scanned; failing rules are in the results file
#   1  a step failed; the [FAIL] line says what to fix
#   2  wrong usage
#
# The tests in ../tests/ run this script against a fake machine. See
# docs/codebase/appliance-cis.md.

set -Eeuo pipefail

# Tests set MEHO_TEST_ROOT to a temporary folder. Every path on the machine
# is then read and written below that folder. On a real machine it is not set.
HOST_ROOT="${MEHO_TEST_ROOT:-}"

CIS_SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly CIS_SOURCE_DIR
readonly DOWNLOAD_DIR=/opt/meho/appliance/download
readonly CIS_DIR=/opt/meho/appliance/cis
readonly DATA_STREAM=ssg-ubuntu2404-ds.xml
readonly CIS_FILES=(tailoring.xml not-applicable.md exceptions.md)
readonly SCE_RUNNER=/usr/local/bin/oscap-run-sce-script
readonly SCAN_PROFILE=xccdf_com.evoila.meho_profile_appliance
readonly REMEDIATE_PROFILE=xccdf_com.evoila.meho_profile_appliance_remediate
readonly RULE_PREFIX=xccdf_org.ssgproject.content_rule_
# rsyslog-gnutls: the TLS module of rsyslog for meho-host log-server --tls
# (#5752). The appliance has no way to the Ubuntu archive later.
readonly PACKAGES=(aide aide-common openscap-scanner libpam-pwquality cracklib-runtime apparmor-utils rsyslog-gnutls)
readonly FSTAB=/etc/fstab
readonly UBUNTU_TMP_MOUNT=/usr/share/systemd/tmp.mount
readonly TMP_MOUNT=/etc/systemd/system/tmp.mount
readonly APPARMOR_DIR=/etc/apparmor.d
readonly IPV6_SYSCTL=/etc/sysctl.d/60-meho-ipv6-off.conf
readonly AIDE_SKIP=/etc/aide/aide.conf.d/90_meho_appliance
# Folders that change all the time; RKE2 and MEHO look after them.
readonly AIDE_SKIPPED=(/var/lib/rancher /var/lib/kubelet /var/lib/meho/volumes /var/log /run)
# sshd keeps the first value it reads, and Ubuntu includes sshd_config.d/*.conf
# first, in name order: after the fixes' 00- and 01-complianceascode-*.conf
# (neither sets these three), before cloud-init's 50-cloud-init.conf.
readonly SSHD_DROPIN=/etc/ssh/sshd_config.d/05-meho-appliance.conf
readonly NFTABLES_CONF=/etc/nftables.conf
readonly NFTABLES_DROPIN=/etc/systemd/system/nftables.service.d/meho-no-flush.conf
readonly REMEDIATE_LOG=/run/meho-cis-remediate.log
readonly SCAN_OUTPUT=/run/meho-cis-scan.txt

# MEHO_FIREWALL_FILE, MEHO_FIREWALL_UNIT and firewall_state of the form-2
# install, from the copy of the download that the build left on the machine.
# shellcheck source=/dev/null
source "${HOST_ROOT}${DOWNLOAD_DIR}/lib/firewall.sh"

CONTENT=""
RESULTS=""

usage() {
    cat <<'HELP'
harden.sh: harden the appliance build VM to CIS Ubuntu 24.04 Level 1 - Server and scan it.

Usage:
  harden.sh --content <ssg-ubuntu2404-ds.xml> --results <file>

  --content <file>  The data stream of ComplianceAsCode for Ubuntu 24.04.
  --results <file>  Where the scan's result list goes, one line per rule.
  -h, --help        Show this help and stop.
HELP
}

ok() { printf '[ok]   %s\n' "$*"; }

note() { printf '[note] %s\n' "$*"; }

warn() { printf '[WARN] %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

fail() {
    printf '[FAIL] %s Fix: %s\n' "$1" "$2" >&2
    exit 1
}

usage_error() {
    printf 'harden.sh: %s\n' "$1" >&2
    printf "Run 'harden.sh --help' to see the options.\n" >&2
    exit 2
}

on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    fail "harden.sh stopped at line $2: a command failed with exit code $1." \
        "read the lines above, correct the cause and run the build again from step 3."
}

parse_options() {
    while (($# > 0)); do
        case $1 in
            -h | --help)
                usage
                exit 0
                ;;
            --content | --results)
                [[ -n ${2-} ]] || usage_error "$1 needs a file."
                if [[ $1 == --content ]]; then CONTENT=$2; else RESULTS=$2; fi
                shift
                ;;
            *) usage_error "unknown argument: $1" ;;
        esac
        shift
    done
    [[ -n $CONTENT && -n $RESULTS ]] || usage_error "it needs --content and --results."
    [[ -f $CONTENT ]] || usage_error "the data stream $CONTENT is not a file."
}

# write_root_file <path on the machine> <mode>: write stdin to the file in
# one step, so a reader never sees half a file.
write_root_file() {
    local target="${HOST_ROOT}$1" mode=$2 temporary
    mkdir -p "${target%/*}"
    temporary=$(mktemp "${target}.XXXXXX")
    cat >"$temporary"
    chmod "$mode" "$temporary"
    mv -f "$temporary" "$target"
}

# rule_names <markdown file>: the scanner rule names in the first column of
# the file's tables, one per line.
rule_names() {
    # The backticks are Markdown, not a command.
    # shellcheck disable=SC2016
    sed -n 's/^| `\([A-Za-z0-9_.-]*\)` |.*/\1/p' "$1"
}

# --- Step 1 ----------------------------------------------------------------

install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q ||
        fail "apt-get update failed, so the packages of the hardening cannot be installed." \
            "check that the build VM reaches the Ubuntu archive (archive.ubuntu.com and security.ubuntu.com over HTTP), then run the build again from step 3."
    apt-get install -y -q --no-install-recommends "${PACKAGES[@]}" ||
        fail "apt-get could not install ${PACKAGES[*]}." "read apt-get's message above, then run the build again from step 3."
    # One firewall tool (CIS 4.1.1): nftables. The form-2 install turned ufw off.
    apt-get purge -y -q ufw
    ok "installed ${PACKAGES[*]}; ufw is removed"
}

# --- Step 2 ----------------------------------------------------------------

install_scanner_files() {
    local file
    mkdir -p "${HOST_ROOT}${CIS_DIR}"
    install -m 0644 "$CONTENT" "${HOST_ROOT}${CIS_DIR}/$DATA_STREAM"
    for file in "${CIS_FILES[@]}"; do
        install -m 0644 "$CIS_SOURCE_DIR/$file" "${HOST_ROOT}${CIS_DIR}/$file"
    done
    mkdir -p "${HOST_ROOT}${SCE_RUNNER%/*}"
    install -m 0755 "$CIS_SOURCE_DIR/${SCE_RUNNER##*/}" "${HOST_ROOT}${SCE_RUNNER}"
    ok "the scanner files are under $CIS_DIR, and OpenSCAP's runner for script checks is $SCE_RUNNER"
}

# --- Step 3 ----------------------------------------------------------------

run_fixes() {
    local rc=0
    # The build user keeps sudo without a password, which build.sh's clean
    # step needs: the rule that takes it away (CIS 5.2.4) is Level 2 only.
    # oscap exits 2 when a rule still fails after its fix, 1 on an error.
    oscap xccdf eval --remediate --tailoring-file "${HOST_ROOT}${CIS_DIR}/tailoring.xml" \
        --profile "$REMEDIATE_PROFILE" "${HOST_ROOT}${CIS_DIR}/$DATA_STREAM" \
        >"${HOST_ROOT}${REMEDIATE_LOG}" 2>&1 || rc=$?
    ((rc == 0 || rc == 2)) ||
        fail "oscap stopped while it ran the fixes of ComplianceAsCode (exit code $rc)." \
            "read $REMEDIATE_LOG on the build VM, then run the build again from step 3."
    ok "ran the fixes of ComplianceAsCode for the profile ${REMEDIATE_PROFILE##*_profile_}"
}

# --- Step 4 ----------------------------------------------------------------

# fstab_without <mount point>: /etc/fstab without its lines for the mount
# point, such as a line a fix of step 3 wrote.
fstab_without() {
    awk -v where="$1" '/^[[:space:]]*#/ || $2 != where' "${HOST_ROOT}${FSTAB}"
}

harden_dev_shm() {
    {
        fstab_without /dev/shm
        printf 'tmpfs /dev/shm tmpfs defaults,nodev,nosuid,noexec 0 0\n'
    } | write_root_file "$FSTAB" 0644
    mount -o remount,nodev,nosuid,noexec /dev/shm
    ok "/dev/shm is mounted with nodev, nosuid and noexec (CIS 1.1.2.2)"
}

# /tmp becomes a RAM disk through systemd's tmp.mount, which Ubuntu ships
# only as an example, with noexec added.
harden_tmp() {
    local unit
    fstab_without /tmp | write_root_file "$FSTAB" 0644
    unit=$(sed 's/^Options=\(.*\)$/Options=\1,noexec/' "${HOST_ROOT}${UBUNTU_TMP_MOUNT}")
    if ! { grep -qE '^Options=(.*,)?nodev(,|$)' <<<"$unit" &&
        grep -qE '^Options=(.*,)?nosuid(,|$)' <<<"$unit" &&
        grep -qE '^Options=(.*,)?noexec(,|$)' <<<"$unit"; }; then
        fail "$UBUNTU_TMP_MOUNT has no Options line with nodev and nosuid in the form of Ubuntu 24.04." \
            "read the file, then adapt harden_tmp in harden.sh."
    fi
    printf '%s\n' "$unit" | write_root_file "$TMP_MOUNT" 0644
    systemctl daemon-reload
    systemctl enable --now tmp.mount
    ok "/tmp is a RAM disk with nodev, nosuid and noexec (CIS 1.1.2.1)"
}

# Ubuntu 24.04 ships profiles in "unconfined" mode, for programs that need
# user namespaces. CIS 1.3.1.3 wants every loaded profile in enforce or
# complain mode; none of those programs is on the appliance.
disable_unconfined_apparmor_profiles() {
    local profile name count=0
    mkdir -p "${HOST_ROOT}${APPARMOR_DIR}/disable"
    for profile in "${HOST_ROOT}${APPARMOR_DIR}"/*; do
        [[ -f $profile ]] || continue
        grep -qE 'flags=\([^)]*\bunconfined\b' "$profile" || continue
        name=${profile##*/}
        ln -sfn "$APPARMOR_DIR/$name" "${HOST_ROOT}${APPARMOR_DIR}/disable/$name"
        # A profile that is not loaded cannot be removed; that is fine.
        apparmor_parser -R "$APPARMOR_DIR/$name" >/dev/null 2>&1 || true
        count=$((count + 1))
    done
    ok "turned off $count AppArmor profiles that Ubuntu ships unconfined (CIS 1.3.1.3)"
}

# The appliance is IPv4 only in v1 (design decision of 2026-10-22). The
# sysctl keeps the IPv6 stack, so programs that open an IPv6 socket still
# work; the kernel parameter ipv6.disable=1 would not.
turn_ipv6_off() {
    printf '%s\n' '# MEHO appliance: IPv4 only (harden.sh).' \
        'net.ipv6.conf.all.disable_ipv6 = 1' 'net.ipv6.conf.default.disable_ipv6 = 1' |
        write_root_file "$IPV6_SYSCTL" 0644
    sysctl -q -p "$IPV6_SYSCTL"
    ok "IPv6 is off"
}

# ADR 0009 decision 4: SSH takes keys only, and never root. CIS Level 1
# alone does not turn password login off. The running sshd keeps its
# settings until the appliance boots, so the build's own login is not cut.
ssh_keys_only() {
    printf '%s\n' '# MEHO appliance (harden.sh): SSH takes keys only, and never root.' \
        'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin no' |
        write_root_file "$SSHD_DROPIN" 0600
    sshd -t ||
        fail "sshd does not accept its settings with $SSHD_DROPIN." "read sshd's message above."
    ok "SSH takes keys only and refuses root (from the next start of sshd)"
}

set_aide_skipped_folders() {
    {
        printf '# MEHO appliance (harden.sh): folders that change all the time.\n'
        printf '!%s\n' "${AIDE_SKIPPED[@]}"
    } | write_root_file "$AIDE_SKIP" 0644
    ok "AIDE skips ${AIDE_SKIPPED[*]}"
}

# CIS 4.3.9 and 4.3.10 look for nftables.service and an include line in
# /etc/nftables.conf. It loads the same file as meho-firewall.service, which
# stays: meho-host check and RKE2's start depend on it. Loading the file
# twice is safe, because it replaces its own table in one step.
enable_nftables_service() {
    write_root_file "$NFTABLES_CONF" 0644 <<EOF
#!/usr/sbin/nft -f
# MEHO appliance (harden.sh): nftables.service loads the MEHO host firewall
# from here, as ${MEHO_FIREWALL_UNIT} does. Never add "flush ruleset": it
# would delete the rules of Canal and kube-proxy.
include "${MEHO_FIREWALL_FILE}"
EOF
    # Ubuntu's unit stops with "nft flush ruleset"; the empty line removes it.
    printf '%s\n' '# MEHO appliance (harden.sh): stopping must not flush the rules of Canal,' \
        '# kube-proxy and the MEHO firewall.' '[Service]' 'ExecStop=' |
        write_root_file "$NFTABLES_DROPIN" 0644
    systemctl daemon-reload
    systemctl enable --now nftables.service
    ok "nftables.service loads $MEHO_FIREWALL_FILE too, and its stop no longer flushes the rules"
}

# --- Step 5 ----------------------------------------------------------------

# The form-2 file has the chains input, forward and output with policy
# accept; the appliance sets drop (form-2 design, section 2; ADR 0009
# decision 1). Each line must be found, so a changed file stops the build.
set_firewall_drop_policies() {
    local rules="${HOST_ROOT}${MEHO_FIREWALL_FILE}" new hook output state
    new=$(mktemp "${rules}.XXXXXX")
    sed -E 's/^([[:space:]]*type filter hook (input|forward|output) priority filter;) policy accept;$/\1 policy drop;/' \
        "$rules" >"$new"
    for hook in input forward output; do
        if ! grep -qE "^[[:space:]]*type filter hook $hook priority filter; policy drop;$" "$new"; then
            rm -f "$new"
            fail "$MEHO_FIREWALL_FILE has no chain for the hook $hook in the form the form-2 install writes, so its policy cannot be set to drop." \
                "compare the file with render_firewall_rules in deploy/linux-server/lib/firewall.sh, then adapt harden.sh."
        fi
    done
    if ! output=$(nft -c -f "$new" 2>&1); then
        rm -f "$new"
        printf '%s\n' "$output" >&2
        fail "nft does not accept the firewall file with policy drop, so it was not changed." "read nft's message above."
    fi
    chmod 0600 "$new"
    mv -f "$new" "$rules"
    nft -f "$rules"
    state=$(firewall_state) ||
        fail "The MEHO firewall is not on after the change (${state#off: })." "see its log: $FIREWALL_LOG_COMMAND"
    ok "the MEHO firewall blocks by default: policy drop on input, forward and output (CIS 4.3.5 to 4.3.8)"
}

# --- Step 6 ----------------------------------------------------------------

scan() {
    local rc=0 result rule counts expected unexpected=0
    oscap xccdf eval --tailoring-file "${HOST_ROOT}${CIS_DIR}/tailoring.xml" \
        --profile "$SCAN_PROFILE" "${HOST_ROOT}${CIS_DIR}/$DATA_STREAM" \
        >"${HOST_ROOT}${SCAN_OUTPUT}" 2>&1 || rc=$?
    ((rc == 0 || rc == 2)) ||
        fail "oscap could not scan the build VM (exit code $rc)." "read $SCAN_OUTPUT on the build VM."
    # oscap prints "Rule<CR><TAB><id>" and "Result<CR><TAB><result>" per rule.
    {
        printf '# CIS Ubuntu Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server, as tailored\n'
        printf '# for the MEHO appliance (deploy/appliance/cis/tailoring.xml), scanned by\n'
        printf '# OpenSCAP with ComplianceAsCode. One line per rule: <result> <rule>.\n'
        tr -d '\r' <"${HOST_ROOT}${SCAN_OUTPUT}" |
            awk -F'\t' -v prefix="$RULE_PREFIX" '
                $1 == "Rule" { rule = $2; sub("^" prefix, "", rule) }
                $1 == "Result" { print $2, rule }'
    } >"$RESULTS"
    chmod 0644 "$RESULTS"
    counts=$(awk '!/^#/ { n[$1]++ } END { for (r in n) printf "%s %d, ", r, n[r] }' "$RESULTS")
    ok "scanned: ${counts%, }"
    expected=$(rule_names "${HOST_ROOT}${CIS_DIR}/exceptions.md")
    while read -r result rule; do
        case $result in
            pass | notapplicable | notselected | informational) ;;
            *)
                if grep -qx -- "$rule" <<<"$expected"; then
                    note "$rule: $result, an exception in exceptions.md"
                else
                    warn "$rule: $result, and it is in neither not-applicable.md nor exceptions.md"
                    unexpected=$((unexpected + 1))
                fi
                ;;
        esac
    done < <(grep -v '^#' "$RESULTS")
    while read -r rule; do
        if [[ -n $rule ]] && grep -qx -- "pass $rule" "$RESULTS"; then
            note "$rule passes, so it can leave exceptions.md"
        fi
    done <<<"$expected"
    if ((unexpected > 0)); then
        warn "$unexpected rules fail and are in neither list. Add each to exceptions.md with its reason, or fix it."
    fi
}

main() {
    parse_options "$@"
    trap 'on_error "$?" "$LINENO"' ERR
    step "Hardening 1: the packages"
    install_packages
    step "Hardening 2: the scanner files"
    install_scanner_files
    step "Hardening 3: the fixes of ComplianceAsCode"
    run_fixes
    step "Hardening 4: the settings of the design"
    harden_dev_shm
    harden_tmp
    disable_unconfined_apparmor_profiles
    turn_ipv6_off
    ssh_keys_only
    set_aide_skipped_folders
    enable_nftables_service
    step "Hardening 5: the firewall blocks by default"
    set_firewall_drop_policies
    step "Hardening 6: the scan"
    scan
}

main "$@"
