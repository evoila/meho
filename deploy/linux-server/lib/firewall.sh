# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The MEHO host firewall: closed by default (ADR 0009; form-2 design,
# section 2). Step 1 of install.sh checks that the firewall can be set
# without locking the admin out. Step 2 writes it and turns it on, before
# RKE2 is installed. This file is sourced, not run.
#
# - One nftables file, /etc/meho/firewall.nft, holds every rule. It changes
#   only its own table, "inet meho", and never runs "flush ruleset": RKE2's
#   network layer (Canal) and kube-proxy keep their own rules in other
#   tables of the same kernel firewall.
# - Its own boot unit, meho-firewall.service, loads the file at every start,
#   before the network and before RKE2. The nftables.service of Ubuntu and
#   RHEL is not used: its stop command, and on RHEL its reload command, run
#   "flush ruleset", which would delete the rules of Canal and kube-proxy.
# - A drop-in for rke2-server.service starts RKE2 only while the MEHO table
#   is loaded, so RKE2's ports are never open to the network without it.
#
# Other tools read the constants below: the tests, and later meho-host and
# the release test of form 2. So shellcheck cannot see every use.
# shellcheck disable=SC2034

readonly MEHO_FIREWALL_FILE=/etc/meho/firewall.nft
readonly MEHO_FIREWALL_UNIT=meho-firewall.service
readonly MEHO_FIREWALL_UNIT_FILE=/etc/systemd/system/meho-firewall.service
readonly RKE2_FIREWALL_DROPIN=/etc/systemd/system/rke2-server.service.d/meho-firewall.conf
readonly FIREWALL_LOG_COMMAND="journalctl -u meho-firewall.service -e"

# The ports as a port scan from another machine must see them. The rules
# open the first two lists and drop everything else that comes in on a
# network card. The closed lists are RKE2's own inbound ports (RKE2
# requirements; ADR 0009), so that a test can prove them closed. The release
# test of form 2 (#4742) reads all of them with firewall_port_list.
readonly FIREWALL_OPEN_TCP_PORTS=(80 443)
readonly FIREWALL_SSH_PORT=22
readonly FIREWALL_CLOSED_TCP_PORTS=(6443 9345 10250 2379 2380 2381 9099 30000-32767)
readonly FIREWALL_CLOSED_UDP_PORTS=(8472)

# Interfaces of the machine's own Kubernetes: loopback, the pod interfaces
# of Calico (cali...) and the VXLAN interface of flannel (flannel.1). A
# packet on them comes from this machine or its pods, never straight from
# the network, so the rules leave it alone.
readonly FIREWALL_CLUSTER_INTERFACES=(lo 'cali*' 'flannel*')

# Set by check_nftables and check_admin_network in step 1, used in step 2.
NFT_BIN=""
SSH_ALLOWED=""
SSH_ALLOWED_NOTE=""

# firewall_port_list: print every port of the lists above, one per line, as
# "<port>/<protocol> <who may connect from another machine>":
# "open" (every address), "admin-network" (only the admin network) or
# "closed" (nobody). It reads nothing on the machine.
firewall_port_list() {
    local port
    for port in "${FIREWALL_OPEN_TCP_PORTS[@]}"; do
        printf '%s/tcp open\n' "$port"
    done
    printf '%s/tcp admin-network\n' "$FIREWALL_SSH_PORT"
    for port in "${FIREWALL_CLOSED_TCP_PORTS[@]}"; do
        printf '%s/tcp closed\n' "$port"
    done
    for port in "${FIREWALL_CLOSED_UDP_PORTS[@]}"; do
        printf '%s/udp closed\n' "$port"
    done
}

# firewall_state: say whether the MEHO host firewall is on. It changes
# nothing, so meho-host check (#4737, #4638) can call it. It prints "on", or
# "off: <reason>", and returns 0 for on and 1 for off.
firewall_state() {
    if ! nft list table inet meho >/dev/null 2>&1; then
        printf 'off: the table inet meho is not loaded\n'
        return 1
    fi
    if ! systemctl is-enabled --quiet "$MEHO_FIREWALL_UNIT" 2>/dev/null; then
        printf 'off: %s is not enabled, so the next start of the machine loads no firewall\n' \
            "$MEHO_FIREWALL_UNIT"
        return 1
    fi
    printf 'on\n'
}

