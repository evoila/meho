#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# build.sh: build the MEHO appliance OVA, meho-appliance-<version>.ova, on
# the lab's vSphere cluster (form-1 design, section 1). The steps:
#   1. base image: the Ubuntu 24.04 cloud image OVA, checksum-verified,
#      deployed with govc; a build-only SSH key and the placeholder host
#      name go in through the image's user-data property; 4 processors,
#      16 GB, a 120 GB thin disk (section 5)
#   2. the form-2 install from the form-2 download, with the placeholder
#      host name and no settings
#   3. the appliance pieces under /opt/meho/appliance, and cloud-init off
#   4. clean for copying: clean.sh, started with systemd-run outside any
#      SSH session; it checks itself and powers off
#   5. export: hardware version 20 (section 6), govc export.ovf, our product
#      section and the OVF transports (rewrite-ovf.sh), the manifest, the
#      OVA and SHA256SUMS
#
# .github/workflows/appliance-build.yml runs it on the lab runner. It also
# runs by hand; see README.md. See docs/codebase/appliance-build.md.
#
# Exit codes:
#   0  the OVA is built
#   1  a step failed; the [FAIL] line says what to fix. The build VM stays
#      in vCenter so you can look at it; destroy it by hand
#   2  wrong usage

set -Eeuo pipefail

APPLIANCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly APPLIANCE_DIR

# The Ubuntu 24.04 cloud image OVA, one fixed serial, and the sha256 from
# the SHA256SUMS file next to it. To move to a newer serial, change both.
readonly DEFAULT_BASE_IMAGE_URL=https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.ova
readonly DEFAULT_BASE_IMAGE_SHA256=ce599f89777c6eaa1a3156698f004b7938dcc16680132d5f10da1269770e74a2
readonly VERSION_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
readonly CPUS=4
readonly MEMORY_MB=16384
readonly DISK_SIZE=120G
readonly HARDWARE_VERSION=20
readonly BUILD_USER=meho-build
readonly CLEAN_UNIT=meho-clean
# How long to wait for SSH after the deploy, and for the power-off after
# the clean script starts.
readonly SSH_WAIT_SECONDS=600
readonly POWER_OFF_WAIT_SECONDS=1200

VERSION=""
OUT_DIR=""
FROM_STEP=1
VM=""
WORK=""
SSH_KEY=""
SSH_OPTIONS=()

usage() {
    cat <<'HELP'
build.sh: build the MEHO appliance OVA on the lab's vSphere cluster.

Usage:
  build.sh [--from-step <1-5>] <version> <out-dir>

  <version>      The MEHO version without the leading "v", for example 0.38.0.
  <out-dir>      The folder for meho-appliance-<version>.ova and SHA256SUMS.
  --from-step N  Go on from step N with the build VM that exists: 2 the
                 form-2 install, 3 the appliance pieces, 4 the clean,
                 5 the export. Default: 1, a new build VM. 4 works only
                 when clean.sh stopped in its step 1, while RKE2 still
                 runs; after a later failure, destroy the VM and build
                 again from step 1.

Settings, from the environment:
  GOVC_URL, GOVC_USERNAME, GOVC_PASSWORD
                           Required. The vCenter and a login, as govc reads
                           them. GOVC_INSECURE=1 for a lab certificate.
  GOVC_DATASTORE, GOVC_FOLDER, GOVC_RESOURCE_POOL, GOVC_NETWORK
                           Required. Where the build VM goes.
  MEHO_DOWNLOAD            Required. The form-2 download of this version,
                           meho-linux-<version>-amd64.tar, already checked
                           against its signed SHA256SUMS-linux.
  MEHO_BUILD_HOSTNAME      Required. The placeholder host name of the build
                           VM. It must have a DNS record in the lab that
                           points to MEHO_BUILD_ADDRESS.
  MEHO_BUILD_ADDRESS, MEHO_BUILD_GATEWAY, MEHO_BUILD_DNS
                           Required. The build VM's address with prefix
                           (10.0.0.10/24), its gateway, and its DNS servers,
                           comma-separated.
  MEHO_BUILD_SSH_KEY       Required. A private SSH key for the build only,
                           with its public key next to it as <file>.pub.
                           Make a new one for every build.
  MEHO_BUILD_VM            The name of the build VM. Default:
                           meho-appliance-<version>. It must not exist yet.
  MEHO_BASE_IMAGE_URL, MEHO_BASE_IMAGE_SHA256
                           The Ubuntu cloud image OVA and its sha256.
                           Default: the pinned serial in this script.
  MEHO_BASE_IMAGE_CACHE    A folder that keeps the downloaded base image
                           between builds. Default: <out-dir>/.cache.

Tools on PATH: govc, ssh, scp, ssh-keygen, curl, tar, sha256sum, base64.
HELP
}

