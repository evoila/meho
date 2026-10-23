# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Step 2 of install.sh: prepare the machine (form-2 design, section 5, step 2).
# It writes the MEHO folders, RKE2's settings, the join token, the MEHO image
# archive and the storage manifest, and installs the meho-host command. This
# file is sourced, not run.

prepare_folders() {
    mkdir -p "${HOST_ROOT}${MEHO_STATE_DIR}" "${HOST_ROOT}${MEHO_CONFIG_DIR}"
    chmod 0700 "${HOST_ROOT}${MEHO_STATE_DIR}" "${HOST_ROOT}${MEHO_CONFIG_DIR}"
    ok "$MEHO_STATE_DIR and $MEHO_CONFIG_DIR exist, for root only"
}

# render_rke2_config <hostname> <cluster cidr> <service cidr> <join address or "">:
# print RKE2's settings (https://docs.rke2.io/reference/server_config).
# The join token is never in this file. RKE2 reads it from token-file.
render_rke2_config() {
    local hostname=$1 cluster_cidr=$2 service_cidr=$3 join_address=$4
    cat <<EOF
# Written by MEHO install.sh. A later run of install.sh writes it again.
write-kubeconfig-mode: "0600"
tls-san:
  - "${hostname}"
ingress-controller: traefik
cluster-cidr: "${cluster_cidr}"
service-cidr: "${service_cidr}"
EOF
    if [[ -n $join_address ]]; then
        cat <<EOF
server: "https://${join_address}:9345"
token-file: "${MEHO_JOIN_TOKEN_FILE}"
EOF
    fi
}

# write_rke2_config takes the arguments of render_rke2_config. The file is
# for root only (mode 0600), and it replaces an older file in one step.
write_rke2_config() {
    local target="${HOST_ROOT}${RKE2_CONFIG_FILE}" temporary
    mkdir -p "${target%/*}"
    temporary=$(mktemp "${target}.XXXXXX")
    render_rke2_config "$@" >"$temporary"
    mv -f "$temporary" "$target"
    ok "wrote RKE2's settings to $RKE2_CONFIG_FILE"
}

# copy_join_token <token file or "">: the token goes from file to file, so it
# is never on a command line or in the output.
copy_join_token() {
    local token_file=$1
    [[ -n $token_file ]] || return 0
    install -m 0600 "$token_file" "${HOST_ROOT}${MEHO_JOIN_TOKEN_FILE}"
    ok "copied the join token to $MEHO_JOIN_TOKEN_FILE, for root only"
}

# RKE2 imports every archive in its images folder at each start, so no image
# is ever pulled. RKE2's installer puts RKE2's own archive there in step 3,
# so this step copies only the MEHO archive.
copy_images() {
    local download_dir=$1 target="${HOST_ROOT}${RKE2_IMAGES_DIR}"
    mkdir -p "$target"
    install -m 0600 "$download_dir/$MEHO_IMAGES_ARCHIVE" "$target/"
    ok "copied the MEHO image archive to $RKE2_IMAGES_DIR"
}

# RKE2 applies every manifest in this folder when it starts.
copy_storage_manifest() {
    local download_dir=$1 target="${HOST_ROOT}${RKE2_MANIFESTS_DIR}"
    mkdir -p "$target"
    install -m 0600 "$download_dir/$STORAGE_MANIFEST" "$target/local-path-storage.yaml"
    ok "copied the storage manifest to $RKE2_MANIFESTS_DIR"
}

# install_meho_host <download dir>: the host command, for the customer's work
# on the machine after the install (form-2 design, section 5). Every run of
# install.sh installs the version of its download.
install_meho_host() {
    local download_dir=$1 target="${HOST_ROOT}${MEHO_HOST_PATH}"
    mkdir -p "${target%/*}"
    install -m 0755 "$download_dir/$MEHO_HOST_FILE" "$target"
    ok "installed meho-host to $MEHO_HOST_PATH"
}
