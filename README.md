# Helm Jetson Alpine

Experimental, source-only bring-up kit for running a small Alpine Linux system
on the Diode Helm carrier with an NVIDIA Jetson Orin NX module. The current
target is the 8 GB `P3767-0001` module, using Alpine 3.24 userspace and the
Jetson Linux R39.2 kernel and boot stack.

Alpine is a good fit for Helm as a headless appliance: it is compact, boots
with OpenRC, and keeps the base system easy to audit. CUDA and NVIDIA's
glibc-only compute/graphics userland are intentionally outside the base image;
add a compatible container or glibc layer if the product needs them.

## Status

This is bring-up software, not a production release. It builds the root
filesystem and offline signed recovery payloads on Apple-silicon macOS, and it
provides native APX inspection and an experimental USB/IP bridge. Live flashing
is deliberately not wrapped in a one-command script yet.

The repository contains no NVIDIA BSP, generated firmware, flash images,
device identifiers, logs, credentials, or signing keys.

## Layout

- `os/alpine`: Alpine root filesystem, initramfs, device-tree overlay, OpenRC
  services, and peripheral helpers
- `tools/apx-macos`: read-only native macOS APX discovery and inspection tool
- `tools/tegraflash-macos`: native USB/IP bridge and arm64 container for
  NVIDIA's R39.2 Linux recovery utilities

## Build on macOS

Requirements:

- Apple-silicon Mac and Docker Desktop
- CMake, Rust, `libusb`, and `pkg-config`
- NVIDIA's `Jetson_Linux_R39.2.0_aarch64.tbz2` BSP archive

```sh
brew install cmake libusb pkg-config rust
mkdir -p build/downloads
# Place Jetson_Linux_R39.2.0_aarch64.tbz2 in build/downloads.

./tools/apx-macos/build.sh
./os/alpine/build.sh
./tools/tegraflash-macos/helm-flash build
./tools/tegraflash-macos/helm-flash doctor
./tools/tegraflash-macos/helm-flash prepare
```

See [the Alpine guide](os/alpine/README.md) for image contents and build
options, and [the macOS recovery guide](tools/tegraflash-macos/README.md) for
the deployment boundary and current bridge limitations.

## Safety and security

`prepare`, `verify`, APX `list`, `inspect`, and `wait` do not write the Jetson.
Actual NVIDIA flash commands can erase QSPI and the selected external storage;
inspect their target and partition configuration before running them.

For bench bring-up, the image intentionally enables passwordless root on the
physical debug UART. Dropbear SSH is key-only and has no usable remote login
unless an authorized key is injected at build time. Remove the UART bypass and
provision unique credentials before deploying outside a controlled lab.
