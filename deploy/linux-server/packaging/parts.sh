# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The steps of build-download.sh: each one puts one part of the download
# into the folder STAGE. This file is sourced, not run.
#
# Globals set by build-download.sh: LINUX_SERVER_DIR, PACKAGING_DIR,
# REPO_ROOT, WORK, STAGE, VERSION, CHART_VERSION, IMAGE_TAG, SIGNER_REF,
# WAIT_SECONDS, CLI_BINARY. This file sets HELM, CHART_DIGEST and
# IMAGE_DIGEST.

# fetch <url> <file>
fetch() {
    curl -fsSL --retry 3 --retry-delay 5 -o "$2" "$1" ||
        fail "Could not download $1." "check the network and run the script again."
}

add_scripts() {
    install -m 0755 "$LINUX_SERVER_DIR/install.sh" "$STAGE/install.sh"
    install -m 0755 "$LINUX_SERVER_DIR/meho-host" "$STAGE/meho-host"
    install -m 0644 "$LINUX_SERVER_DIR"/lib/*.sh "$STAGE/lib/"
    install -m 0644 "$LINUX_SERVER_DIR/storage/local-path-storage.yaml" "$STAGE/storage/"
    install -m 0644 "$LINUX_SERVER_DIR/values-single-node.yaml" "$STAGE/chart/"
    ok "install.sh, meho-host, lib/, storage/ and chart/values-single-node.yaml"
}

# Helm goes into the download, and the build uses the same binary to pull
# and render the chart.
add_helm() {
    local tarball=$WORK/helm.tar.gz reported
    fetch "https://get.helm.sh/helm-$HELM_VERSION-linux-amd64.tar.gz" "$tarball"
    check_sha256 "$tarball" "$HELM_SHA256" "helm-$HELM_VERSION-linux-amd64.tar.gz"
    tar -xzf "$tarball" -C "$WORK" linux-amd64/helm
    install -m 0755 "$WORK/linux-amd64/helm" "$STAGE/bin/helm"
    HELM=$STAGE/bin/helm
    reported=$("$HELM" version --short)
    [[ $reported == "$HELM_VERSION+"* ]] ||
        fail "bin/helm reports the version $reported, not $HELM_VERSION." "check the pinned Helm download."
    ok "bin/helm $HELM_VERSION (sha256 checked)"
}

add_cli() {
    local reported
    reported=$("$CLI_BINARY" version) ||
        fail "$CLI_BINARY does not run on this machine." "give MEHO_CLI_BINARY the Linux amd64 meho CLI."
    [[ $reported == "meho v$VERSION "* ]] ||
        fail "The meho CLI reports '$reported', not the version v$VERSION." \
            "give MEHO_CLI_BINARY the Linux amd64 CLI of the same release."
    install -m 0755 "$CLI_BINARY" "$STAGE/bin/meho"
    ok "bin/meho: $reported"
}

# cosign_verify <image reference by digest> <workflow file that signed it>
cosign_verify() {
    cosign verify "$1" \
        --certificate-identity "$SIGNER_PREFIX/$2@$SIGNER_REF" \
        --certificate-oidc-issuer "$OIDC_ISSUER" >/dev/null 2>"$WORK/cosign.log"
}

# published: the chart and the MEHO image of this version are on ghcr.io and
# signed by the workflows of this repository. PUBLISH_STATE says what is
# still missing.
published() {
    PUBLISH_STATE="the chart $MEHO_CHART_REPOSITORY:$CHART_VERSION is not on ghcr.io"
    CHART_DIGEST=$(regctl image digest "$MEHO_CHART_REPOSITORY:$CHART_VERSION" 2>/dev/null) || return 1
    PUBLISH_STATE="the image $MEHO_IMAGE_REPOSITORY:$IMAGE_TAG is not on ghcr.io"
    IMAGE_DIGEST=$(regctl image digest "$MEHO_IMAGE_REPOSITORY:$IMAGE_TAG" 2>/dev/null) || return 1
    PUBLISH_STATE="the chart $MEHO_CHART_REPOSITORY@$CHART_DIGEST has no valid signature from chart.yml@$SIGNER_REF"
    cosign_verify "$MEHO_CHART_REPOSITORY@$CHART_DIGEST" chart.yml || return 1
    PUBLISH_STATE="the image $MEHO_IMAGE_REPOSITORY@$IMAGE_DIGEST has no valid signature from image.yml@$SIGNER_REF"
    cosign_verify "$MEHO_IMAGE_REPOSITORY@$IMAGE_DIGEST" image.yml || return 1
}

# On a release tag, chart.yml and image.yml run at the same time as this
# build, so the build waits for them for at most WAIT_SECONDS.
wait_until_published() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    until published; do
        if ((SECONDS >= deadline)); then
            [[ $PUBLISH_STATE != *signature* ]] || cat "$WORK/cosign.log" >&2
            fail "After $((WAIT_SECONDS / 60)) minutes, $PUBLISH_STATE." \
                "check that chart.yml and image.yml passed for $SIGNER_REF, then run the build again."
        fi
        note "waiting: $PUBLISH_STATE"
        sleep 30
    done
    ok "chart $CHART_VERSION ($CHART_DIGEST) and image $IMAGE_TAG ($IMAGE_DIGEST) are signed by chart.yml and image.yml at $SIGNER_REF"
}

add_chart() {
    local output pulled_digest chart_file
    mkdir -p "$WORK/chart"
    output=$("$HELM" pull "oci://$MEHO_CHART_REPOSITORY" --version "$CHART_VERSION" -d "$WORK/chart" 2>&1) ||
        fail "helm could not pull the chart $CHART_VERSION: $output" "check the network and run the script again."
    pulled_digest=$(awk '/^Digest:/ { print $2; exit }' <<<"$output")
    [[ $pulled_digest == "$CHART_DIGEST" ]] ||
        fail "helm pulled the chart with the digest $pulled_digest, but cosign checked $CHART_DIGEST." \
            "run the build again; the chart tag moved while the build ran."
    chart_file=$(find "$WORK/chart" -maxdepth 1 -name '*.tgz' -print -quit)
    install -m 0644 "$chart_file" "$STAGE/chart/meho-$CHART_VERSION.tgz"
    ok "chart/meho-$CHART_VERSION.tgz (cosign verify passed for $CHART_DIGEST)"
}

add_rke2() {
    local base file expected
    base="https://github.com/rancher/rke2/releases/download/${RKE2_VERSION//+/%2B}"
    fetch "$base/sha256sum-amd64.txt" "$STAGE/rke2/sha256sum-amd64.txt"
    check_sha256 "$STAGE/rke2/sha256sum-amd64.txt" "$RKE2_CHECKSUMS_SHA256" "sha256sum-amd64.txt of RKE2 $RKE2_VERSION"
    for file in "${RKE2_FILES[@]}"; do
        fetch "$base/$file" "$STAGE/rke2/$file"
        expected=$(awk -v name="$file" '$2 == name { print $1 }' "$STAGE/rke2/sha256sum-amd64.txt")
        check_sha256 "$STAGE/rke2/$file" "$expected" "$file of RKE2 $RKE2_VERSION"
    done
    fetch "https://raw.githubusercontent.com/rancher/rke2/${RKE2_VERSION//+/%2B}/install.sh" "$STAGE/rke2/install.sh"
    check_sha256 "$STAGE/rke2/install.sh" "$RKE2_INSTALLER_SHA256" "RKE2's install.sh at $RKE2_VERSION"
    chmod 0644 "$STAGE"/rke2/*
    chmod 0755 "$STAGE/rke2/install.sh"
    ok "rke2/: RKE2 $RKE2_VERSION, every file checked against sha256sum-amd64.txt"
}

# chart_image_refs <helm> <chart> <values file> <image tag>: the images of
# the chart, rendered with the core on, as the form-2 install renders it.
chart_image_refs() {
    "$1" template meho "$2" --namespace meho \
        -f "$PACKAGING_DIR/render-values.yaml" -f "$3" --set "image.tag=$4" | images_in_manifests
}

add_images() {
    local refs=$WORK/image-refs.txt lock=$WORK/images.lock name digest
    {
        chart_image_refs "$HELM" "$STAGE/chart/meho-$CHART_VERSION.tgz" "$STAGE/chart/values-single-node.yaml" "$IMAGE_TAG"
        images_in_manifests <"$STAGE/storage/local-path-storage.yaml"
    } | LC_ALL=C sort -u >"$refs"
    lock_images "$refs" "$lock"
    awk -v prefix="$MEHO_IMAGE_REPOSITORY:" -v digest="$IMAGE_DIGEST" \
        'index($1, prefix) == 1 && $2 == digest { found = 1 } END { exit !found }' "$lock" ||
        fail "The chart does not use the signed MEHO image $MEHO_IMAGE_REPOSITORY@$IMAGE_DIGEST." \
            "build with the chart and the image of the same version."
    fetch_images "$lock" "$WORK/image-layout"
    write_image_archive "$WORK/image-layout" "$STAGE/$IMAGE_ARCHIVE"
    rm -rf "$WORK/image-layout"
    chmod 0644 "$STAGE/$IMAGE_ARCHIVE"
    while read -r name digest; do
        ok "image $name ($digest)"
    done <"$lock"
}

# licence_line_for <repository>: print "<licence files> <source>" from
# packaging/image-sources.txt, or nothing when the image has no line.
licence_line_for() {
    awk -v repository="$1" '!/^#/ && $1 == repository { $1 = ""; sub(/^ +/, ""); print; exit }' \
        "$PACKAGING_DIR/image-sources.txt"
}

# write_licence_readme <lock file>: LICENSES/README.txt names each part of
# the download, its licence file and where its source code is.
write_licence_readme() {
    local lock=$1 name digest line licences where
    cat <<EOF
Licences and source code of $(download_name "$VERSION")

Each part of this download is listed below with its licence (a file in this
folder) and the place of its source code. The container images also hold
the software of their base operating system (for example Debian, Alpine or
Red Hat UBI), each under its own licence.

MEHO $VERSION: install.sh, meho-host, lib/, storage/, the Helm chart $CHART_VERSION
(chart/) and the meho CLI (bin/meho)
  Licence: Apache-2.0.txt and MEHO-NOTICE.txt
  Source:  https://github.com/evoila/meho

RKE2 $RKE2_VERSION (rke2/), with the images in rke2-images.linux-amd64.tar.zst
  Licence: Apache-2.0.txt for RKE2. Each RKE2 image has its own licence.
  Source:  https://github.com/rancher/rke2/tree/$RKE2_VERSION
           (the image list is rke2-images-all.linux-amd64.txt of that release)

Helm $HELM_VERSION (bin/helm)
  Licence: Apache-2.0.txt
  Source:  https://github.com/helm/helm/tree/$HELM_VERSION

Container images in $IMAGE_ARCHIVE:
EOF
    while read -r name digest; do
        line=$(licence_line_for "${name%:*}")
        [[ -n $line ]] ||
            fail "No licence is known for the image ${name%:*}." \
                "add a line for ${name%:*} to deploy/linux-server/packaging/image-sources.txt."
        read -r licences where <<<"$line"
        printf '\n%s (%s)\n  Licence: %s\n  Source:  %s\n' "$name" "$digest" "${licences//,/ and }" "$where"
    done <"$lock"
}

add_licences() {
    install -m 0644 "$REPO_ROOT/LICENSE" "$STAGE/LICENSES/Apache-2.0.txt"
    install -m 0644 "$REPO_ROOT/NOTICE" "$STAGE/LICENSES/MEHO-NOTICE.txt"
    install -m 0644 "$PACKAGING_DIR"/licenses/*.txt "$STAGE/LICENSES/"
    write_licence_readme "$WORK/images.lock" >"$STAGE/LICENSES/README.txt"
    chmod 0644 "$STAGE/LICENSES/README.txt"
    ok "LICENSES/: $(find "$STAGE/LICENSES" -type f | wc -l) files"
}

write_version_file() {
    cat >"$STAGE/VERSION" <<EOF
MEHO_VERSION=$VERSION
MEHO_IMAGE_TAG=$IMAGE_TAG
CHART_VERSION=$CHART_VERSION
RKE2_VERSION=$RKE2_VERSION
HELM_VERSION=$HELM_VERSION
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
    chmod 0644 "$STAGE/VERSION"
    ok "VERSION"
}

# pack <output tar>: one top folder, owned by root, files sorted by name.
pack() {
    local output=$1 name
    name=$(download_name "$VERSION")
    chmod -R go-w "$STAGE"
    tar --create --file "$output" --directory "$WORK" --owner=0 --group=0 --numeric-owner --sort=name "$name"
}