ok() { printf '[ok]   %s\n' "$*"; }

note() { printf '[note] %s\n' "$*"; }

step() { printf '\n== %s\n' "$*"; }

fail() {
    printf '[FAIL] %s Fix: %s\n' "$1" "$2" >&2
    exit 1
}

usage_error() {
    printf 'build.sh: %s\n' "$1" >&2
    printf "Run 'build.sh --help' to see the options.\n" >&2
    exit 2
}

on_error() {
    if ((BASHPID != $$)); then
        exit "$1"
    fi
    printf '[FAIL] build.sh stopped at line %s: a command failed with exit code %s. Fix: read the lines above, correct the cause and run the build again. The build VM %s stays in vCenter; destroy it with: govc vm.destroy %s\n' \
        "$2" "$1" "$VM" "$VM" >&2
    exit 1
}

need_env() {
    local name
    for name in "$@"; do
        [[ -n ${!name:-} ]] || usage_error "$name is not set."
    done
}

need_tools() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || usage_error "$tool is not on PATH."
    done
}

parse_options() {
    while (($# > 0)); do
        case $1 in
            -h | --help)
                usage
                exit 0
                ;;
            --from-step)
                [[ ${2-} =~ ^[1-5]$ ]] || usage_error "--from-step takes a number from 1 to 5."
                FROM_STEP=$2
                shift
                ;;
            --*) usage_error "unknown option: $1" ;;
            *)
                if [[ -z $VERSION ]]; then
                    VERSION=$1
                elif [[ -z $OUT_DIR ]]; then
                    OUT_DIR=$1
                else
                    usage_error "unknown argument: $1"
                fi
                ;;
        esac
        shift
    done
    [[ -n $VERSION && -n $OUT_DIR ]] || usage_error "it needs two arguments: <version> <out-dir>."
    [[ $VERSION =~ $VERSION_PATTERN ]] || usage_error "the version '$VERSION' is not like 0.36.0 (no leading v)."
}

