# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# The MEHO image archive of the download: which images go in, how they are
# fetched, and how the archive is checked. This file is sourced, not run.
#
# Why the archive is an OCI image layout and not a "docker save" file:
# the chart and the storage manifest pin images by digest, for example
# busybox:1.37@sha256:bdf5.... RKE2 imports every archive in its images
# folder with containerd (k3s pkg/agent/containerd, preloadFile), and then
# names each image also <repository>@<digest of the imported manifest>.
# containerd keeps the original registry manifest only for an OCI layout.
# For a "docker save" archive it writes a new manifest with a new digest,
# so a pod that asks for the pinned digest would not find its image and
# would try to pull it from the internet. See
# docs/codebase/linux-server-download.md.

# images_in_manifests: read Kubernetes YAML on stdin and print every
# container image it names, one per line, without quotes.
images_in_manifests() {
    sed -n -E 's/^[[:space:]]*(-[[:space:]]+)?image:[[:space:]]*["'\'']?([^"'\''[:space:]]+)["'\'']?[[:space:]]*$/\2/p'
}

# normalize_image_ref <reference>: print "<repository> <tag> <digest>" with
# the repository in its full form (docker.io/library/busybox, not busybox).
# A missing tag or digest is printed as "-". A reference with neither gets
# the tag "latest", as container runtimes do. Returns 1 on a malformed
# reference.
normalize_image_ref() {
    local reference=$1 name digest=- tag=- domain rest last
    name=${reference%%@*}
    if [[ $reference == *@* ]]; then
        digest=${reference#*@}
        [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    fi
    last=${name##*/}
    if [[ $last == *:* ]]; then
        tag=${last##*:}
        name=${name%:*}
    fi
    [[ $name =~ ^[a-z0-9]+([._-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-]+[a-z0-9]+)*)*$ ]] || return 1
    [[ $tag == - || $tag =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || return 1
    domain=${name%%/*}
    if [[ $name != */* || ($domain != *.* && $domain != *:* && $domain != localhost) ]]; then
        domain=docker.io
        rest=$name
    else
        rest=${name#*/}
    fi
    [[ $domain == index.docker.io ]] && domain=docker.io
    [[ $domain == docker.io && $rest != */* ]] && rest=library/$rest
    [[ $tag == - && $digest == - ]] && tag=latest
    printf '%s/%s %s %s\n' "$domain" "$rest" "$tag" "$digest"
}

# archive_name <repository> <tag> <digest>: the name of an image in the
# archive. RKE2 needs a name with a tag (it adds the digest name itself).
# An image that is pinned by digest only gets the tag "digest-<hex>".
archive_name() {
    if [[ $2 != - ]]; then
        printf '%s:%s\n' "$1" "$2"
    else
        printf '%s:digest-%s\n' "$1" "${3#sha256:}"
    fi
}

# lock_images <file with references> <lock file>: write one line per image,
# "<name in the archive> <digest>". A reference with a digest keeps it. A
# reference with a tag only is resolved at the registry now, so every image
# is fetched by digest.
lock_images() {
    local references=$1 lock=$2 reference repository tag digest
    : >"$lock"
    while IFS= read -r reference; do
        [[ -n $reference ]] || continue
        read -r repository tag digest < <(normalize_image_ref "$reference") ||
            fail "The image reference '$reference' is not valid." "correct it in the chart or the storage manifest."
        if [[ $digest == - ]]; then
            digest=$(regctl image digest "$repository:$tag") ||
                fail "The registry did not answer the digest of $repository:$tag." \
                    "check the network and that the image exists, then run the script again."
        fi
        printf '%s %s\n' "$(archive_name "$repository" "$tag" "$digest")" "$digest" >>"$lock"
    done <"$references"
    LC_ALL=C sort -u -o "$lock" "$lock"
    local duplicates
    duplicates=$(awk '{ print $1 }' "$lock" | uniq -d | join_lines ' ')
    if [[ -n $duplicates ]]; then
        fail "These image names point to more than one digest: $duplicates." \
            "pin each image to one digest in the chart and the storage manifest."
    fi
}

# media_type_of <manifest file>: the media type of an image manifest or index.
media_type_of() {
    jq -r '.mediaType // (if has("manifests")
        then "application/vnd.oci.image.index.v1+json"
        else "application/vnd.oci.image.manifest.v1+json" end)' "$1"
}

# fetch_images <lock file> <layout folder>: fetch every image of the lock,
# for linux/amd64 only, into one OCI image layout. The top manifest of each
# image (often an index of several platforms) is kept as the registry
# serves it, so its digest stays the pinned digest. Of the other platforms
# only that index is kept, not their content.
fetch_images() {
    local lock=$1 layout=$2 name digest number=0 blob
    while read -r name digest; do
        regctl image copy --platform "$IMAGE_PLATFORM" "${name%:*}@$digest" "ocidir://$layout:image$number" >/dev/null ||
            fail "Could not fetch ${name%:*}@$digest for $IMAGE_PLATFORM." \
                "check the network and that the image has a $IMAGE_PLATFORM variant, then run the script again."
        number=$((number + 1))
    done <"$lock"
    # The top manifests go in only after the last copy: regctl removes every
    # file of the layout that its own index.json does not name.
    while read -r name digest; do
        blob=$layout/blobs/sha256/${digest#sha256:}
        regctl manifest get --format raw-body "${name%:*}@$digest" >"$blob.part" ||
            fail "Could not fetch the manifest ${name%:*}@$digest." "check the network and run the script again."
        [[ $(sha256_of "$blob.part") == "${digest#sha256:}" ]] ||
            fail "The registry sent a manifest for ${name%:*}@$digest with another digest." "run the script again."
        mv -f "$blob.part" "$blob"
    done <"$lock"
    write_layout_index "$lock" "$layout"
}

# write_layout_index <lock file> <layout folder>: name every image of the
# lock in index.json. containerd reads the name from the annotation
# io.containerd.image.name. index.json is not content-addressed, so writing
# it changes no digest.
write_layout_index() {
    local lock=$1 layout=$2 name digest blob
    while read -r name digest; do
        blob=$layout/blobs/sha256/${digest#sha256:}
        jq -n -c --arg name "$name" --arg digest "$digest" --arg type "$(media_type_of "$blob")" \
            --argjson size "$(stat -c %s -- "$blob")" '{
                mediaType: $type, digest: $digest, size: $size,
                annotations: {
                    "io.containerd.image.name": $name,
                    "org.opencontainers.image.ref.name": ($name | split(":") | last)
                }
            }'
    done <"$lock" | jq -s '{schemaVersion: 2, mediaType: "application/vnd.oci.image.index.v1+json", manifests: .}' \
        >"$layout/index.json"
    printf '{"imageLayoutVersion":"1.0.0"}\n' >"$layout/oci-layout"
}

# write_image_archive <layout folder> <archive>: pack the layout as a tar
# and compress it with zstd at the default level. The image layers are
# already compressed, so a higher level gains little, and the default level
# keeps the window far below what RKE2's reader accepts.
write_image_archive() {
    local layout=$1 archive=$2
    tar --create --directory "$layout" --owner=0 --group=0 --numeric-owner --sort=name \
        oci-layout index.json blobs | zstd -q -T0 -o "$archive"
}

# platform_manifest_of <layout> <digest>: print the digest of the linux/amd64
# manifest of an image: the digest itself for a single manifest, or the
# linux/amd64 entry of an index. Prints nothing when there is none.
platform_manifest_of() {
    local blob=$1/blobs/sha256/${2#sha256:} type
    type=$(media_type_of "$blob")
    if [[ $type == *index* || $type == *manifest.list* ]]; then
        jq -r --arg os "${IMAGE_PLATFORM%/*}" --arg arch "${IMAGE_PLATFORM#*/}" \
            '[.manifests[] | select(.platform.os == $os and .platform.architecture == $arch)][0].digest // empty' "$blob"
    else
        printf '%s\n' "$2"
    fi
}

# image_problem <layout> <name> <digest>: print what is missing or wrong in
# the linux/amd64 content of one image, or nothing when it is complete.
image_problem() {
    local layout=$1 name=$2 digest=$3 manifest blobs=$1/blobs/sha256 config platform layer
    [[ -f $blobs/${digest#sha256:} ]] || {
        printf '%s: its manifest %s is missing' "$name" "$digest"
        return
    }
    manifest=$(platform_manifest_of "$layout" "$digest")
    [[ -n $manifest && -f $blobs/${manifest#sha256:} ]] || {
        printf '%s: no %s manifest' "$name" "$IMAGE_PLATFORM"
        return
    }
    config=$(jq -r '.config.digest' "$blobs/${manifest#sha256:}")
    [[ -f $blobs/${config#sha256:} ]] || {
        printf '%s: its image config %s is missing' "$name" "$config"
        return
    }
    platform=$(jq -r '.os + "/" + .architecture' "$blobs/${config#sha256:}")
    [[ $platform == "$IMAGE_PLATFORM" ]] || {
        printf '%s: it is built for %s, not %s' "$name" "$platform" "$IMAGE_PLATFORM"
        return
    }
    while read -r layer; do
        [[ -f $blobs/${layer#sha256:} ]] || {
            printf '%s: its layer %s is missing' "$name" "$layer"
            return
        }
    done < <(jq -r '.layers[].digest' "$blobs/${manifest#sha256:}")
}

# verify_image_archive <archive> <lock file> <work folder>: the archive is
# an OCI image layout that RKE2 can read, every file in it has the digest
# it is named by, and it holds exactly the images of the lock, each under
# its locked digest and complete for linux/amd64.
verify_image_archive() {
    local archive=$1 lock=$2 layout=$3/image-layout actual mismatch problems name digest
    mkdir -p "$layout"
    if ! zstd -q -dc --memory="$ZSTD_DECODER_MEMORY" "$archive" | tar -x -C "$layout"; then
        fail "${archive##*/} cannot be read with $ZSTD_DECODER_MEMORY of zstd decoder memory, which is all RKE2 uses." \
            "compress the image archive with the default zstd settings (no --long, no --ultra)."
    fi
    if [[ ! -f $layout/oci-layout || ! -f $layout/index.json || ! -d $layout/blobs/sha256 ]] ||
        [[ $(jq -r '.imageLayoutVersion' "$layout/oci-layout") != 1.0.0 ]]; then
        fail "${archive##*/} is not an OCI image layout (oci-layout and index.json are missing)." \
            "build it with deploy/linux-server/build-download.sh; a \"docker save\" file changes the image digests."
    fi
    mismatch=$(cd "$layout/blobs/sha256" && sha256sum -- * | awk '{ sub(/^\*/, "", $2); if ($1 != $2) print $2 }' | join_lines ' ')
    if [[ -n $mismatch ]]; then
        fail "These files of ${archive##*/} do not have the digest they are named by: $mismatch." "build the download again."
    fi
    actual=$(jq -r '.manifests[] | (.annotations["io.containerd.image.name"] // "<no name>") + " " + .digest' \
        "$layout/index.json" | LC_ALL=C sort)
    if [[ $actual != "$(LC_ALL=C sort "$lock")" ]]; then
        local missing extra
        missing=$(LC_ALL=C comm -13 <(printf '%s\n' "$actual") <(LC_ALL=C sort "$lock") | join_lines '; ')
        extra=$(LC_ALL=C comm -23 <(printf '%s\n' "$actual") <(LC_ALL=C sort "$lock") | join_lines '; ')
        fail "The images in ${archive##*/} are not the images of the chart and the storage manifest, each by its digest. Expected but not in the archive: ${missing:-none}. In the archive but not expected: ${extra:-none}." \
            "build the download again from the same chart."
    fi
    problems=$(while read -r name digest; do
        image_problem "$layout" "$name" "$digest" && printf '\n'
    done <"$lock" | grep -v '^$' | join_lines '; ') || true
    if [[ -n $problems ]]; then
        fail "Some images in ${archive##*/} are not complete for $IMAGE_PLATFORM: $problems." "build the download again."
    fi
    ok "${archive##*/} is an OCI image layout with $(wc -l <"$lock") images, each under its pinned digest and complete for $IMAGE_PLATFORM"
}
