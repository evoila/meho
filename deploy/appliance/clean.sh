#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# clean.sh: step 4 of the appliance build (form-1 design, section 1):
# remove from the build VM everything that must differ between appliances,
# check that nothing is left, and power off. build.sh copies it to
# /run/meho-clean.sh (a RAM disk, so it is not in the OVA) and starts it
# with systemd-run, outside any SSH session, because it deletes the build
# user and closes SSH.
#
# What goes, in this order:
#   1. every Secret the core's Jobs made (label
#      meho.evoila.com/managed-by=core-bootstrap) and any other
#      <release>-core-* Secret; then nothing is removed before a check
#      proves none is left
#   2. RKE2 and its pods: rke2-server stops and no longer starts at boot,
#      and rke2-killall.sh stops the pods, which outlive the service; then
#      RKE2's own state: server/db, server/tls, server/cred and the token
#      files; everything under agent/ except agent/images/; the node
#      password; the kubeconfig; /var/lib/kubelet
#   3. MEHO's data, once nothing writes to it: everything under
#      /var/lib/meho/ (the data volumes, the OpenBao init in them, the two
#      one-time files, support bundles), and the install's answers under
#      /etc/meho/ (with the first tenant id)
#   4. the machine: the build user's sessions, the user, its key (also the
#      copy cloud-init puts in root's authorized_keys) and its sudo rule;
#      the build's network file; cloud-init's state, logs and the
#      machine-id; the SSH host keys; SSH closed for every address
#      (meho-host admin-network none); the logs; fstrim
# What stays: server/manifests/, agent/images/, /etc/rancher/rke2/config.yaml,
# the pod security file /etc/rancher/rke2/meho-pss.yaml and the appliance
# pieces under /opt/meho/appliance. The preset file that keeps RKE2 off at
# the first boot is written.
#
# Usage:
#   clean.sh [--build-user <name>]   clean, check, power off
#   clean.sh --check [--build-user <name>]
#                                    only the check; exit 1 when anything is left
#
# Exit codes:
#   0  clean and powered off, or (--check) nothing is left
#   1  a step failed, or the check found something. The VM stays on, so the
#      VM console in vCenter shows the lines above
#   2  wrong usage
#
# The tests in tests/ run this script against a fake machine. See
# docs/codebase/appliance-build.md.

set -Eeuo pipefail

# Tests set MEHO_TEST_ROOT to a temporary folder. Every path on the machine
# is then read and written below that folder. On a real machine it is not set.
HOST_ROOT="${MEHO_TEST_ROOT:-}"

readonly HELM_RELEASE=meho
readonly HELM_NAMESPACE=meho
readonly CORE_SECRET_LABEL=meho.evoila.com/managed-by=core-bootstrap
readonly MEHO_STATE_DIR=/var/lib/meho
readonly MEHO_PLACE_VALUES=/etc/meho/values-place.yaml
readonly MEHO_INSTALL_RECORD=/etc/meho/install.json
readonly RKE2_DIR=/var/lib/rancher/rke2
readonly RKE2_KUBECTL=$RKE2_DIR/bin/kubectl
readonly RKE2_KUBECONFIG=/etc/rancher/rke2/rke2.yaml
readonly RKE2_NODE_PASSWORD=/etc/rancher/node/password
readonly RKE2_CONFIG=/etc/rancher/rke2/config.yaml
readonly RKE2_POD_SECURITY=/etc/rancher/rke2/meho-pss.yaml
# systemd treats the appliance's first boot (machine-id "uninitialized") as
# a first boot and enables every unit without a preset rule. This file
# keeps RKE2 off until first start enables it (systemd.preset(5)).
readonly RKE2_PRESET=/etc/systemd/system-preset/10-meho-appliance.preset
# RKE2's tarball installer, which the form-2 install runs, puts it here.
readonly RKE2_KILLALL=/usr/local/bin/rke2-killall.sh
readonly KUBELET_DIR=/var/lib/kubelet
readonly RKE2_SERVER_STATE=(server/db server/tls server/cred server/token server/agent-token server/node-token)
readonly RKE2_KEEP=(server/manifests agent/images)
readonly APPLIANCE_DIR=/opt/meho/appliance
readonly APPLIANCE_INSTALL=$APPLIANCE_DIR/download/install.sh
readonly CLOUD_INIT_OFF=/etc/cloud/cloud-init.disabled
readonly BUILD_NETPLAN=/etc/netplan/60-meho-build.yaml
readonly SUDOERS_CLOUD_INIT=/etc/sudoers.d/90-cloud-init-users
readonly MACHINE_ID=/etc/machine-id
readonly ROOT_AUTHORIZED_KEYS=/root/.ssh/authorized_keys

