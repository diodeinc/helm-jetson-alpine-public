#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "$script_dir/../.." && pwd)

l4t_archive=${HELM_L4T_ARCHIVE:-"$repo_dir/build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2"}
build_dir=${HELM_ALPINE_BUILD_DIR:-"$repo_dir/build/helm-alpine"}
image_name=${HELM_ALPINE_BUILDER_IMAGE:-"helm-alpine-builder:3.24"}

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Desktop is required to assemble the Linux filesystem on macOS." >&2
    exit 69
fi
if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop is installed but its Linux engine is not running." >&2
    exit 69
fi
if [ ! -f "$l4t_archive" ]; then
    echo "Missing NVIDIA Jetson Linux R39.2 BSP archive:" >&2
    echo "  $l4t_archive" >&2
    exit 66
fi

mkdir -p "$build_dir/work" "$build_dir/out"

docker build --platform linux/arm64 -t "$image_name" "$script_dir"

set -- \
    --rm \
    --platform linux/arm64 \
    -e HELM_ROOTFS_SIZE="${HELM_ROOTFS_SIZE:-2G}" \
    -v "$script_dir:/source:ro" \
    -v "$l4t_archive:/inputs/Jetson_Linux_R39.2.0_aarch64.tbz2:ro" \
    -v "$build_dir/work:/work" \
    -v "$build_dir/out:/out"

if [ -n "${HELM_SSH_AUTHORIZED_KEYS_FILE:-}" ]; then
    if [ ! -f "$HELM_SSH_AUTHORIZED_KEYS_FILE" ]; then
        echo "HELM_SSH_AUTHORIZED_KEYS_FILE is not a file." >&2
        exit 66
    fi
    set -- "$@" -v "$HELM_SSH_AUTHORIZED_KEYS_FILE:/run/helm_authorized_keys:ro"
fi

docker run "$@" "$image_name"

echo "Helm Alpine artifacts: $build_dir/out"
