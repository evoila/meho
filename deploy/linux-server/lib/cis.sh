# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# RKE2's CIS profile, on from the first install (ADR 0009, closed by
# default). The profile is RKE2's built-in setting for the CIS Kubernetes
# Benchmark (https://docs.rke2.io/security/hardening_guide). This file has:
#
# - the host steps that RKE2 checks when it starts with the profile: the
#   etcd user and group, and RKE2's own kernel settings file, copied to
#   /etc/sysctl.d and loaded. They run after RKE2's installer, which brings
#   the file, and before RKE2 starts. Each step is safe to run again. The
#   install records which part of the etcd account it created, so
#   meho-host uninstall removes only that part (#5133).
# - the pod security file that RKE2's settings name: RKE2's own file for
#   the profile, with one more exempt namespace, local-path-storage.
# - cis_state, which changes nothing, for meho-host check (#4638).
#
# This file is sourced, not run.

readonly RKE2_CIS_PROFILE=cis
readonly RKE2_POD_SECURITY_FILE=/etc/rancher/rke2/meho-pss.yaml
# The places where RKE2's installer puts its kernel settings file: the
# tarball under /usr/local, or under /opt/rke2 when /usr/local is read-only
# or a mount point; the RPM under /usr. install.sh always installs the
# tarball (INSTALL_RKE2_ARTIFACT_PATH), on Ubuntu and RHEL alike.
readonly RKE2_CIS_SYSCTL_SOURCES=(
    /usr/local/share/rke2/rke2-cis-sysctl.conf
    /opt/rke2/share/rke2/rke2-cis-sysctl.conf
    /usr/share/rke2/rke2-cis-sysctl.conf
)
readonly CIS_SYSCTL_FILE=/etc/sysctl.d/60-rke2-cis.conf
readonly SYSCTL_UNIT=systemd-sysctl.service
readonly ETCD_ACCOUNT=etcd
# The parts of the etcd account that the install created: "user", "group"
# or both, one per line. meho-host uninstall reads it before it removes
# /etc/meho and removes only these parts.
readonly ETCD_ACCOUNT_RECORD=/etc/meho/etcd-account

# render_pod_security_config: RKE2's own pod security file for the CIS
# profile (restrictedPSAConfig in pkg/rke2/psa.go of RKE2), with
# local-path-storage added to the exempt namespaces. The "restricted"
# standard then applies in every other namespace, the MEHO namespace too.
# The local-path provisioner starts its helper pods in its own namespace,
# and they use folders of the host for the volumes, which "restricted"
# refuses. tests/fixtures/rke2-pod-security-cis.yaml is RKE2's file.
render_pod_security_config() {
    cat <<'EOF'
# Written by MEHO install.sh. A later run of install.sh writes it again.
# RKE2's own pod security file for its CIS profile, with one more exempt
# namespace: local-path-storage. The storage provisioner's helper pods there
# use folders of this machine for the volumes.
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
- name: PodSecurity
  configuration:
    apiVersion: pod-security.admission.config.k8s.io/v1beta1
    kind: PodSecurityConfiguration
    defaults:
      enforce: "restricted"
      enforce-version: "latest"
      audit: "restricted"
      audit-version: "latest"
      warn: "restricted"
      warn-version: "latest"
    exemptions:
      usernames: []
      runtimeClasses: []
      namespaces: [kube-system, compliance-operator-system, tigera-operator, local-path-storage]
EOF
}

# RKE2 writes its own file only when its settings name no file, so this one
# replaces it. The API server reads it at its start.
write_pod_security_config() {
    render_pod_security_config | write_root_file "$RKE2_POD_SECURITY_FILE" 0600
    ok "wrote the pod security file of RKE2's CIS profile to $RKE2_POD_SECURITY_FILE"
}

# RKE2 looks the etcd user and group up in /etc/passwd and /etc/group only,
# not through other name services (hardening guide), so these files decide.
etcd_user_exists() { grep -q "^${ETCD_ACCOUNT}:" "${HOST_ROOT}/etc/passwd" 2>/dev/null; }

etcd_group_exists() { grep -q "^${ETCD_ACCOUNT}:" "${HOST_ROOT}/etc/group" 2>/dev/null; }

# etcd_account_recorded <part>: true when the record names the part, user
# or group, as created by the install.
etcd_account_recorded() {
    grep -qx -- "$1" "${HOST_ROOT}${ETCD_ACCOUNT_RECORD}" 2>/dev/null
}