BUILD_USER=meho-build
CHECK_ONLY=0
LEFT=0

usage() {
    cat <<'HELP'
clean.sh: clean the appliance build VM for copying, check it, power off.

Usage:
  clean.sh [--build-user <name>]
  clean.sh --check [--build-user <name>]

  --build-user <name>  The user the build logs in as. Default: meho-build.
  --check              Only check that nothing is left. Exit 1 when it is.
  -h, --help           Show this help and stop.
HELP
}

ok() { printf '[ok]   %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

fail() {
    printf '[FAIL] %s Fix: %s\n' "$1" "$2" >&2
    exit 1
}

usage_error() {
    printf 'clean.sh: %s\n' "$1" >&2
    printf "Run 'clean.sh --help' to see the options.\n" >&2
    exit 2
}

on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    printf '[FAIL] clean.sh stopped at line %s: a command failed with exit code %s. Fix: read the lines above on the VM console, correct the cause and run the build again.\n' \
        "$2" "$1" >&2
    exit 1
}

parse_options() {
    while (($# > 0)); do
        case $1 in
            -h | --help)
                usage
                exit 0
                ;;
            --check) CHECK_ONLY=1 ;;
            --build-user)
                [[ -n ${2-} && $2 != --* ]] || usage_error "--build-user needs a value."
                BUILD_USER=$2
                shift
                ;;
            *) usage_error "unknown option: $1" ;;
        esac
        shift
    done
}

kubectl() {
    "${HOST_ROOT}${RKE2_KUBECTL}" --kubeconfig "${HOST_ROOT}${RKE2_KUBECONFIG}" -n "$HELM_NAMESPACE" "$@"
}

# core_secrets: the <release>-core-* Secrets, one per line. False when
# kubectl cannot list the Secrets, so a failed kubectl never counts as none.
core_secrets() {
    local names
    names=$(kubectl get secret -o name) || return 1
    grep "^secret/${HELM_RELEASE}-core-" <<<"$names" || true
}

delete_core_secrets() {
    local name left
    kubectl delete secret -l "$CORE_SECRET_LABEL"
    # A <release>-core-* Secret without the label goes too.
    left=$(core_secrets) ||
        fail "kubectl cannot list the Secrets of the cluster. Nothing was removed from the machine." \
            "check that RKE2 runs ('meho-host check'), then run the build again."
    while read -r name; do
        [[ -n $name ]] && kubectl delete secret "${name#secret/}"
    done <<<"$left"
    left=$(core_secrets) ||
        fail "kubectl cannot list the Secrets of the cluster. Nothing was removed from the machine." \
            "check that RKE2 runs ('meho-host check'), then run the build again."
    [[ -z $left ]] ||
        fail "These Secrets are still in the cluster: ${left//$'\n'/, }. Nothing was removed from the machine." \
            "find out why the core's Jobs made a Secret that kubectl cannot delete, then run the build again."
    ok "the Secrets of the core are deleted"
}