check_nftables() {
    NFT_BIN=$(command -v nft) ||
        fail "nft, the nftables tool, is not on this machine. The MEHO host firewall needs it." \
            "install the nftables package from your distribution's mirror, then run install.sh again."
    nft list tables >/dev/null 2>&1 ||
        fail "nft cannot read the firewall tables of the kernel." \
            "use a kernel with nftables support, for example the standard kernel of Ubuntu 24.04."
    ok "nftables is ready for the MEHO host firewall ($NFT_BIN)"
}

# ssh_connection: print the SSH_CONNECTION value of the SSH session that
# started install.sh, "<client address> <client port> <server address>
# <server port>", or nothing when there is none (the machine's console).
# sudo removes SSH_CONNECTION from the environment (its env_reset default),
# so the function also reads the environment of the processes above this
# one, up to the shell of the SSH login.
ssh_connection() {
    local pid=$PPID depth entry
    if [[ -n ${SSH_CONNECTION:-} ]]; then
        printf '%s\n' "$SSH_CONNECTION"
        return 0
    fi
    for ((depth = 0; depth < 32 && pid > 1; depth++)); do
        if [[ -r ${HOST_ROOT}/proc/$pid/environ ]]; then
            while IFS= read -r -d '' entry; do
                if [[ $entry == SSH_CONNECTION=* ]]; then
                    printf '%s\n' "${entry#SSH_CONNECTION=}"
                    return 0
                fi
            done <"${HOST_ROOT}/proc/$pid/environ"
        fi
        pid=$(awk '$1 == "PPid:" { print $2; exit }' "${HOST_ROOT}/proc/$pid/status" 2>/dev/null) || return 0
        [[ $pid =~ ^[0-9]+$ ]] || return 0
    done
    return 0
}

# check_admin_network <--admin-network value, or "">: decide which addresses
# may use SSH after the install, and stop when that would lock the admin out.
# Packets of connections that are already open always pass the rules, so
# the SSH session that runs install.sh stays open. The check is about the
# next login. It sets SSH_ALLOWED (a range, one address, or "" for nobody)
# and SSH_ALLOWED_NOTE (where the value came from, for the rules file).
check_admin_network() {
    local wanted=$1 connection client="" server_port=""
    connection=$(ssh_connection)
    if [[ -n $connection ]]; then
        read -r client _ _ server_port <<<"$connection"
        client=${client#::ffff:}
    fi
    if [[ -n $client && $wanted != none && $server_port != "$FIREWALL_SSH_PORT" ]]; then
        fail "This SSH session reached sshd on port $server_port. The MEHO firewall allows SSH only on port $FIREWALL_SSH_PORT, so a new SSH login on port $server_port would get no answer." \
            "let sshd also listen on port $FIREWALL_SSH_PORT (a 'Port $FIREWALL_SSH_PORT' line in /etc/ssh/sshd_config), then run install.sh again."
    fi
    case $wanted in
        none) check_no_ssh "$client" ;;
        "") check_ssh_from_session "$client" ;;
        *) check_ssh_from_range "$wanted" "$client" ;;
    esac
}

check_no_ssh() {
    local client=$1
    SSH_ALLOWED=""
    SSH_ALLOWED_NOTE="Empty: install.sh ran with --admin-network none, so SSH is closed for every address."
    if [[ -n $client ]]; then
        warn "--admin-network none closes SSH for every address. This session stays open, but no new SSH login works after it ends."
    else
        ok "SSH is closed for every address (--admin-network none)"
    fi
}

