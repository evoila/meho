# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Steps 4 to 6 of install.sh (form-2 design, section 5): install the MEHO
# core with the download's Helm, write the two one-time files, install the
# meho command and write the install record. This file is sourced, not run.
#
# No secret touches a command line, the output or a log here. The values of
# a Secret go from kubectl straight into a file that only root can read.

# Helm's time limit for the install, the same as the RKE2 wait.
readonly HELM_TIMEOUT=15m
# kubectl prints the values of a Secret with these templates, decoded. The
# $-names are the template's own, not shell variables.
# shellcheck disable=SC2016
readonly FIRST_LOGIN_TEMPLATE='{{range $key, $value := .data}}{{$key}}={{base64decode $value}}{{"\n"}}{{end}}'
readonly UNSEAL_KEY_TEMPLATE='{{base64decode .data.key}}{{"\n"}}'

# Set by read_download_version.
MEHO_VERSION=""
MEHO_IMAGE_TAG=""
CHART_VERSION=""
RKE2_VERSION=""
CHART_FILE=""

# version_value <VERSION file> <key>: one value of the download's VERSION file.
version_value() {
    awk -F= -v key="$2" '$1 == key { print substr($0, length(key) + 2); exit }' "$1"
}

# read_download_version <download dir>: the versions in the download's
# VERSION file, and the chart file they name.
read_download_version() {
    local download_dir=$1 file="$1/$VERSION_FILE"
    MEHO_VERSION=$(version_value "$file" MEHO_VERSION)
    MEHO_IMAGE_TAG=$(version_value "$file" MEHO_IMAGE_TAG)
    CHART_VERSION=$(version_value "$file" CHART_VERSION)
    RKE2_VERSION=$(version_value "$file" RKE2_VERSION)
    [[ -n $MEHO_VERSION && -n $MEHO_IMAGE_TAG && -n $CHART_VERSION && -n $RKE2_VERSION ]] ||
        fail "The VERSION file of the download is not complete." \
            "unpack the whole download again, and run install.sh from its folder."
    CHART_FILE=chart/meho-$CHART_VERSION.tgz
    [[ -f $download_dir/$CHART_FILE ]] ||
        fail "The download is not complete. Missing: $CHART_FILE." \
            "unpack the whole download again, and run install.sh from its folder."
    ok "the download holds MEHO $MEHO_VERSION, the chart $CHART_VERSION and RKE2 $RKE2_VERSION"
}

# tenant_id: the id in an existing values-place.yaml, so a second run keeps
# the id Keycloak already uses; on a fresh machine, a new one. The kernel
# gives a new random UUID every time its uuid file is read.
tenant_id() {
    local file="${HOST_ROOT}${MEHO_PLACE_VALUES}" stored=""
    if [[ -f $file ]]; then
        stored=$(awk -F'"' '$1 == "  firstTenantId: " { print $2; exit }' "$file")
    fi
    if [[ -n $stored ]]; then
        printf '%s\n' "$stored"
    else
        cat "${HOST_ROOT}/proc/sys/kernel/random/uuid"
    fi
}

# render_place_values <hostname> <tenant id> <admin user> <approver user> <pod range>:
# print the answers of this place as Helm values (form-2 design, section 6).
# shortcut: the two network-policy ranges are wiring, not a place answer,
# until the chart derives them from its built-in parts (#4047); drop them
# then, and the update (#4736) removes them from existing place files.
render_place_values() {
    local hostname=$1 tenant_id=$2 admin_user=$3 approver_user=$4 pod_range=$5
    cat <<EOF
# Written by MEHO install.sh: the answers of this place. A later run of
# install.sh passes this file to Helm again. It holds no secret.
ingress:
  host: "${hostname}"
coreKeycloak:
  hostname: "${hostname}"
  firstTenantId: "${tenant_id}"
  firstLogin:
    adminUsername: "${admin_user}"
    approverUsername: "${approver_user}"
# Until the chart knows its built-in parts: the pod range, so MEHO may
# reach the built-in PostgreSQL and OpenBao.
networkPolicy:
  postgresCIDR: "${pod_range}"
  vaultCIDR: "${pod_range}"
EOF
}

# write_file <machine path> <render function> [argument]...: write what the
# function prints to the path, below HOST_ROOT, for root only. It replaces
# an older file in one step.
write_file() {
    local target="${HOST_ROOT}$1" temporary
    shift
    mkdir -p "${target%/*}"
    temporary=$(mktemp "${target}.XXXXXX")
    "$@" >"$temporary"
    mv -f "$temporary" "$target"
}

# write_place_values takes the arguments of render_place_values.
write_place_values() {
    write_file "$MEHO_PLACE_VALUES" render_place_values "$@"
    ok "wrote the answers of this place to $MEHO_PLACE_VALUES"
}

