# Helm Jetson Alpine

[![Native macOS CI](https://github.com/diodeinc/helm-jetson-alpine/actions/workflows/ci.yml/badge.svg)](https://github.com/diodeinc/helm-jetson-alpine/actions/workflows/ci.yml)

A small Alpine Linux appliance OS for the Diode Helm carrier and NVIDIA Jetson
Orin NX 8 GB (`P3767-0001`). The current image combines Alpine 3.24 with the
Jetson Linux R39.2 kernel, modules, firmware, and a Helm-specific device tree.

Alpine is a good fit for Helm: OpenRC keeps the boot path small, the base image
is easy to audit, and the target needs no desktop or Ubuntu package layer.
CUDA and NVIDIA's glibc graphics/compute stack are intentionally not part of
the base OS.

## Current status

The host path is native Apple-silicon macOS. It uses no Docker, Linux VM,
Rosetta, or NVIDIA Linux host executable.

- `apko` resolves the aarch64 Alpine filesystem directly on macOS.
- Native libarchive, `dtc`, `cpio`, zstd, and Python build the rootfs, device
  trees, initramfs, Android boot image, and T234 recovery blob.
- The recovery image RAM-boots on the Jetson and carries a guarded target-side
  NVMe installer, a guarded QSPI installer, and their verified payloads.
- The separate `t234-bootkit` native libusb loader sends the six-file RCM
  bundle from macOS. The host-side `boot` operation is volatile and does not
  write QSPI or NVMe.
- Native Python tooling imports NVIDIA's recorded R39.2 QSPI partition index,
  verifies every catalog payload, and produces the fixed 64 MiB raw image.

The R39.2 bundle is structurally verified but has not yet completed its first
hardware boot on this profile. Treat it as bring-up software. The QSPI writer
requires a complete backup, exact profile confirmation, and full-device
readback verification. Its first write and cold-boot qualification are still
pending, so do not use it on irreplaceable hardware.

## Native macOS build

Requirements:

- Apple-silicon macOS
- NVIDIA `Jetson_Linux_R39.2.0_aarch64.tbz2`
- access to [`diodeinc/t234-bootkit`](https://github.com/diodeinc/t234-bootkit)
- a fixed-profile R39.2 signed input directory for RCM bundle assembly

Install host tools:

```sh
brew install apko cmake cpio dtc libarchive libusb pkg-config shellcheck zstd
```

Place the BSP at `build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2`, then:

```sh
./tools/helm-macos/helm-macos bootstrap
./tools/helm-macos/helm-macos build
```

The BSP is consumed as a data archive. Its Linux host programs are never
executed. NVIDIA target firmware remains a required opaque input.

To assemble the fixed P3767-0001/Helm recovery bundle and its QSPI image:

```sh
HELM_R39_SIGNED_DIR=/absolute/path/to/r39.2-signed \
  ./tools/helm-macos/helm-macos bundle
```

The signed directory and its recorded flash index are profile catalog inputs,
not an executable toolchain. They are deliberately excluded from Git. See
[`tools/helm-macos/README.md`](tools/helm-macos/README.md) for its exact scope
and the bundle contract.

## RAM boot and target-side install

With one T234 recovery device connected directly to the Mac:

```sh
./tools/helm-macos/helm-macos probe
./tools/helm-macos/helm-macos verify
./tools/helm-macos/helm-macos boot
```

`boot` transfers the recovery environment into RAM. On the Helm debug UART,
first inspect the device and then explicitly install:

```sh
helm-install inspect
helm-install install --device /dev/nvme0n1 --confirm /dev/nvme0n1
```

The second command destroys that NVMe. It refuses non-NVMe paths, mounted
targets, devices below 2 GiB, and confirmation text that does not exactly
match the selected whole disk. It creates a GPT/ext4 `HELM_ROOT` filesystem
and extracts the verified OS archive. It does not touch QSPI.

Only after the NVMe install succeeds, mount it for the mandatory QSPI backup:

```sh
mkdir -p /mnt/helm-root
mount /dev/nvme0n1p1 /mnt/helm-root
helm-qspi-install inspect
helm-qspi-install install --device /dev/mtd0 \
  --backup /mnt/helm-root/root/qspi-before.img \
  --confirm helm-orin-nx-8gb-r39.2
```

The last command erases and rewrites all 64 MiB of QSPI. It refuses the wrong
device geometry, a non-block-backed backup path, insufficient backup space,
an existing backup file, a bad image checksum, or incorrect confirmation. It
backs up QSPI before erasing and compares a full-device readback afterward.
This destructive path is implemented and structurally verified, but not yet
qualified on the physical Helm.

## Repository layout

- `os/alpine`: native image build, OpenRC system, recovery initramfs, installer,
  Helm device tree, and peripheral helpers
- `tools/apx-macos`: read-only native APX discovery and inspection
- `tools/helm-macos`: native build/bundle/RCM/QSPI orchestration and owned
  image-format builders

See [`os/alpine/README.md`](os/alpine/README.md) for image contents and first
boot behavior.

## Security

The bring-up image enables passwordless root on the physical debug UART.
Dropbear is key-only and has no usable remote login unless an authorized key
is injected at build time. Remove the UART bypass and provision unique
credentials before deploying outside a controlled bench.