remove_rke2_state() {
    local item
    systemctl disable --now rke2-server
    mkdir -p "${HOST_ROOT}${RKE2_PRESET%/*}"
    printf 'disable rke2-server.service\ndisable rke2-agent.service\n' >"${HOST_ROOT}${RKE2_PRESET}"
    # The unit stops only rke2, kubelet and containerd (KillMode=process), so
    # the pods keep running. The killall script stops them and unmounts their
    # volumes; their folders under /var/lib/kubelet go below.
    "${HOST_ROOT}${RKE2_KILLALL}"
    for item in "${RKE2_SERVER_STATE[@]}"; do
        rm -rf "${HOST_ROOT}${RKE2_DIR}/${item:?}"
    done
    find "${HOST_ROOT}${RKE2_DIR}/agent" -mindepth 1 -maxdepth 1 ! -name images -exec rm -rf {} +
    rm -f "${HOST_ROOT}${RKE2_NODE_PASSWORD}" "${HOST_ROOT}${RKE2_KUBECONFIG}"
    rm -rf --one-file-system "${HOST_ROOT}${KUBELET_DIR}"
    ok "RKE2 and its pods are stopped, RKE2 does not start at boot (also not at the first boot), and its state is gone; its images and manifests stay"
}

delete_meho_data() {
    find "${HOST_ROOT}${MEHO_STATE_DIR}" -mindepth 1 -delete
    # First start runs install.sh, which writes both again with a new tenant id.
    rm -f "${HOST_ROOT}${MEHO_PLACE_VALUES}" "${HOST_ROOT}${MEHO_INSTALL_RECORD}"
    ok "$MEHO_STATE_DIR is empty, and the install's answers under /etc/meho are gone"
}

remove_build_user() {
    local waited=0
    loginctl terminate-user "$BUILD_USER" || true
    pkill -KILL -u "$BUILD_USER" || true
    # userdel refuses a user with a running process, and pkill returns
    # before the processes are gone.
    while pgrep -u "$BUILD_USER" >/dev/null && ((waited++ < 30)); do
        sleep 1
    done
    userdel -r "$BUILD_USER"
    # cloud-init also writes the build key into root's file (disable_root).
    rm -f "${HOST_ROOT}${ROOT_AUTHORIZED_KEYS}" "${HOST_ROOT}${SUDOERS_CLOUD_INIT}" "${HOST_ROOT}${BUILD_NETPLAN}"
    ok "the build user $BUILD_USER, its keys, its sudo rule and the build's network file are gone"
}

clean_machine_identity() {
    cloud-init clean --logs --machine-id
    rm -f "${HOST_ROOT}"/etc/ssh/ssh_host_*
    ok "cloud-init's state, the machine-id and the SSH host keys are gone"
}

close_ssh() {
    meho-host admin-network none
    ok "SSH is closed for every address"
}

truncate_logs() {
    journalctl --rotate
    journalctl --vacuum-time=1s
    # Not the journal files: journald has them open, and the vacuum above
    # has emptied them.
    find "${HOST_ROOT}/var/log" -path "${HOST_ROOT}/var/log/journal" -prune -o -type f -exec truncate -s 0 {} +
    rm -f "${HOST_ROOT}"/root/.bash_history
    fstrim -av
    ok "the logs are empty and the free space is trimmed"
}

# left <what>: one line per thing that is still there. The check goes on,
# so the console shows the whole list.
left() {
    printf '[LEFT] %s\n' "$*" >&2
    LEFT=$((LEFT + 1))
}

check_none() {
    local path
    for path in "$@"; do
        if [[ -e ${HOST_ROOT}${path} || -L ${HOST_ROOT}${path} ]]; then
            left "$path is still there"
        fi
    done
}

check_core_secrets() {
    local secrets
    if [[ ! -e ${HOST_ROOT}${RKE2_DIR}/server/db ]]; then
        ok "no Kubernetes database, so no Secret"
    elif secrets=$(core_secrets 2>/dev/null); then
        if [[ -n $secrets ]]; then
            left "Secrets of the core: ${secrets//$'\n'/, }"
        else
            ok "no Secret of the core"
        fi
    else
        left "the Kubernetes database $RKE2_DIR/server/db, and kubectl cannot list its Secrets"
    fi
}

