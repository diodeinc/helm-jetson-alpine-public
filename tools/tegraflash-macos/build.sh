#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)
INSTALL_DIR=${HELM_MACOS_TOOLS_DIR:-$REPO_ROOT/build/macos-tools}
IMAGE_NAME=${HELM_TEGRAFLASH_IMAGE:-helm-tegraflash-r39.2:macos}
MODE=${1:-all}

case "$MODE" in
    all|native|container) ;;
    *)
        echo "usage: $0 [all|native|container]" >&2
        exit 2
        ;;
esac

if [ "$MODE" = all ] || [ "$MODE" = native ]; then
    cargo build --locked --release --manifest-path "$SCRIPT_DIR/Cargo.toml"
    mkdir -p "$INSTALL_DIR/bin"
    cp -f "$SCRIPT_DIR/target/release/helm-apx-bridge" "$INSTALL_DIR/bin/helm-apx-bridge"
    codesign --force --sign - "$INSTALL_DIR/bin/helm-apx-bridge" >/dev/null
    echo "Installed native bridge: $INSTALL_DIR/bin/helm-apx-bridge"
fi

if [ "$MODE" = all ] || [ "$MODE" = container ]; then
    docker build --platform linux/arm64 -t "$IMAGE_NAME" "$SCRIPT_DIR"
    echo "Built recovery-tool image: $IMAGE_NAME"
fi
