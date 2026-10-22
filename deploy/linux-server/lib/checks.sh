# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Step 1 of install.sh: check the machine (form-2 design, section 5, step 1).
# Each check prints one [ok] or [WARN] line, or stops the script with one
# [FAIL] line that says what to fix. Nothing on the machine changes in this
# step. This file is sourced, not run.

readonly MIN_CPUS=4
# /proc/meminfo shows less than the memory of the machine, because the
# firmware and the kernel keep a part of it. A machine with 8 GB shows about
# 7.5 GiB, so the lowest value that passes is 7 GiB.
readonly MIN_MEMORY_KIB=$((7 * 1024 * 1024))
readonly MIN_MEMORY_TEXT="8 GB"
readonly MIN_DISK_FREE_KIB=$((80 * 1000 * 1000 * 1000 / 1024))
readonly MIN_DISK_FREE_TEXT="80 GB"
readonly OTHER_RUNTIMES=(k3s rke2 kubelet containerd dockerd crio)
# RKE2's inbound ports, and 80 and 443, which RKE2's Traefik takes on the host.
readonly REQUIRED_TCP_PORTS=(80 443 6443 9345 10250 2379 2380 2381 9099)
readonly REQUIRED_UDP_PORTS=(8472)
readonly IPV4_IN_TEXT_RE='([0-9]{1,3}\.){3}[0-9]{1,3}'
readonly HOSTNAME_FIX="pass the full DNS name with --hostname, for example --hostname meho.example.com."

check_root() {
    [[ $(id -u) == 0 ]] ||
        fail "install.sh does not run as root." "run it with sudo: sudo ./install.sh"
    ok "running as root"
}

# os_release_value <key>: one value of /etc/os-release, without quotes.
os_release_value() {
    local file="${HOST_ROOT}/etc/os-release"
    [[ -r $file ]] || return 0
    awk -F= -v key="$1" '$1 == key {
        value = substr($0, length(key) + 2)
        gsub(/^["\047]|["\047]$/, "", value)
        print value
        exit
    }' "$file"
}

check_os() {
    local arch id version name
    arch=$(uname -m)
    [[ $arch == x86_64 ]] ||
        fail "This machine is $arch. MEHO runs only on x86-64." "use an x86-64 machine."
    [[ -d ${HOST_ROOT}/run/systemd/system ]] ||
        fail "This machine does not run systemd." \
            "use a Linux that starts with systemd, for example Ubuntu 24.04 LTS."
    id=$(os_release_value ID)
    version=$(os_release_value VERSION_ID)
    name=$(os_release_value PRETTY_NAME)
    if [[ $id == ubuntu && $version == 24.04 ]]; then
        ok "${name:-Ubuntu 24.04 LTS} on x86-64 with systemd"
    else
        warn "${name:-This Linux} is not tested with MEHO. Ubuntu 24.04 LTS is tested. The install goes on."
    fi
}

