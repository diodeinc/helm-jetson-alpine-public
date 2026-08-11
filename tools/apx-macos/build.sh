#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "$script_dir/../.." && pwd)
build_dir=${HELM_APX_BUILD_DIR:-"$repo_dir/build/apx-macos"}
install_dir=${HELM_APX_INSTALL_DIR:-"$repo_dir/build/macos-tools"}

if ! command -v cmake >/dev/null 2>&1; then
    echo "cmake is required (brew install cmake)" >&2
    exit 69
fi
if ! command -v pkg-config >/dev/null 2>&1 ||
   ! pkg-config --exists libusb-1.0; then
    echo "libusb and pkg-config are required (brew install libusb pkg-config)" >&2
    exit 69
fi

cmake -S "$script_dir" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$install_dir"
cmake --build "$build_dir" --parallel
ctest --test-dir "$build_dir" --output-on-failure
cmake --install "$build_dir"

echo "Installed native tools to $install_dir"
