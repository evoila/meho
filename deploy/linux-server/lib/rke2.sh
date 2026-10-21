# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Step 3 of install.sh: install RKE2 from the download, start it, and wait
# until the node is Ready and the default storage class works (form-2 design,
# section 5, step 3). This file is sourced, not run.

readonly RKE2_WAIT_SECONDS=900
readonly RKE2_POLL_SECONDS=5
RKE2_DEADLINE=0
RKE2_POLLS=0

# RKE2's own installer, in offline mode (https://docs.rke2.io/install/airgap).
# It checks the files against sha256sum-amd64.txt, unpacks RKE2 and puts
# RKE2's image archive into the images folder.
install_rke2() {
    local download_dir=$1
    INSTALL_RKE2_ARTIFACT_PATH="$download_dir/rke2" sh "$download_dir/rke2/install.sh" ||
        fail "RKE2's installer stopped with an error." \
            "read its messages above, correct the cause and run install.sh again."
    ok "installed RKE2 from the download"
}

# rke2-server.service sets TimeoutStartSec=0, so a start that waits for the
# service can wait forever. The start is only queued here (--no-block), and
# wait_for_rke2 sets the time limit.
start_rke2() {
    systemctl enable --now --no-block rke2-server.service ||
        fail "systemd could not start rke2-server." "see RKE2's log: $RKE2_LOG_COMMAND"
    ok "started rke2-server"
}

# RKE2 unpacks its own kubectl when it starts, so it is missing before that.
rke2_kubectl() {
    local kubectl="${HOST_ROOT}${RKE2_KUBECTL}"
    [[ -x $kubectl ]] || return 1
    "$kubectl" --kubeconfig "${HOST_ROOT}${RKE2_KUBECONFIG}" --request-timeout=10s "$@" 2>/dev/null
}

node_is_ready() {
    local statuses
    statuses=$(rke2_kubectl get nodes \
        -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}') || return 1
    [[ $statuses =~ ^True( True)*$ ]]
}

default_storage_class_exists() {
    local is_default
    is_default=$(rke2_kubectl get storageclass local-path \
        -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}') || return 1
    [[ $is_default == true ]]
}

storage_provisioner_runs() {
    local available
    available=$(rke2_kubectl -n local-path-storage get deployment local-path-provisioner \
        -o jsonpath='{.status.availableReplicas}') || return 1
    [[ $available =~ ^[1-9][0-9]*$ ]]
}

# wait_until <what> <probe> [argument]...: run the probe every few seconds
# until it succeeds. All waits share one time limit. The limit is the clock
# (SECONDS), and also the number of polls, so that a test with a "sleep"
# that returns at once ends too.
wait_until() {
    local what=$1
    shift
    until "$@"; do
        if ((SECONDS >= RKE2_DEADLINE || RKE2_POLLS * RKE2_POLL_SECONDS >= RKE2_WAIT_SECONDS)); then
            fail "RKE2 was not ready after $((RKE2_WAIT_SECONDS / 60)) minutes: $what did not happen." \
                "see RKE2's log: $RKE2_LOG_COMMAND"
        fi
        sleep "$RKE2_POLL_SECONDS"
        RKE2_POLLS=$((RKE2_POLLS + 1))
    done
    ok "$what"
}

wait_for_rke2() {
    RKE2_DEADLINE=$((SECONDS + RKE2_WAIT_SECONDS))
    note "waiting for RKE2, at most $((RKE2_WAIT_SECONDS / 60)) minutes. To follow it: journalctl -u rke2-server -f"
    wait_until "the node is Ready" node_is_ready
    wait_until "local-path is the default storage class" default_storage_class_exists
    wait_until "the storage provisioner runs" storage_provisioner_runs
}