# record_etcd_account <part>...: add the parts that this run created to the
# record. A part that the record names already stays in it, so a later run
# keeps the answer of the run that created the part.
record_etcd_account() {
    local created=" $* " part parts=()
    for part in user group; do
        if etcd_account_recorded "$part" || [[ $created == *" $part "* ]]; then
            parts+=("$part")
        fi
    done
    {
        cat <<EOF
# Written by MEHO install.sh: the parts of the $ETCD_ACCOUNT account for RKE2's
# etcd that the install created. meho-host uninstall removes only these.
EOF
        printf '%s\n' "${parts[@]}"
    } | write_root_file "$ETCD_ACCOUNT_RECORD" 0600
}

# create_etcd_account: RKE2 runs etcd as this user with the CIS profile and
# stops at its start when the user or the group is missing. The command is
# the one of the hardening guide. A user or group that exists already stays
# as it is. The record is written right after the part is created, so a
# run that stops later still leaves the answer for the next run and for
# meho-host uninstall.
create_etcd_account() {
    if etcd_user_exists && etcd_group_exists; then
        ok "the $ETCD_ACCOUNT user and group for RKE2's etcd exist"
        return 0
    fi
    if etcd_user_exists; then
        groupadd -r "$ETCD_ACCOUNT" ||
            fail "groupadd could not create the $ETCD_ACCOUNT group." \
                "read its message above, correct the cause and run install.sh again."
        record_etcd_account group
        ok "created the $ETCD_ACCOUNT group for RKE2's etcd. The $ETCD_ACCOUNT user existed before."
    elif etcd_group_exists; then
        useradd -r -c "etcd user" -s /sbin/nologin -M -g "$ETCD_ACCOUNT" "$ETCD_ACCOUNT" ||
            fail "useradd could not create the $ETCD_ACCOUNT user." \
                "read its message above, correct the cause and run install.sh again."
        record_etcd_account user
        ok "created the $ETCD_ACCOUNT user for RKE2's etcd. The $ETCD_ACCOUNT group existed before."
    else
        useradd -r -c "etcd user" -s /sbin/nologin -M -U "$ETCD_ACCOUNT" ||
            fail "useradd could not create the $ETCD_ACCOUNT user and group." \
                "read its message above, correct the cause and run install.sh again."
        record_etcd_account user group
        ok "created the $ETCD_ACCOUNT user and group for RKE2's etcd"
    fi
}

# cis_sysctl_source: print the path of RKE2's kernel settings file on this
# machine, or return 1 when RKE2's installer put it in none of the places.
cis_sysctl_source() {
    local path
    for path in "${RKE2_CIS_SYSCTL_SOURCES[@]}"; do
        if [[ -f ${HOST_ROOT}${path} ]]; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 1
}