check_settings() {
    need_env GOVC_URL GOVC_USERNAME GOVC_PASSWORD GOVC_DATASTORE GOVC_FOLDER GOVC_RESOURCE_POOL GOVC_NETWORK \
        MEHO_DOWNLOAD MEHO_BUILD_HOSTNAME MEHO_BUILD_ADDRESS MEHO_BUILD_GATEWAY MEHO_BUILD_DNS MEHO_BUILD_SSH_KEY
    [[ -f $MEHO_DOWNLOAD ]] || usage_error "MEHO_DOWNLOAD $MEHO_DOWNLOAD is not a file."
    [[ $MEHO_DOWNLOAD == */meho-linux-$VERSION-amd64.tar ]] ||
        usage_error "MEHO_DOWNLOAD must be meho-linux-$VERSION-amd64.tar, the form-2 download of version $VERSION."
    [[ -f $MEHO_BUILD_SSH_KEY && -f $MEHO_BUILD_SSH_KEY.pub ]] ||
        usage_error "MEHO_BUILD_SSH_KEY must name a private key with its public key next to it as $MEHO_BUILD_SSH_KEY.pub."
    [[ $MEHO_BUILD_ADDRESS == */* ]] || usage_error "MEHO_BUILD_ADDRESS must have a prefix, for example 10.0.0.10/24."
    [[ $MEHO_BUILD_HOSTNAME == *.* ]] || usage_error "MEHO_BUILD_HOSTNAME must be a full DNS name."
    need_tools govc ssh scp ssh-keygen curl tar sha256sum base64
    VM=${MEHO_BUILD_VM:-meho-appliance-$VERSION}
    SSH_KEY=$MEHO_BUILD_SSH_KEY
    SSH_OPTIONS=(
        -i "$SSH_KEY"
        -o IdentitiesOnly=yes
        -o StrictHostKeyChecking=accept-new
        -o UserKnownHostsFile="$WORK/known_hosts"
        -o ServerAliveInterval=30
        -o BatchMode=yes
        -o ConnectTimeout=10
    )
}

# ssh_build <command>: run a command on the build VM as the build user. The
# command is one string, built here on purpose.
ssh_build() {
    # shellcheck disable=SC2029
    ssh "${SSH_OPTIONS[@]}" "$BUILD_USER@${MEHO_BUILD_ADDRESS%/*}" "$@"
}

scp_build() {
    scp -q "${SSH_OPTIONS[@]}" "$@"
}

# Not govc vm.info: it also reads the VM's host, and a login with rights only
# on the folder, the pool, the datastore and the network may not read it.
vm_power_state() {
    govc object.collect -s "$VM" runtime.powerState
}

# user_data: the cloud-config for the build VM's first boot. Only the build
# user exists (no "default" in the users list, so cloud-init makes no
# "ubuntu" user), with the build key and sudo. The network is static, so
# the lab's DNS record for the placeholder name fits. The cloud image's own
# DHCP file goes, so there is one network file; clean.sh removes it too.
user_data() {
    local dns
    dns=$(printf '%s' "$MEHO_BUILD_DNS" | sed 's/,/, /g')
    cat <<EOT
#cloud-config
hostname: ${MEHO_BUILD_HOSTNAME%%.*}
fqdn: $MEHO_BUILD_HOSTNAME
prefer_fqdn_over_hostname: true
manage_etc_hosts: false
users:
  - name: $BUILD_USER
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - "$(cat "$SSH_KEY.pub")"
ssh_pwauth: false
disable_root: true
write_files:
  - path: /etc/netplan/60-meho-build.yaml
    permissions: "0600"
    content: |
      network:
        version: 2
        ethernets:
          build:
            match:
              name: "en*"
            dhcp4: false
            addresses: [$MEHO_BUILD_ADDRESS]
            routes:
              - to: default
                via: $MEHO_BUILD_GATEWAY
            nameservers:
              addresses: [$dns]
runcmd:
  - rm -f /etc/netplan/50-cloud-init.yaml
  - netplan apply
EOT
}

download_base_image() {
    local url=${MEHO_BASE_IMAGE_URL:-$DEFAULT_BASE_IMAGE_URL} sha256=${MEHO_BASE_IMAGE_SHA256:-$DEFAULT_BASE_IMAGE_SHA256}
    local cache=${MEHO_BASE_IMAGE_CACHE:-$OUT_DIR/.cache}
    BASE_IMAGE=$cache/${url##*/}
    mkdir -p "$cache"
    if [[ ! -f $BASE_IMAGE ]]; then
        note "downloading $url"
        curl -fsSL -o "$BASE_IMAGE.part" "$url"
        mv -f "$BASE_IMAGE.part" "$BASE_IMAGE"
    fi
    echo "$sha256  $BASE_IMAGE" | sha256sum -c - >/dev/null ||
        fail "The base image $BASE_IMAGE does not have the sha256 $sha256." \
            "delete the file and run the build again; if it is wrong again, check the serial and the sha256 in build.sh."
    ok "the base image ${url##*/} is checksum-verified"
}

deploy_build_vm() {
    local spec disk
    if [[ -n $(govc find -type m -name "$VM") ]]; then
        fail "The VM $VM already exists in vCenter." \
            "destroy it with 'govc vm.destroy $VM', or go on from a later step with --from-step."
    fi
    spec=$WORK/import.json
    # The image's own properties: the host name, the key and the user-data
    # (base64, as the image's description says). The spec shape is the one
    # govc import.spec prints for this OVA.
    cat >"$spec" <<EOT
{
  "DiskProvisioning": "thin",
  "IPAllocationPolicy": "dhcpPolicy",
  "IPProtocol": "IPv4",
  "PropertyMapping": [
    {"Key": "instance-id", "Value": "$VM"},
    {"Key": "hostname", "Value": "${MEHO_BUILD_HOSTNAME%%.*}"},
    {"Key": "public-keys", "Value": "$(cat "$SSH_KEY.pub")"},
    {"Key": "user-data", "Value": "$(user_data | base64 -w 0)"}
  ],
  "NetworkMapping": [{"Name": "VM Network", "Network": "$GOVC_NETWORK"}],
  "MarkAsTemplate": false,
  "PowerOn": false,
  "InjectOvfEnv": true,
  "WaitForIP": false,
  "Name": "$VM"
}
EOT
    govc import.ova -name "$VM" -options "$spec" "$BASE_IMAGE" >/dev/null
    govc vm.change -vm "$VM" -c "$CPUS" -m "$MEMORY_MB"
    # The image has one disk. cloud-init grows the root file system to the
    # disk's size at the first boot, so the disk changes before power-on.
    disk=$(govc device.ls -vm "$VM" | awk '/^disk-/ { print $1; exit }')
    [[ -n $disk ]] || fail "The VM $VM has no disk device." "look at 'govc device.ls -vm $VM'."
    govc vm.disk.change -vm "$VM" -disk.name "$disk" -size "$DISK_SIZE"
    # Hardware version 20 from the start: the image ships vmx-10, and the
    # in-guest trim of the clean step (fstrim) reaches the thin disk only
    # from virtual hardware 13 on.
    govc vm.upgrade -version "$HARDWARE_VERSION" -vm "$VM"
    govc vm.power -on "$VM" >/dev/null
    ok "the build VM $VM runs: $CPUS processors, $((MEMORY_MB / 1024)) GB, a $DISK_SIZE thin disk, hardware version $HARDWARE_VERSION"
}

wait_for_ssh() {
    local waited=0
    note "waiting for SSH at ${MEHO_BUILD_ADDRESS%/*}"
    until ssh_build true 2>/dev/null; do
        ((waited += 10))
        ((waited < SSH_WAIT_SECONDS)) ||
            fail "The build VM did not answer over SSH within $((SSH_WAIT_SECONDS / 60)) minutes." \
                "open the VM console in vCenter and read cloud-init's messages; check MEHO_BUILD_ADDRESS and the network."
        sleep 10
    done
    ok "the build VM answers over SSH"
}

run_form2_install() {
    local name
    name=$(basename "$MEHO_DOWNLOAD" .tar)
    scp_build "$MEHO_DOWNLOAD" "$BUILD_USER@${MEHO_BUILD_ADDRESS%/*}:$name.tar"
    ssh_build "tar -xf $name.tar && rm $name.tar"
    # No --admin-network: the firewall then lets in SSH only from this
    # session's address, which clean.sh removes.
    ssh_build "cd $name && sudo ./install.sh --hostname $MEHO_BUILD_HOSTNAME"
    ok "the form-2 install is done on $MEHO_BUILD_HOSTNAME"
}

add_appliance_pieces() {
    local name
    name=$(basename "$MEHO_DOWNLOAD" .tar)
    # The unpacked download without the RKE2 files and the image archive,
    # which are already where RKE2 reads them: install.sh --from-disk needs
    # the rest at first start.
    ssh_build "sudo rm -rf /opt/meho/appliance/download && sudo mkdir -p /opt/meho/appliance &&
        sudo cp -a $name /opt/meho/appliance/download &&
        sudo rm -rf /opt/meho/appliance/download/rke2 /opt/meho/appliance/download/images &&
        sudo chown -R root:root /opt/meho/appliance &&
        sudo touch /etc/cloud/cloud-init.disabled"
    # shortcut: the first-start service and its unit (#4812), the console
    # banner and the OS hardening (#4640) are not built yet; add their
    # install lines here when they land.
    ok "the appliance pieces are under /opt/meho/appliance, and cloud-init is off"
}

run_clean() {
    local waited=0 state
    scp_build "$APPLIANCE_DIR/clean.sh" "$BUILD_USER@${MEHO_BUILD_ADDRESS%/*}:meho-clean.sh"
    # /run is a RAM disk, so the script is not in the OVA. systemd-run
    # starts it outside this SSH session, which the script cuts. Its lines
    # go to the VM console as well as the journal, so a failed check can be
    # read in vCenter after SSH is closed. --collect: a failed unit does not
    # keep the name, so a second run after a failure in its step 1 works.
    ssh_build "sudo install -m 0700 meho-clean.sh /run/meho-clean.sh && rm meho-clean.sh &&
        sudo systemd-run --unit=$CLEAN_UNIT --collect -p StandardOutput=journal+console -p StandardError=journal+console \
            /run/meho-clean.sh --build-user $BUILD_USER"
    note "the clean script runs on the build VM; waiting for the power-off"
    until [[ $(vm_power_state) == poweredOff ]]; do
        ((waited += 15))
        ((waited < POWER_OFF_WAIT_SECONDS)) ||
            fail "The build VM is still on after $((POWER_OFF_WAIT_SECONDS / 60)) minutes, so clean.sh stopped or its check found something." \
                "open the VM console in vCenter and read the [FAIL] and [LEFT] lines."
        sleep 15
    done
    ok "the build VM is clean and powered off"
}

export_ova() {
    local export_dir=$WORK/export/$VM ovf name file manifest state files=() disks=()
    state=$(vm_power_state)
    [[ $state == poweredOff ]] ||
        fail "The build VM is $state, not powered off." "run the clean step first: --from-step 4."
    govc vm.upgrade -version "$HARDWARE_VERSION" -vm "$VM"
    rm -rf "$WORK/export"
    govc export.ovf -vm "$VM" -sha 0 "$WORK/export" >/dev/null
    ovf=$export_dir/$VM.ovf
    [[ -f $ovf ]] || fail "govc export.ovf wrote no $ovf." "look at the lines above."
    grep -q "<vssd:VirtualSystemType>vmx-$HARDWARE_VERSION</vssd:VirtualSystemType>" "$ovf" ||
        fail "The exported OVF is not at hardware version $HARDWARE_VERSION." \
            "check 'govc object.collect -s $VM config.version' and the ESXi version of the build host."
    "$APPLIANCE_DIR/rewrite-ovf.sh" "$ovf" "$APPLIANCE_DIR/product-section.xml" "$VERSION"
    name=meho-appliance-$VERSION
    manifest=$export_dir/$VM.mf
    rm -f "$manifest"
    for file in "$export_dir"/*; do
        files+=("${file##*/}")
        [[ ${file##*/} == "$VM.ovf" ]] || disks+=("${file##*/}")
    done
    for file in "${files[@]}"; do
        printf 'SHA256(%s)= %s\n' "$file" "$(sha256sum "$export_dir/$file" | cut -d ' ' -f 1)"
    done >"$manifest"
    # An OVA is a tar with the descriptor first, then the manifest.
    tar --format=ustar -C "$export_dir" -cf "$WORK/$name.ova" "$VM.ovf" "$VM.mf" "${disks[@]}"
    (cd "$WORK" && sha256sum "$name.ova" >SHA256SUMS)
    mv -f "$WORK/$name.ova" "$WORK/SHA256SUMS" "$OUT_DIR/"
    govc vm.destroy "$VM"
    ok "wrote $OUT_DIR/$name.ova and $OUT_DIR/SHA256SUMS; the build VM is destroyed"
}

main() {
    parse_options "$@"
    mkdir -p "$OUT_DIR"
    WORK=$(mktemp -d "$OUT_DIR/.build.XXXXXX")
    trap 'rm -rf "$WORK"' EXIT
    check_settings
    trap 'on_error "$?" "$LINENO"' ERR

    if ((FROM_STEP <= 1)); then
        step "Step 1: the base image"
        download_base_image
        deploy_build_vm
        wait_for_ssh
    fi
    if ((FROM_STEP <= 2)); then
        step "Step 2: the form-2 install"
        wait_for_ssh
        run_form2_install
    fi
    if ((FROM_STEP <= 3)); then
        step "Step 3: the appliance pieces"
        add_appliance_pieces
    fi
    if ((FROM_STEP <= 4)); then
        step "Step 4: clean for copying"
        run_clean
    fi
    step "Step 5: export"
    export_ova
    printf '\nbuild.sh: wrote %s/meho-appliance-%s.ova\n' "$OUT_DIR" "$VERSION"
}

main "$@"