check_no_containers() {
    local rc=0
    pgrep -f containerd-shim >/dev/null || rc=$?
    case $rc in
        0) left "a pod of RKE2 still runs (a containerd-shim process)" ;;
        1) ;;
        *) left "pgrep cannot tell whether a pod of RKE2 still runs (exit code $rc)" ;;
    esac
}

check_clean() {
    local item found
    check_core_secrets
    check_no_containers
    found=$(find "${HOST_ROOT}${MEHO_STATE_DIR}" -mindepth 1 2>/dev/null | head -n 1)
    [[ -z $found ]] || left "files under $MEHO_STATE_DIR"
    for item in "${RKE2_SERVER_STATE[@]}"; do
        check_none "$RKE2_DIR/$item"
    done
    found=$(find "${HOST_ROOT}${RKE2_DIR}/agent" -mindepth 1 -maxdepth 1 ! -name images 2>/dev/null | head -n 1)
    [[ -z $found ]] || left "RKE2 state under $RKE2_DIR/agent/ other than images/"
    check_none "$RKE2_NODE_PASSWORD" "$RKE2_KUBECONFIG" "$KUBELET_DIR" "$MEHO_PLACE_VALUES" "$MEHO_INSTALL_RECORD" \
        "$BUILD_NETPLAN" "$SUDOERS_CLOUD_INIT" "$ROOT_AUTHORIZED_KEYS"
    if [[ $(systemctl is-enabled rke2-server 2>/dev/null || true) == enabled ]]; then
        left "rke2-server starts at boot"
    fi
    for item in "${RKE2_KEEP[@]}"; do
        [[ -d ${HOST_ROOT}${RKE2_DIR}/$item ]] || left "$RKE2_DIR/$item is missing; it must stay"
    done
    for item in "$RKE2_CONFIG" "$RKE2_POD_SECURITY" "$RKE2_PRESET" "$APPLIANCE_INSTALL" "$CLOUD_INIT_OFF"; do
        [[ -f ${HOST_ROOT}${item} ]] || left "$item is missing; it must stay"
    done
    found=$(find "${HOST_ROOT}/etc/ssh" -maxdepth 1 -name 'ssh_host_*' 2>/dev/null | head -n 1)
    [[ -z $found ]] || left "SSH host keys under /etc/ssh"
    if getent passwd "$BUILD_USER" >/dev/null 2>&1; then
        left "the build user $BUILD_USER"
    fi
    check_none "/home/$BUILD_USER"
    if [[ -s ${HOST_ROOT}${MACHINE_ID} && $(cat "${HOST_ROOT}${MACHINE_ID}") != uninitialized ]]; then
        left "a machine-id in $MACHINE_ID"
    fi
    ((LEFT == 0)) ||
        fail "$LEFT thing(s) are still on the machine; see the [LEFT] lines." \
            "fix clean.sh so that it removes them, and run the build again."
    ok "nothing that must differ between appliances is left"
}

main() {
    parse_options "$@"
    trap 'on_error "$?" "$LINENO"' ERR
    if ((CHECK_ONLY)); then
        step "Check the machine"
        check_clean
        exit 0
    fi
    step "Step 1: delete the Secrets of the core"
    delete_core_secrets
    step "Step 2: stop RKE2 and its pods, and remove RKE2's own state"
    remove_rke2_state
    step "Step 3: delete MEHO's data"
    delete_meho_data
    step "Step 4: remove the build user, the machine identity and the logs"
    remove_build_user
    clean_machine_identity
    close_ssh
    truncate_logs
    step "Step 5: check the machine"
    check_clean
    step "Step 6: power off"
    sync
    systemctl poweroff
}

main "$@"