check_ssh_from_session() {
    local client=$1
    [[ -n $client ]] ||
        fail "--admin-network is missing, and install.sh does not run in an SSH session, so it cannot tell which addresses may use SSH." \
            "pass the address range of your admin network, for example --admin-network 10.0.50.0/24, or --admin-network none to close SSH."
    is_ipv4 "$client" ||
        fail "--admin-network is missing, and this SSH session comes from $client, which is not an IPv4 address. The MEHO firewall allows SSH only over IPv4." \
            "pass the IPv4 range of your admin network, for example --admin-network 10.0.50.0/24."
    SSH_ALLOWED=$client
    SSH_ALLOWED_NOTE="Only the address of the SSH session that ran install.sh: no --admin-network was given."
    warn "No --admin-network was given, so SSH is allowed only from $client, the address of this SSH session. To allow a range, change the set admin_network in $MEHO_FIREWALL_FILE, then run: systemctl reload $MEHO_FIREWALL_UNIT"
}

check_ssh_from_range() {
    local range=$1 client=$2
    SSH_ALLOWED=$range
    SSH_ALLOWED_NOTE="Given with --admin-network at the install."
    if [[ -z $client ]]; then
        ok "SSH is allowed from $range"
    elif ! is_ipv4 "$client"; then
        warn "This SSH session comes from $client, which is not an IPv4 address. It stays open, but a new SSH login works only over IPv4, from $range."
    elif cidrs_overlap "$client" "$range"; then
        ok "SSH is allowed from $range, which holds the address of this session ($client)"
    else
        fail "This SSH session comes from $client, which is outside --admin-network $range. After the install, a new SSH login from $client would get no answer." \
            "pass a range that holds $client, or run install.sh from a machine inside $range or from the machine's console."
    fi
    if [[ ${range#*/} == 0 ]]; then
        warn "--admin-network $range allows SSH from every address."
    fi
}

# check_join_firewall <join address or "">: the rules drop RKE2's ports when
# they come from another machine, so a join cannot work with them as they
# are. The rules for other nodes come with the high-availability work
# (ADR 0009). Until then, the admin adds them by hand on both machines.
check_join_firewall() {
    local join_address=$1
    [[ -n $join_address ]] || return 0
    warn "--join-address is given, but the MEHO firewall drops RKE2's ports when they come from another machine. The join works only after both machines accept the other node's address: add accept rules to the chain prerouting in $MEHO_FIREWALL_FILE on each machine, then run: systemctl reload $MEHO_FIREWALL_UNIT"
}

# render_firewall_rules <SSH range, address or ""> <note> <pod range> <service range>:
# print the MEHO nftables file. The chain prerouting filters the traffic
# from the network. The chains input, forward and output carry the rules of
# the MEHO appliance for traffic to the machine, through it and from it
# (form-2 design, section 2, "One file for both forms"; ADR 0009 decision 1).
# On a form-2 machine their policy is accept and they have no drop rule, so
# they let every packet pass. The appliance build sets the three policies to
# drop: the CIS Ubuntu 24.04 rules 4.3.5 to 4.3.8 look for a base chain with
# policy drop on each of the hooks input, forward and output.
render_firewall_rules() {
    local ssh_allowed=$1 note=$2 cluster_cidr=$3 service_cidr=$4 elements=""
    local open_text closed_text
    open_text=$(join_by ' and ' "${FIREWALL_OPEN_TCP_PORTS[@]}")
    closed_text="$(join_by ', ' "${FIREWALL_CLOSED_TCP_PORTS[@]}") and $(join_by ', ' "${FIREWALL_CLOSED_UDP_PORTS[@]}")/UDP"
    if [[ -n $ssh_allowed ]]; then
        elements="
        elements = { ${ssh_allowed} }"
    fi
    cat <<EOF
#!/usr/sbin/nft -f
# MEHO host firewall: closed by default (ADR 0009).
#
# Written by MEHO install.sh. meho-firewall.service loads this file at every
# start of the machine, before the network and before RKE2. After a change,
# load it again with: systemctl reload ${MEHO_FIREWALL_UNIT}
#
# From another machine, only these answer:
#   - TCP ${open_text}, from every address: the console, the API and the
#     login. Port 80 only redirects to 443.
#   - SSH (TCP ${FIREWALL_SSH_PORT}) and ping, from the addresses in the set admin_network.
# The machine itself may open any connection, and the replies come back.
# Everything else from another machine is dropped, RKE2's own ports too:
# TCP ${closed_text}.
#
# The chain prerouting does this filtering. The chains input, forward and
# output hold the rules of the MEHO appliance, for traffic to this machine,
# through it and from it. The appliance sets their policy to drop. Here
# their policy is accept and they have no drop rule, so every packet passes
# them.
#
# The file changes only the table inet meho. It never runs "flush ruleset",
# because RKE2's network layer (Canal) and kube-proxy keep their own rules
# in other tables.
#
# To let more traffic in, add your own accept rules to the chain
# prerouting, before its last rule (counter). Then load the file again.
# Never add "flush ruleset" to this file.

# Replace the table in one step: create it when it is missing, delete it,
# and define it again. nft applies the whole file as one transaction.
table inet meho
delete table inet meho

table inet meho {
    # The addresses that may use SSH (IPv4 only).
    # ${note}
    set admin_network {
        type ipv4_addr
        flags interval${elements}
    }
EOF
    render_prerouting_chain "$cluster_cidr" "$service_cidr"
    render_input_chain
    render_forward_chain "$cluster_cidr" "$service_cidr"
    render_output_chain "$cluster_cidr" "$service_cidr"
    printf '}\n'
}

# render_interface_rules <iifname or oifname>: accept the interfaces of the
# machine's own Kubernetes (FIREWALL_CLUSTER_INTERFACES).
render_interface_rules() {
    local match=$1 interface
    for interface in "${FIREWALL_CLUSTER_INTERFACES[@]}"; do
        printf '        %s "%s" accept\n' "$match" "$interface"
    done
}

# render_inbound_accept_rules <match for "this machine", or "">: the accept
# rules for traffic from another machine to this one. The chains prerouting
# and input both print them, so the input chain of the appliance lets in
# the same traffic as the chain prerouting.
render_inbound_accept_rules() {
    local local_match=$1 open_ports
    open_ports=$(join_by ', ' "${FIREWALL_OPEN_TCP_PORTS[@]}")
    cat <<EOF
        ${local_match}tcp dport { ${open_ports} } accept
        ${local_match}ip saddr @admin_network tcp dport ${FIREWALL_SSH_PORT} accept
        ${local_match}ip saddr @admin_network icmp type echo-request accept

        # IPv6 neighbour discovery and router advertisements, and the
        # replies of DHCP servers.
        icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert, mld-listener-query } accept
        udp sport 67 udp dport 68 accept
        udp sport 547 udp dport 546 accept
EOF
}

# render_prerouting_chain <pod range> <service range>: the chain that filters
# the traffic from the network. The nft manual (nft(8), "CHAINS") and the
# nftables wiki ("Netfilter hooks") give the priorities: conntrack -200,
# mangle -150, dstnat -100. RKE2's port forwarding to Traefik is a
# destination NAT at dstnat, so the chain at mangle sees the packet first,
# with its original port, and connection tracking has already run.
render_prerouting_chain() {
    local cluster_cidr=$1 service_cidr=$2
    cat <<EOF

    # RKE2's Traefik takes ports 80 and 443 through a host port: a
    # destination NAT (priority dstnat, -100) turns a packet for port 443 of
    # this machine into a packet for the Traefik pod. This chain runs before
    # that (priority mangle, -150), so it sees the original port.
    chain prerouting {
        type filter hook prerouting priority mangle; policy drop;

        # The machine itself and its pods.
EOF
    render_interface_rules iifname
    cat <<EOF

        # From here on, the packet came in on a network card.
        ct state established,related accept
        ct state invalid drop

        # A pod or service address never comes from a network card.
        ip saddr { ${cluster_cidr}, ${service_cidr} } drop

        # "fib daddr type local": the packet is for this machine, not passed on.
EOF
    render_inbound_accept_rules "fib daddr type local "
    cat <<EOF

        # Counts the packets that the policy drops.
        counter
    }
EOF
}

# render_input_chain: the chain for traffic to this machine, after the port
# forwarding. It accepts what the chain prerouting accepts, without
# "fib daddr type local": the input hook sees only packets for this machine.
# Programs on the machine (the kubelet, the API server, etcd) reach each
# other over lo, also when they use the machine's own address. Pods reach
# them over their cali interfaces.
render_input_chain() {
    cat <<EOF

    # Traffic to this machine, after the port forwarding. The console on 80
    # and 443 normally does not come here: the port forwarding sends it to
    # the Traefik pod, through the chain forward. The appliance sets the
    # policy to drop. Here it is accept: the chain prerouting already filters.
    chain input {
        type filter hook input priority filter; policy accept;

        # The machine itself and its pods. Programs on this machine reach
        # each other over lo, also through the machine's own address.
EOF
    render_interface_rules iifname
    cat <<EOF

        ct state established,related accept

        # The same rules as in the chain prerouting.
EOF
    render_inbound_accept_rules ""
    cat <<EOF

        # Counts the packets that reach the policy.
        counter
    }
EOF
}

# render_forward_chain <pod range> <service range>: the chain for traffic
# through this machine: between the pods, from the pods to the network, and
# the console on its way to the Traefik pod.
render_forward_chain() {
    local cluster_cidr=$1 service_cidr=$2
    cat <<EOF

    # Traffic through this machine: to and from its pods. The appliance sets
    # the policy to drop. Here it is accept.
    chain forward {
        type filter hook forward priority filter; policy accept;

        ct state established,related accept

        # Packets that the port forwarding rewrote: the console on 80 and
        # 443, on its way to the Traefik pod.
        ct status dnat accept

        # The pod and service ranges.
        ip saddr { ${cluster_cidr}, ${service_cidr} } accept
        ip daddr { ${cluster_cidr}, ${service_cidr} } accept

        # Counts the packets that reach the policy.
        counter
    }
EOF
}

# render_output_chain <pod range> <service range>: the chain for traffic
# from this machine. Linux sends version 2 of the multicast listener
# reports (MLD) by default (force_mld_version 0, ip-sysctl), so both report
# types pass: without them, a switch that follows MLD may stop passing the
# IPv6 neighbour discovery to this machine.
render_output_chain() {
    local cluster_cidr=$1 service_cidr=$2
    cat <<EOF

    # Traffic from this machine. The appliance sets the policy to drop. Here
    # it is accept, because the OS and its other software belong to the
    # customer (ADR 0009 decision 2).
    chain output {
        type filter hook output priority filter; policy accept;

        # The machine itself and its pods, for example the health checks of
        # the kubelet.
EOF
    render_interface_rules oifname
    cat <<EOF

        ct state established,related accept
        ip daddr { ${cluster_cidr}, ${service_cidr} } accept

        # DNS, NTP, the requests of the DHCP clients, and ping.
        udp dport { 53, 123 } accept
        tcp dport 53 accept
        udp sport 68 udp dport 67 accept
        udp sport 546 udp dport 547 accept
        icmp type echo-request accept

        # IPv6 neighbour discovery, and the multicast listener reports of
        # MLD versions 1 and 2.
        icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert, mld-listener-report, mld2-listener-report } accept

        # Counts the packets that reach the policy.
        counter
    }
EOF
}

# render_firewall_unit <nft path>: the boot unit. It follows the layout of the
# nftables.service of Debian and Ubuntu, without its "flush ruleset". There
# is no stop command, so stopping the unit never leaves the machine open.
render_firewall_unit() {
    local nft=$1
    cat <<EOF
# Written by MEHO install.sh. It loads the MEHO host firewall,
# ${MEHO_FIREWALL_FILE}, at every start of the machine, before the network
# and before RKE2. Stopping the unit leaves the rules loaded. To remove them:
# nft delete table inet meho
[Unit]
Description=MEHO host firewall (nftables table inet meho)
Documentation=file:${MEHO_FIREWALL_FILE}
Wants=network-pre.target
Before=network-pre.target shutdown.target rke2-server.service
After=nftables.service
Conflicts=shutdown.target
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
StandardInput=null
ProtectSystem=full
ProtectHome=true
ExecStart=${nft} -f ${MEHO_FIREWALL_FILE}
ExecReload=${nft} -f ${MEHO_FIREWALL_FILE}

[Install]
WantedBy=sysinit.target
EOF
}

# render_rke2_firewall_dropin <nft path>: RKE2 starts only while the MEHO
# table is loaded. rke2-server.service restarts itself after a failed start,
# so RKE2 comes up as soon as the firewall is back.
render_rke2_firewall_dropin() {
    local nft=$1
    cat <<EOF
# Written by MEHO install.sh. RKE2 starts only while the MEHO host firewall
# is loaded, so RKE2's ports are never open to the network without it.
[Unit]
Wants=${MEHO_FIREWALL_UNIT}
After=${MEHO_FIREWALL_UNIT}

[Service]
ExecStartPre=/bin/sh -c '${nft} list table inet meho >/dev/null'
EOF
}

# write_firewall <pod range> <service range>: the rules file, the boot unit
# and the RKE2 drop-in. It uses SSH_ALLOWED, SSH_ALLOWED_NOTE and NFT_BIN
# from step 1.
write_firewall() {
    local cluster_cidr=$1 service_cidr=$2
    render_firewall_rules "$SSH_ALLOWED" "$SSH_ALLOWED_NOTE" "$cluster_cidr" "$service_cidr" |
        write_root_file "$MEHO_FIREWALL_FILE" 0600
    render_firewall_unit "$NFT_BIN" | write_root_file "$MEHO_FIREWALL_UNIT_FILE" 0644
    render_rke2_firewall_dropin "$NFT_BIN" | write_root_file "$RKE2_FIREWALL_DROPIN" 0644
    ok "wrote the MEHO firewall to $MEHO_FIREWALL_FILE, and its boot unit $MEHO_FIREWALL_UNIT"
}

# start_firewall: check the file, then load it through its unit. A file that
# nft refuses stops the install before any rule changes.
start_firewall() {
    local output state
    if ! output=$(nft -c -f "${HOST_ROOT}${MEHO_FIREWALL_FILE}" 2>&1); then
        printf '%s\n' "$output" >&2
        fail "nft does not accept the MEHO firewall file $MEHO_FIREWALL_FILE. Nothing was loaded." \
            "read nft's message above. A kernel without nftables features such as fib or ct cannot run the rules."
    fi
    systemctl daemon-reload
    systemctl enable "$MEHO_FIREWALL_UNIT" ||
        fail "systemd could not enable $MEHO_FIREWALL_UNIT." "see its log: $FIREWALL_LOG_COMMAND"
    systemctl restart "$MEHO_FIREWALL_UNIT" ||
        fail "systemd could not load the MEHO firewall." "see its log: $FIREWALL_LOG_COMMAND"
    state=$(firewall_state) ||
        fail "The MEHO firewall is not on after its start (${state#off: })." "see its log: $FIREWALL_LOG_COMMAND"
    ok "the MEHO firewall is on: from another machine, only TCP $(join_by ' and ' "${FIREWALL_OPEN_TCP_PORTS[@]}") answer, and SSH from ${SSH_ALLOWED:-no address}"
}

# replace_other_firewalls: turn firewalld and ufw off. It runs only after
# start_firewall has proven the MEHO rules loaded, so the machine is never
# without a firewall. RKE2's known-issues page says firewalld conflicts with
# Canal and should be disabled.
replace_other_firewalls() {
    if systemctl is-active --quiet firewalld.service || systemctl is-enabled --quiet firewalld.service; then
        systemctl disable --now firewalld.service ||
            fail "systemd could not turn firewalld off." \
                "run 'systemctl disable --now firewalld.service', then run install.sh again."
        ok "turned firewalld off: the MEHO firewall replaces it"
    fi
    local ufw_status=""
    if command -v ufw >/dev/null 2>&1; then
        ufw_status=$(ufw status 2>/dev/null) || ufw_status=""
    fi
    if grep -qx 'Status: active' <<<"$ufw_status"; then
        ufw disable >/dev/null ||
            fail "ufw could not be turned off." "run 'ufw disable', then run install.sh again."
        ok "turned ufw off: the MEHO firewall replaces it"
    fi
}

set_up_firewall() {
    write_firewall "$@"
    start_firewall
    replace_other_firewalls
}