# install_core <download dir>: Helm installs the chart, or upgrades it when
# the release exists, and waits until every pod and Job is done (form-2
# design, section 5, step 4). Helm's own output is not shown: it names
# kubectl commands, and the customer never sees Kubernetes. Its error
# messages are shown.
install_core() {
    local download_dir=$1
    note "installing the MEHO core. Each part of it may take up to ${HELM_TIMEOUT%m} minutes."
    "$download_dir/$HELM_FILE" upgrade --install "$HELM_RELEASE" "$download_dir/$CHART_FILE" \
        --kubeconfig "${HOST_ROOT}${RKE2_KUBECONFIG}" \
        --namespace "$HELM_NAMESPACE" --create-namespace \
        --wait --wait-for-jobs --timeout "$HELM_TIMEOUT" \
        --set "image.tag=$MEHO_IMAGE_TAG" \
        -f "$download_dir/$SINGLE_NODE_VALUES" \
        -f "${HOST_ROOT}${MEHO_PLACE_VALUES}" >/dev/null ||
        fail "Helm could not install the MEHO core. Its message is above." \
            "run 'meho-host support-bundle' and send the file to support."
    ok "the MEHO core runs: Helm release $HELM_RELEASE in namespace $HELM_NAMESPACE"
}

# write_secret_file <Secret> <template> <machine path>: the values of a
# Kubernetes Secret go from kubectl straight into a file for root only.
# False when the Secret cannot be read.
write_secret_file() {
    local secret=$1 template=$2 target="${HOST_ROOT}$3" temporary
    mkdir -p "${target%/*}"
    temporary=$(mktemp "${target}.XXXXXX")
    if ! rke2_kubectl -n "$HELM_NAMESPACE" get secret "$secret" -o go-template="$template" >"$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    mv -f "$temporary" "$target"
}

copy_first_login() {
    write_secret_file "$HELM_RELEASE-core-first-login" "$FIRST_LOGIN_TEMPLATE" "$MEHO_FIRST_LOGIN_FILE" ||
        fail "The Secret $HELM_RELEASE-core-first-login with the first-login passwords could not be read." \
            "run 'meho-host support-bundle' and send the file to support."
    ok "wrote the first-login passwords to $MEHO_FIRST_LOGIN_FILE, for root only"
}

# copy_unseal_key: false, with a warning, when the Secret does not exist.
# Without the Secret, OpenBao is in keyholder mode (a person unseals it
# after every restart), and there is no key to copy.
# shortcut: a missing Secret is only a warning, because nothing on main
# makes it yet (#4044 part 2, #4046); make it a [FAIL] when the OpenBao
# setup Job lands, since the Secret then always exists after a fresh install.
copy_unseal_key() {
    if ! write_secret_file "$HELM_RELEASE-core-vault-unseal" "$UNSEAL_KEY_TEMPLATE" "$MEHO_UNSEAL_KEY_FILE"; then
        warn "The Secret $HELM_RELEASE-core-vault-unseal does not exist, so no unseal-key file was written. OpenBao then needs a keyholder at every restart."
        return 1
    fi
    ok "wrote the unseal key to $MEHO_UNSEAL_KEY_FILE, for root only"
}

install_cli() {
    local download_dir=$1 target="${HOST_ROOT}${MEHO_CLI_PATH}"
    mkdir -p "${target%/*}"
    install -m 0755 "$download_dir/$CLI_FILE" "$target"
    ok "installed the meho command to $MEHO_CLI_PATH"
}

# render_install_record <date> <hostname> <public address> <tenant id>
# <admin user> <approver user> <pod range> <service range> <from disk 0|1>:
# print /etc/meho/install.json. A later run of install.sh reads it to find
# the install and its answers (form-2 design, section 7). No secret is in
# it. The values need no escaping: install.sh accepts only letters, digits
# and a few signs in them.
render_install_record() {
    local from_disk=false
    if (($9)); then
        from_disk=true
    fi
    cat <<EOF
{
  "meho_version": "${MEHO_VERSION}",
  "chart_version": "${CHART_VERSION}",
  "rke2_version": "${RKE2_VERSION}",
  "installed_at": "$1",
  "options": {
    "hostname": "$2",
    "public_address": "$3",
    "first_tenant_id": "$4",
    "admin_user": "$5",
    "approver_user": "$6",
    "cluster_cidr": "$7",
    "service_cidr": "$8",
    "from_disk": ${from_disk}
  }
}
EOF
}

# write_install_record takes the arguments of render_install_record.
write_install_record() {
    write_file "$MEHO_INSTALL_RECORD" render_install_record "$@"
    ok "wrote the install record to $MEHO_INSTALL_RECORD"
}

# print_result <hostname> <unseal key written 0|1>: the lines the customer
# needs next (form-2 design, section 5, step 6). The first-start list
# comes with #4042.
print_result() {
    printf '\ninstall.sh: MEHO %s is installed on %s.\n' "$MEHO_VERSION" "$1"
    printf '  Console:               https://%s/ui/\n' "$1"
    printf '  First-login passwords: %s (root only; delete it after the first login)\n' "$MEHO_FIRST_LOGIN_FILE"
    if (($2)); then
        printf '  Unseal key:            %s (root only; copy it to your key store, then delete it)\n' "$MEHO_UNSEAL_KEY_FILE"
    fi
    printf '  First-start list:      not in this version yet. Open the console address to check that MEHO answers.\n'
}
