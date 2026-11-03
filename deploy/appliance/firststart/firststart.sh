#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# firststart.sh: the appliance's first start (form-1 design, section 2).
# meho-firststart.service runs it once, at the first boot after the import.
# It reads the fields of the import wizard and makes this appliance its own:
#
#   1. new SSH host keys (the build removed the build VM's)
#   2. the root file system grows to the disk, in case the admin made the
#      disk bigger at the import
#   3. the wizard fields: all are checked before anything changes. Without
#      them (an import straight into an ESXi host), the same fields are asked
#      on the VM console, and a wrong answer is asked again. Then the host
#      name, the network, the time servers, the admin account (the password
#      must be changed at the first login), its SSH key, and the SSH rule
#      (meho-host admin-network)
#   4. wait until the clock is synchronised
#   5. the core: install.sh --from-disk --hostname <host name>. It starts
#      RKE2 with this appliance's own keys, installs the core and writes the
#      two one-time files
#   6. this appliance's own bootloader password, in the third one-time file
#   7. this appliance's own AIDE database (CIS 6.3.1), which the daily
#      check of AIDE compares the files with
#
# Each step writes a done-marker under /var/lib/meho/firststart/ and is not
# run again; /var/lib/meho/firststart.done follows the last one, and the
# unit does not start again. A failed step writes its [FAIL] line to
# /var/lib/meho/firststart/failed, which the console banner shows
# (banner.sh). The next boot starts again from that step. Every step is safe
# to run twice, in case the power fails in the middle of one.
#
# The wizard values stay in this process's memory. None of them is written
# to a log. The console answers come on standard input, which the unit
# connects to the VM console (tty1).
#
# Exit codes:
#   0  first start is done
#   1  a step failed; the [FAIL] line says what to fix
#
# The tests in ../tests/ run this script against a fake machine. See
# docs/codebase/appliance-firststart.md.

set -Eeuo pipefail

# Tests set MEHO_TEST_ROOT to a temporary folder. Every path on the machine
# is then read and written below that folder. On a real machine it is not set.
HOST_ROOT="${MEHO_TEST_ROOT:-}"

readonly DOWNLOAD_DIR=/opt/meho/appliance/download
readonly STATE_DIR=/var/lib/meho/firststart
readonly DONE_FILE=/var/lib/meho/firststart.done
readonly FAILED_FILE=$STATE_DIR/failed
readonly BOOTLOADER_PASSWORD_FILE=/var/lib/meho/bootloader-password
readonly NETPLAN_FILE=/etc/netplan/50-meho.yaml
readonly TIMESYNCD_FILE=/etc/systemd/timesyncd.conf.d/50-meho.conf
readonly GRUB_PASSWORD_SCRIPT=/etc/grub.d/01_meho-password
readonly GRUB_LINUX_SCRIPT=/etc/grub.d/10_linux
readonly GRUB_MENU_FILE=/etc/default/grub.d/90-meho-boot-menu.cfg
readonly CONSOLE=/dev/tty1
readonly ADMIN_USER=admin
readonly MIN_PASSWORD_LENGTH=14
readonly CLOCK_WAIT_SECONDS=600
readonly OVF_ENV_NAMES=(ovf-env.xml ovf_env.xml OVF-ENV.XML OVF_ENV.XML)
readonly STEPS=(ssh-host-keys disk wizard-fields clock core bootloader-password aide-database)
readonly VAPP_FIX="power the VM off, correct the field in the VM's vApp options in vCenter (Configure, vApp Options), and power it on."

# The host name and address checks of the form-2 install, from the copy of
# the download that the build left on the appliance.
# shellcheck source=/dev/null
source "${HOST_ROOT}${DOWNLOAD_DIR}/lib/net.sh"
# shellcheck source=/dev/null
source "${HOST_ROOT}${DOWNLOAD_DIR}/lib/checks.sh"

CURRENT_STEP=""
ANSWER=""
declare -A FIELD=()