# sysctl_file_settings <file>: print each setting of a sysctl.d file as
# "<key> <value>". Empty lines and comments (# or ;) are left out, and so is
# the "-" in front of a key that systemd-sysctl may skip.
sysctl_file_settings() {
    awk '
        /^[[:space:]]*([#;]|$)/ || index($0, "=") == 0 { next }
        {
            key = substr($0, 1, index($0, "=") - 1)
            value = substr($0, index($0, "=") + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            sub(/^-/, "", key)
            print key, value
        }' "$1"
}

# kernel_setting <key>: print the value the kernel has now, from /proc/sys,
# with single spaces between the parts of a value.
kernel_setting() {
    local path="${HOST_ROOT}/proc/sys/${1//.//}" parts
    [[ -r $path ]] || return 1
    read -r -a parts <"$path" || [[ ${#parts[@]} -gt 0 ]] || return 1
    printf '%s\n' "${parts[*]}"
}

# cis_kernel_settings_loaded <sysctl file>: return 0 when the kernel has
# every setting of the file. Otherwise print the first setting it does not
# have, as "<key> is <now>, but RKE2 needs <value>", and return 1. The
# values come from the file, so they follow RKE2's file of each version.
cis_kernel_settings_loaded() {
    local key value current needed
    while read -r key value; do
        read -r -a needed <<<"$value"
        current=$(kernel_setting "$key") || current="not readable"
        if [[ $current != "${needed[*]}" ]]; then
            printf '%s is %s, but RKE2 needs %s\n' "$key" "$current" "${needed[*]}"
            return 1
        fi
    done < <(sysctl_file_settings "$1")
}

# load_cis_kernel_settings: copy RKE2's kernel settings file to
# /etc/sysctl.d, where systemd-sysctl loads it at every start of the
# machine, and load it now (hardening guide). RKE2 stops at its start when
# the kernel does not have these values. The load runs only when the kernel
# lacks one of them, so a second run of install.sh changes nothing. The
# check after the load reads every value back: a file that systemd-sysctl
# reads later can set one of them again.
load_cis_kernel_settings() {
    local rke2_file target="${HOST_ROOT}${CIS_SYSCTL_FILE}" reason
    rke2_file=$(cis_sysctl_source) ||
        fail "RKE2's kernel settings file for its CIS profile is in none of these places: $(join_by ', ' "${RKE2_CIS_SYSCTL_SOURCES[@]}")." \
            "RKE2's installer puts it there. Run install.sh again from the complete download."
    if [[ ! -f $target || $(<"$target") != "$(<"${HOST_ROOT}${rke2_file}")" ]]; then
        mkdir -p "${target%/*}"
        install -m 0644 "${HOST_ROOT}${rke2_file}" "$target"
        ok "copied RKE2's kernel settings for its CIS profile from $rke2_file to $CIS_SYSCTL_FILE"
    fi
    if cis_kernel_settings_loaded "$target" >/dev/null; then
        ok "the kernel has RKE2's settings for its CIS profile"
        return 0
    fi
    systemctl restart "$SYSCTL_UNIT" ||
        fail "systemd could not load the kernel settings." "see its log: journalctl -u $SYSCTL_UNIT -e"
    reason=$(cis_kernel_settings_loaded "$target") ||
        fail "After the load, the kernel still does not have RKE2's settings for its CIS profile: $reason." \
            "a file that systemd-sysctl reads after $CIS_SYSCTL_FILE sets this key to another value, for example /etc/sysctl.conf or a file in /etc/sysctl.d with a higher number. Remove that line, then run install.sh again."
    ok "loaded RKE2's kernel settings for its CIS profile"
}

# prepare_cis_host: the host steps of the profile. They run after RKE2's
# installer and before RKE2 starts.
prepare_cis_host() {
    create_etcd_account
    load_cis_kernel_settings
}

# cis_state: say whether RKE2's CIS profile is on for this machine. It
# changes nothing, so meho-host check (#4638) can call it. It prints "on",
# or "off: <reason>", and returns 0 for on and 1 for off. On means: RKE2's
# settings set the profile, the pod security file exists, the etcd user
# and group exist, the kernel settings file is in /etc/sysctl.d, and the
# kernel has its values. It needs RKE2_CONFIG_FILE from lib/common.sh.
cis_state() {
    local reason
    if ! grep -qxE "profile: *\"?${RKE2_CIS_PROFILE}\"?" "${HOST_ROOT}${RKE2_CONFIG_FILE}" 2>/dev/null; then
        printf 'off: RKE2'"'"'s settings (%s) do not set profile: %s\n' "$RKE2_CONFIG_FILE" "$RKE2_CIS_PROFILE"
        return 1
    fi
    if [[ ! -s ${HOST_ROOT}${RKE2_POD_SECURITY_FILE} ]]; then
        printf 'off: the pod security file %s is missing, so RKE2 cannot start\n' "$RKE2_POD_SECURITY_FILE"
        return 1
    fi
    if ! etcd_user_exists || ! etcd_group_exists; then
        printf 'off: the %s user or group is missing in /etc/passwd or /etc/group, so RKE2 does not start with the profile\n' \
            "$ETCD_ACCOUNT"
        return 1
    fi
    if [[ ! -f ${HOST_ROOT}${CIS_SYSCTL_FILE} ]]; then
        printf 'off: %s is missing, so the next start of the machine does not load RKE2'"'"'s kernel settings\n' \
            "$CIS_SYSCTL_FILE"
        return 1
    fi
    if ! reason=$(cis_kernel_settings_loaded "${HOST_ROOT}${CIS_SYSCTL_FILE}"); then
        printf 'off: the kernel setting %s, so RKE2 does not start with the profile\n' "$reason"
        return 1
    fi
    printf 'on\n'
}
