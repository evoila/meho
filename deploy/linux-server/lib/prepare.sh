# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Step 2 of install.sh: prepare the machine (form-2 design, section 5, step 2).
# It writes the MEHO folders, RKE2's settings with their pod security file,
# the join token, the MEHO image archive, the storage manifest and the
# settings of RKE2's Traefik, and installs the meho-host command. The host
# firewall of step 2 is in lib/firewall.sh. RKE2's CIS profile (its pod
# security file and its host steps) is in lib/cis.sh. This file is sourced,
# not run.

prepare_folders() {
    mkdir -p "${HOST_ROOT}${MEHO_STATE_DIR}" "${HOST_ROOT}${MEHO_CONFIG_DIR}"
    chmod 0700 "${HOST_ROOT}${MEHO_STATE_DIR}" "${HOST_ROOT}${MEHO_CONFIG_DIR}"
    ok "$MEHO_STATE_DIR and $MEHO_CONFIG_DIR exist, for root only"
}

# render_rke2_config <hostname> <cluster cidr> <service cidr> <join address or "">:
# print RKE2's settings (https://docs.rke2.io/reference/server_config).
# The join token is never in this file. RKE2 reads it from token-file.
# RKE2's CIS profile is always on (ADR 0009), with the pod security file of
# lib/cis.sh in place of RKE2's own.
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
profile: ${RKE2_CIS_PROFILE}
pod-security-admission-config-file: "${RKE2_POD_SECURITY_FILE}"
EOF
    if [[ -n $join_address ]]; then
        cat <<EOF
server: "https://${join_address}:9345"
token-file: "${MEHO_JOIN_TOKEN_FILE}"
EOF
    fi
}

# write_rke2_config takes the arguments of render_rke2_config. The file is
# for root only (mode 0600), and it replaces an older file in one step. The
# pod security file that the settings name is written first.
write_rke2_config() {
    local target="${HOST_ROOT}${RKE2_CONFIG_FILE}" temporary
    write_pod_security_config
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

# render_traefik_config: port 80 answers only with a redirect to 443
# (ADR 0009). RKE2 passes these values to its own Traefik chart
# (rke2-traefik 41.2.003 in RKE2 v1.37.1), which turns them into
# --entryPoints.web.http.redirections.entryPoint.* arguments of Traefik.
# A HelmChartConfig must have the name and namespace of RKE2's HelmChart
# (https://docs.rke2.io/helm).
render_traefik_config() {
    cat <<'EOF'
# Written by MEHO install.sh. Port 80 answers only with a redirect to 443.
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: rke2-traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ports:
      web:
        http:
          redirections:
            entryPoint:
              to: websecure
              scheme: https
              permanent: true
EOF
}

# RKE2 applies every manifest in this folder when it starts, and again when
# the file changes.
write_traefik_config() {
    render_traefik_config | write_root_file "$RKE2_MANIFESTS_DIR/$TRAEFIK_CONFIG_MANIFEST" 0600
    ok "wrote the setting that makes RKE2's Traefik redirect port 80 to 443 ($RKE2_MANIFESTS_DIR/$TRAEFIK_CONFIG_MANIFEST)"
}

# install_meho_host <download dir>: the host command, for the customer's work
# on the machine after the install (form-2 design, section 5), and the files
# of lib/ that it sources: the read-only state functions of the firewall and
# of RKE2's CIS profile. Every run of install.sh installs the version of its
# download.
install_meho_host() {
    local download_dir=$1 target="${HOST_ROOT}${MEHO_HOST_PATH}" lib_dir="${HOST_ROOT}${MEHO_HOST_LIB_DIR}" file
    mkdir -p "${target%/*}" "$lib_dir"
    for file in "${MEHO_HOST_LIB_FILES[@]}"; do
        install -m 0644 "$download_dir/$file" "$lib_dir/"
    done
    install -m 0755 "$download_dir/$MEHO_HOST_FILE" "$target"
    ok "installed meho-host to $MEHO_HOST_PATH, and the files it sources to $MEHO_HOST_LIB_DIR"
}