ok() { printf '[ok]   %s\n' "$*"; }

note() { printf '[note] %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

# fail <problem> <fix>: print one [FAIL] line, keep it for the console
# banner, and stop. The text must never hold a wizard value that is a secret.
fail() {
    local line="[FAIL] Step $CURRENT_STEP: $1 Fix: $2"
    printf '%s\n' "$line" >&2
    mkdir -p "${HOST_ROOT}${STATE_DIR}"
    printf '%s\n' "$line" >"${HOST_ROOT}${FAILED_FILE}"
    exit 1
}

on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    fail "firststart.sh stopped at line $2: a command failed with exit code $1." \
        "read the lines above on the console or with: journalctl -u meho-firststart. Correct the cause and $(how_to_restart); first start goes on from this step."
}

# how_to_restart: before step 3 makes the admin account, nobody can log in
# to run sudo reboot.
how_to_restart() {
    if getent passwd "$ADMIN_USER" >/dev/null; then
        printf 'restart the appliance (sudo reboot)'
    else
        printf 'power the VM off and on in vCenter'
    fi
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

# split_list <text>: the comma-separated items of the text, one per line,
# without spaces.
split_list() {
    local item
    local -a items
    IFS=, read -ra items <<<"${1// /}"
    for item in "${items[@]}"; do
        [[ -z $item ]] || printf '%s\n' "$item"
    done
}

# --- Step 1 ----------------------------------------------------------------

make_ssh_host_keys() {
    # Makes only the missing keys, so a second run keeps the first run's keys.
    ssh-keygen -A
    ok "this appliance has its own SSH host keys"
}

# --- Step 2 ----------------------------------------------------------------

grow_root_file_system() {
    local source name disk partition rc=0
    source=$(findmnt -n -o SOURCE /)
    name=${source##*/}
    disk=$(lsblk -n -o PKNAME "$source")
    partition=$(cat "${HOST_ROOT}/sys/class/block/$name/partition")
    growpart "/dev/$disk" "$partition" || rc=$?
    # growpart exits 1 with NOCHANGE when the partition already fills the disk.
    ((rc == 0 || rc == 1)) ||
        fail "growpart could not grow $source (exit code $rc)." \
            "read growpart's message above, then $(how_to_restart)."
    resize2fs "$source"
    ok "the root file system fills the disk"
}

# --- Step 3 ----------------------------------------------------------------

# read_ovf_env: print the OVF environment, the XML document in which vCenter
# hands over the wizard fields. VMware Tools gives it (the com.vmware.guestInfo
# transport); without them it is a file on a CD-ROM (the iso transport).
read_ovf_env() {
    local env device mount_dir name
    if env=$(vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>/dev/null) && [[ -n $env ]]; then
        printf '%s\n' "$env"
        return 0
    fi
    for device in "${HOST_ROOT}"/dev/sr[0-9]*; do
        [[ -e $device ]] || continue
        mount_dir=$(mktemp -d)
        if mount -o ro -t iso9660 "${device#"$HOST_ROOT"}" "$mount_dir" 2>/dev/null; then
            for name in "${OVF_ENV_NAMES[@]}"; do
                if [[ -f $mount_dir/$name ]]; then
                    cat "$mount_dir/$name"
                    umount "$mount_dir"
                    rmdir "$mount_dir"
                    return 0
                fi
            done
            umount "$mount_dir"
        fi
        rmdir "$mount_dir"
    done
    return 1
}

# parse_ovf_env: read the OVF environment on stdin and print each property
# as its key and its value, each followed by a NUL byte. Python's XML parser
# reads the values, so escaped characters such as &amp; come out right.
parse_ovf_env() {
    python3 -I -c '
import sys
import xml.etree.ElementTree as ET

oe = "{http://schemas.dmtf.org/ovf/environment/1}"
root = ET.fromstring(sys.stdin.read())
for prop in root.iter(oe + "Property"):
    sys.stdout.write(prop.get(oe + "key", "") + "\0" + prop.get(oe + "value", "") + "\0")
'
}

# read_wizard_fields <OVF environment>: keep each property in FIELD.
read_wizard_fields() {
    local key value i
    local -a pairs
    mapfile -d '' -t pairs < <(printf '%s\n' "$1" | parse_ovf_env)
    wait $! ||
        fail "The wizard fields cannot be read: the OVF environment is not valid XML." \
            "import the OVA again through vCenter."
    for ((i = 0; i + 1 < ${#pairs[@]}; i += 2)); do
        key=${pairs[i]}
        value=${pairs[i + 1]}
        FIELD[$key]=$value
    done
    ok "read the wizard fields"
}

field() { printf '%s' "${FIELD[$1]-}"; }

# normalize_field <key>: the forms the wizard implies: a host name in lower
# case, and static when the network field is empty.
normalize_field() {
    local value
    value=$(field "$1")
    case $1 in
        hostname) FIELD[hostname]=${value,,} ;;
        net.mode) FIELD[net.mode]=${value:-static} ;;
    esac
}

# field_problem <key>: print why the field's value is wrong, or nothing when
# it is right. The text never holds the value of a password.
field_problem() {
    local value item
    value=$(field "$1")
    case $1 in
        hostname)
            is_fqdn "$value" ||
                printf '%s' "The host name '$value' is not a full DNS name such as meho.example.com."
            ;;
        net.mode)
            [[ $value == static || $value == dhcp ]] ||
                printf '%s' "The network field is '$value'; it must be static or dhcp."
            ;;
        net.ip)
            is_ipv4_cidr "$value" ||
                printf '%s' "The IP address '$value' is not an IPv4 address with a prefix such as 10.0.0.10/24."
            ;;
        net.gateway)
            is_ipv4 "$value" ||
                printf '%s' "The default gateway '$value' is not an IPv4 address."
            ;;
        net.dns)
            if [[ -z $(split_list "$value") ]]; then
                printf '%s' "No DNS server is given. A static address needs at least one."
                return 0
            fi
            while read -r item; do
                if ! is_ipv4 "$item"; then
                    printf '%s' "The DNS server '$item' is not an IPv4 address."
                    return 0
                fi
            done < <(split_list "$value")
            ;;
        net.ntp)
            if [[ -z $(split_list "$value") ]]; then
                printf '%s' "No time server is given."
                return 0
            fi
            while read -r item; do
                if [[ ! $item =~ ^[A-Za-z0-9.-]+$ ]]; then
                    printf '%s' "The time server '$item' is not a host name or an IPv4 address."
                    return 0
                fi
            done < <(split_list "$value")
            ;;
        net.admin_network)
            is_ipv4 "$value" || is_ipv4_network "$value" ||
                printf '%s' "The admin network '$value' is not an IPv4 range that starts at its first address, such as 10.0.50.0/24, or one address."
            ;;
        admin.password)
            if ((${#value} < MIN_PASSWORD_LENGTH)); then
                printf '%s' "The appliance admin password is shorter than $MIN_PASSWORD_LENGTH characters."
            elif [[ $value == *$'\n'* ]]; then
                printf '%s' "The appliance admin password holds a line break."
            fi
            ;;
        admin.ssh_key)
            if [[ -z $value || $value == *$'\n'* ]] || ! printf '%s\n' "$value" | ssh-keygen -l -f - >/dev/null 2>&1; then
                printf '%s' "The admin SSH public key is missing or is not one public key in OpenSSH form (for example ssh-ed25519 AAAA... name)."
            fi
            ;;
    esac
}

# check_wizard_fields: every check runs before anything changes, so a wrong
# field leaves the machine as it was.
check_wizard_fields() {
    local key problem
    local -a keys=(hostname net.mode)
    normalize_field hostname
    normalize_field net.mode
    [[ $(field net.mode) != static ]] || keys+=(net.ip net.gateway net.dns)
    keys+=(net.ntp net.admin_network admin.password admin.ssh_key)
    for key in "${keys[@]}"; do
        problem=$(field_problem "$key")
        [[ -z $problem ]] || fail "$problem" "$VAPP_FIX"
    done

    # install.sh --from-disk does not copy a join token, so RKE2 could not join.
    if [[ -n $(field cluster.join.address) || -n $(field cluster.join.token) ]]; then
        fail "A join address or a join token is given. This version runs as a single appliance and cannot join a cluster." \
            "power the VM off, empty both join fields in the VM's vApp options in vCenter (Configure, vApp Options), and power it on."
    fi
    ok "the wizard fields are valid"
}

# read_answer [-s]: read one line from standard input, the VM console, into
# ANSWER. With -s the console does not show what is typed.
read_answer() {
    if ! IFS= read -r "$@" ANSWER; then
        printf '\n'
        fail "The console setup ended before every field was given." \
            "power the VM off and on; first start asks again."
    fi
    [[ ${1-} != -s ]] || printf '\n'
}

# ask_field <key> <question> [optional]: ask on the console until the answer
# passes the wizard's check of the field. With optional, an empty answer is
# taken as it is.
ask_field() {
    local key=$1 question=$2 problem
    while true; do
        printf '%s: ' "$question"
        read_answer
        FIELD[$key]=$ANSWER
        normalize_field "$key"
        if [[ -n ${3-} && -z $ANSWER ]]; then
            return 0
        fi
        problem=$(field_problem "$key")
        [[ -n $problem ]] || return 0
        printf '%s Try again.\n' "$problem"
    done
}

ask_password() {
    local first problem
    while true; do
        printf 'Appliance admin password, for the console and sudo (at least %d characters): ' "$MIN_PASSWORD_LENGTH"
        read_answer -s
        first=$ANSWER
        printf 'The same password again: '
        read_answer -s
        if [[ $ANSWER != "$first" ]]; then
            printf 'The two passwords differ. Try again.\n'
            continue
        fi
        FIELD[admin.password]=$ANSWER
        problem=$(field_problem admin.password)
        [[ -n $problem ]] || return 0
        printf '%s Try again.\n' "$problem"
    done
}

# ask_console_fields: the wizard fields, asked on the VM console, in the
# wizard's order. The join fields stay empty: this version runs alone.
ask_console_fields() {
    printf '\nThis appliance has no wizard fields: it was imported without the vApp options of vCenter.\n'
    printf 'Answer the questions below. A wrong answer is asked again.\n\n'
    ask_field hostname 'Host name (the full DNS name, for example meho.example.com)'
    ask_field net.ntp 'Time servers (comma-separated)'
    ask_field net.mode 'Network: static or dhcp (empty: static)'
    if [[ $(field net.mode) == static ]]; then
        ask_field net.ip 'IP address with prefix (for example 10.0.0.10/24)'
        ask_field net.gateway 'Default gateway'
        ask_field net.dns 'DNS servers (comma-separated)'
    fi
    ask_field net.admin_network 'Admin network: the IPv4 range that may use SSH (for example 10.0.50.0/24)'
    ask_password
    ask_field admin.ssh_key 'Admin SSH public key, in one line (empty: SSH stays closed)' optional
    ok "read the fields from the console"
}

write_network() {
    local mode dns
    mode=$(field net.mode)
    {
        printf 'network:\n  version: 2\n  renderer: networkd\n  ethernets:\n'
        printf '    meho:\n      match:\n        name: "en*"\n'
        if [[ $mode == dhcp ]]; then
            printf '      dhcp4: true\n'
        else
            dns=$(split_list "$(field net.dns)" | paste -sd, -)
            printf '      addresses: [%s]\n' "$(field net.ip)"
            printf '      routes:\n        - to: default\n          via: %s\n' "$(field net.gateway)"
            printf '      nameservers:\n        addresses: [%s]\n' "${dns//,/, }"
        fi
    } | write_root_file "$NETPLAN_FILE" 0600
    netplan apply
    ok "the network is set ($mode)"
}

write_time_servers() {
    printf '[Time]\nNTP=%s\n' "$(split_list "$(field net.ntp)" | paste -sd' ' -)" |
        write_root_file "$TIMESYNCD_FILE" 0644
    timedatectl set-ntp true
    systemctl restart systemd-timesyncd.service
    ok "the time servers are set"
}

set_up_admin() {
    local home="${HOST_ROOT}/home/$ADMIN_USER"
    if ! getent passwd "$ADMIN_USER" >/dev/null; then
        useradd --create-home --shell /bin/bash --groups sudo "$ADMIN_USER"
    fi
    # printf is a shell builtin, so the password is in no process's arguments.
    printf '%s:%s\n' "$ADMIN_USER" "$(field admin.password)" | chpasswd
    passwd --expire "$ADMIN_USER" >/dev/null
    mkdir -p "$home/.ssh"
    chmod 0700 "$home/.ssh"
    if [[ -n $(field admin.ssh_key) ]]; then
        printf '%s\n' "$(field admin.ssh_key)" | write_root_file "/home/$ADMIN_USER/.ssh/authorized_keys" 0600
    else
        rm -f "$home/.ssh/authorized_keys"
    fi
    chown -R "$ADMIN_USER:$ADMIN_USER" "$home/.ssh"
    ok "the account $ADMIN_USER has its password (to be changed at the first login)"
    if [[ -n $(field admin.ssh_key) ]]; then
        ok "the account $ADMIN_USER has the SSH key"
    else
        note "no SSH key was given, so SSH stays closed. To open it, log in as $ADMIN_USER on the console and put your public key into ~/.ssh/authorized_keys (mode 0600)."
    fi
}

apply_wizard_fields() {
    local env
    if env=$(read_ovf_env); then
        read_wizard_fields "$env"
        check_wizard_fields
    else
        ask_console_fields
    fi
    hostnamectl hostname "$(field hostname)"
    ok "the host name is $(field hostname)"
    write_network
    write_time_servers
    set_up_admin
    meho-host admin-network "$(field net.admin_network)"
    ok "SSH answers only from $(field net.admin_network)"
}

# --- Step 4 ----------------------------------------------------------------

wait_for_clock() {
    local waited=0
    until [[ $(timedatectl show --property=NTPSynchronized --value 2>/dev/null || true) == yes ]]; do
        ((waited < CLOCK_WAIT_SECONDS)) ||
            fail "The clock did not synchronise with the time servers in $TIMESYNCD_FILE within $((CLOCK_WAIT_SECONDS / 60)) minutes." \
                "check that the time servers answer from this network (UDP port 123). Log in as $ADMIN_USER on the console, correct the file if needed, and restart the appliance (sudo reboot)."
        sleep 5
        ((waited += 5))
    done
    ok "the clock is synchronised"
}

# --- Step 5 ----------------------------------------------------------------

install_core() {
    local hostname
    # The host name as the machine has it now: step 3 set it, and a fix the
    # admin made on the console after a failed step stays.
    hostname=$(<"${HOST_ROOT}/etc/hostname")
    "${HOST_ROOT}${DOWNLOAD_DIR}/install.sh" --from-disk --hostname "$hostname" ||
        fail "install.sh stopped; its [FAIL] line above says why." \
            "correct the cause and restart the appliance (sudo reboot); first start runs install.sh again."
}

# --- Step 6 ----------------------------------------------------------------

set_bootloader_password() {
    local password hash
    # Letters and digits only, without y and z: the boot menu has a US
    # keyboard, and those two swap places on a German one.
    password=$(head -c 32 /dev/urandom | base64 | tr -d '+/=yYzZ\n')
    password=${password:0:24}
    printf '%s\n' "$password" | write_root_file "$BOOTLOADER_PASSWORD_FILE" 0600
    hash=$(printf '%s\n%s\n' "$password" "$password" | grub-mkpasswd-pbkdf2 | awk '/grub\.pbkdf2\./ { print $NF }')
    [[ $hash == grub.pbkdf2.sha512.* ]] ||
        fail "grub-mkpasswd-pbkdf2 gave no password hash." "restart the appliance (sudo reboot) to try again."
    # Only the user root may edit a boot entry or open the boot menu's
    # command line. The script is root-only, because it holds the hash.
    write_root_file "$GRUB_PASSWORD_SCRIPT" 0700 <<SCRIPT
#!/bin/sh
cat <<'GRUB'
set superusers="root"
export superusers
password_pbkdf2 root $hash
GRUB
SCRIPT
    # Ubuntu's boot entries get the class --unrestricted, so the appliance
    # still boots without the password. 10_linux has no setting for it.
    sed -i 's/^CLASS="--class gnu-linux --class gnu --class os"$/CLASS="--class gnu-linux --class gnu --class os --unrestricted"/' \
        "${HOST_ROOT}${GRUB_LINUX_SCRIPT}"
    grep -q '^CLASS=".*--unrestricted"$' "${HOST_ROOT}${GRUB_LINUX_SCRIPT}" ||
        fail "$GRUB_LINUX_SCRIPT has no CLASS line in the form of Ubuntu 24.04, so the boot entries cannot be opened without the password." \
            "report this to evoila support with the file attached. The appliance runs; only the boot-menu password is not set."
    # The cloud image hides the boot menu and waits 0 seconds, so only Shift
    # held at the right moment would open it. The way back in needs the menu.
    printf 'GRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT=5\nGRUB_RECORDFAIL_TIMEOUT=5\n' |
        write_root_file "$GRUB_MENU_FILE" 0644
    update-grub
    ok "the boot menu shows for 5 seconds at every boot and has this appliance's own password, in $BOOTLOADER_PASSWORD_FILE (root only)"
}

# --- Step 7 ----------------------------------------------------------------

# The build hardened the appliance (cis/harden.sh) but left the database
# out: it must hold this appliance's files, after the core is installed.
build_aide_database() {
    aideinit --yes --force
    ok "AIDE has the database of this appliance's files; its daily check compares them with it"
}

# --- Steps -----------------------------------------------------------------

run_step() {
    local number=$1 name=${STEPS[$1 - 1]} marker
    marker="${HOST_ROOT}${STATE_DIR}/$number-$name.done"
    CURRENT_STEP="$number ($name)"
    if [[ -e $marker ]]; then
        note "step $number ($name) is already done"
        return 0
    fi
    step "Step $number: $name"
    "${@:2}"
    touch "$marker"
}

main() {
    # Standard input is the VM console, and bash opens it at its start, so
    # the console becomes this script's terminal: Ctrl+C, Ctrl+\ or Ctrl+Z
    # typed there would stop first start in the middle of a step.
    trap '' INT QUIT TSTP
    # The progress shows on the VM console in vCenter as well as in the
    # journal. The unit's journal+console would go to the serial port, the
    # last console= of the cloud image's kernel line.
    if [[ -w ${HOST_ROOT}${CONSOLE} ]]; then
        exec > >(tee -a "${HOST_ROOT}${CONSOLE}") 2>&1
    fi
    trap 'on_error "$?" "$LINENO"' ERR
    mkdir -p "${HOST_ROOT}${STATE_DIR}"
    rm -f "${HOST_ROOT}${FAILED_FILE}"
    printf 'MEHO appliance: first start\n'

    run_step 1 make_ssh_host_keys
    run_step 2 grow_root_file_system
    run_step 3 apply_wizard_fields
    run_step 4 wait_for_clock
    run_step 5 install_core
    run_step 6 set_bootloader_password
    run_step 7 build_aide_database
    touch "${HOST_ROOT}${DONE_FILE}"
    printf '\nfirststart.sh: first start is done. Log in as %s; the banner above the login lists the next steps.\n' "$ADMIN_USER"
}

main "$@"