check_download() {
    local download_dir=$1 file missing=()
    for file in "${DOWNLOAD_FILES[@]}"; do
        [[ -f $download_dir/$file ]] || missing+=("$file")
    done
    if ((${#missing[@]} > 0)); then
        fail "The download is not complete. Missing: $(join_by ', ' "${missing[@]}")." \
            "unpack the whole download again, and run install.sh from its folder."
    fi
    ok "the download is complete"
}

check_cpus() {
    local cpus
    cpus=$(getconf _NPROCESSORS_ONLN)
    if ! [[ $cpus =~ ^[0-9]+$ ]] || ((cpus < MIN_CPUS)); then
        fail "This machine has $cpus processors. MEHO needs at least $MIN_CPUS." \
            "give the machine at least $MIN_CPUS processors."
    fi
    ok "$cpus processors (at least $MIN_CPUS)"
}

kib_to_gib() { awk -v kib="$1" 'BEGIN { printf "%.1f", kib / 1048576 }'; }

kib_to_gb() { awk -v kib="$1" 'BEGIN { printf "%.0f", kib * 1024 / 1e9 }'; }

check_memory() {
    local kib
    kib=$(awk '$1 == "MemTotal:" { print $2; exit }' "${HOST_ROOT}/proc/meminfo")
    if ! [[ $kib =~ ^[0-9]+$ ]] || ((kib < MIN_MEMORY_KIB)); then
        fail "This machine has $(kib_to_gib "$kib") GiB of memory. MEHO needs a machine with at least $MIN_MEMORY_TEXT." \
            "give the machine at least $MIN_MEMORY_TEXT of memory."
    fi
    ok "$(kib_to_gib "$kib") GiB of memory (the machine needs at least $MIN_MEMORY_TEXT)"
}

check_disk() {
    local kib
    kib=$(df -P -k "${HOST_ROOT}/var/lib" | awk 'NR == 2 { print $4 }')
    if ! [[ $kib =~ ^[0-9]+$ ]] || ((kib < MIN_DISK_FREE_KIB)); then
        fail "/var/lib has $(kib_to_gb "$kib") GB free. MEHO needs at least $MIN_DISK_FREE_TEXT free there." \
            "make the file system of /var/lib larger, or free space in it."
    fi
    ok "$(kib_to_gb "$kib") GB free for /var/lib (at least $MIN_DISK_FREE_TEXT)"
}

check_no_other_runtime() {
    local name found=() names
    for name in "${OTHER_RUNTIMES[@]}"; do
        if command -v "$name" >/dev/null 2>&1 || pgrep -x "$name" >/dev/null 2>&1; then
            found+=("$name")
        fi
    done
    if ((${#found[@]} > 0)); then
        names=$(join_by ', ' "${found[@]}")
        fail "Another Kubernetes or container runtime is on this machine: $names." \
            "use a clean machine, or remove $names first."
    fi
    ok "no other Kubernetes or container runtime"
}

# listening_ports <tcp|udp>: the local ports that sockets of this kind listen on.
listening_ports() {
    local kind=-t
    if [[ $1 == udp ]]; then
        kind=-u
    fi
    ss -H -l -n "$kind" | awk '{ count = split($4, part, ":"); print part[count] }' | sort -u
}

check_ports() {
    local tcp udp port busy=()
    tcp=$(listening_ports tcp)
    udp=$(listening_ports udp)
    for port in "${REQUIRED_TCP_PORTS[@]}"; do
        if grep -qxF -- "$port" <<<"$tcp"; then
            busy+=("$port/tcp")
        fi
    done
    for port in "${REQUIRED_UDP_PORTS[@]}"; do
        if grep -qxF -- "$port" <<<"$udp"; then
            busy+=("$port/udp")
        fi
    done
    if ((${#busy[@]} > 0)); then
        fail "Ports that RKE2 needs are in use: $(join_by ', ' "${busy[@]}")." \
            "stop the programs that use them. 'ss -tulpn' shows which programs they are."
    fi
    ok "the ports RKE2 needs are free: TCP $(join_by ', ' "${REQUIRED_TCP_PORTS[@]}"); UDP $(join_by ', ' "${REQUIRED_UDP_PORTS[@]}")"
}

check_clock() {
    local synchronised
    synchronised=$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || true)
    [[ $synchronised == yes ]] ||
        fail "The clock of this machine is not synchronised." \
            "turn on time sync with chrony or systemd-timesyncd (for example: timedatectl set-ntp true), and check that the time server answers."
    ok "the clock is synchronised"
}

# is_fqdn <name>: true for a full DNS name with at least two labels, such as
# meho.example.com. The last label must hold a letter, so an address is not a name.
is_fqdn() {
    local name=$1
    local label='[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
    local fqdn_re="^(${label}\\.)+${label}\$"
    ((${#name} <= 253)) && [[ $name =~ $fqdn_re ]] && [[ ${name##*.} =~ [a-z] ]]
}

check_hostname() {
    local name=$1
    [[ -n $name ]] ||
        fail "The full name of this machine is not known." "$HOSTNAME_FIX"
    is_fqdn "$name" ||
        fail "The host name '$name' is not a fully qualified DNS name." "$HOSTNAME_FIX"
    ok "$name is a fully qualified name"
}

# dns_addresses <name>: the IPv4 addresses that DNS gives for the name, one
# per line. An entry in /etc/hosts must not count. systemd-resolved answers
# from /etc/hosts too, so resolvectl asks with --synthesize=no. Without
# systemd-resolved, getent asks only the "dns" source of the name service.
dns_addresses() {
    local name=$1
    if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
        resolvectl query --legend=no --synthesize=no --cache=no -4 "$name" 2>/dev/null |
            sed -e 's/^[^ ]*: //' -e 's/([^)]*)//g' |
            grep -oE "$IPV4_IN_TEXT_RE" | sort -u || true
    else
        getent -s dns ahostsv4 "$name" 2>/dev/null | awk '{ print $1 }' | sort -u || true
    fi
}

machine_addresses() {
    ip -4 -o addr show scope global | awk '{ split($4, part, "/"); print part[1] }' | sort -u
}

# check_dns <name> <public address or empty>: the name resolves through DNS,
# to an address of this machine or to the --public-address.
check_dns() {
    local name=$1 public_address=$2 addresses shown local_addresses address
    addresses=$(dns_addresses "$name")
    [[ -n $addresses ]] ||
        fail "$name does not resolve through DNS. An entry in /etc/hosts does not count." \
            "create a DNS A record for $name that points to this machine."
    shown=${addresses//$'\n'/, }
    ok "$name resolves through DNS to $shown"
    if [[ -n $public_address ]]; then
        grep -qxF -- "$public_address" <<<"$addresses" ||
            fail "$name resolves to $shown, not to the address given with --public-address ($public_address)." \
                "correct the DNS record, or pass the address it points to with --public-address."
        ok "$name points to the address given with --public-address"
        return 0
    fi
    local_addresses=$(machine_addresses)
    while IFS= read -r address; do
        if grep -qxF -- "$address" <<<"$local_addresses"; then
            ok "$address is an address of this machine"
            return 0
        fi
    done <<<"$addresses"
    fail "$name resolves to $shown, which is not an address of this machine." \
        "correct the DNS record. Behind address translation in your network, pass the translated address with --public-address."
}

# machine_networks: the IPv4 ranges of this machine's addresses and routes,
# one per line. The default route is left out, because it covers every address.
machine_networks() {
    ip -4 -o addr show | awk '{ print $4 }'
    ip -4 route show | awk '
        { destination = ($1 ~ /^(unicast|local|broadcast|multicast|throw|unreachable|prohibit|blackhole|nat)$/) ? $2 : $1 }
        destination != "default" { print destination }'
}

check_ranges() {
    local cluster_cidr=$1 service_cidr=$2 networks network
    if cidrs_overlap "$cluster_cidr" "$service_cidr"; then
        fail "The pod range $cluster_cidr and the service range $service_cidr overlap." \
            "choose two separate ranges with --cluster-cidr and --service-cidr."
    fi
    networks=$(machine_networks)
    while IFS= read -r network; do
        is_ipv4 "${network%/*}" || continue
        if cidrs_overlap "$cluster_cidr" "$network"; then
            fail "The pod range $cluster_cidr overlaps $network, an address or a route of this machine." \
                "choose a free range with --cluster-cidr."
        fi
        if cidrs_overlap "$service_cidr" "$network"; then
            fail "The service range $service_cidr overlaps $network, an address or a route of this machine." \
                "choose a free range with --service-cidr."
        fi
    done <<<"$networks"
    ok "the pod range $cluster_cidr and the service range $service_cidr are free on this machine"
}
