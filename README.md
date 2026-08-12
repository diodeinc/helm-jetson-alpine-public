# Helm Jetson Alpine

[![Native macOS CI](https://github.com/diodeinc/helm-jetson-alpine/actions/workflows/ci.yml/badge.svg)](https://github.com/diodeinc/helm-jetson-alpine/actions/workflows/ci.yml)

A small Alpine Linux appliance OS for the Diode Helm carrier and the complete
Jetson Orin NX/Nano P3767 module family supported by Helm. The image combines
Alpine 3.24 with the Jetson Linux R39.2 kernel, modules, firmware, and
Helm-specific device trees.

Alpine is a good fit for Helm: OpenRC keeps the boot path small, the base image
is easy to audit, and the target needs no desktop or Ubuntu package layer.
CUDA and NVIDIA's glibc graphics/compute stack are intentionally not part of
the base OS.

## Supported modules

| Native profile | Module | Recovery USB |
| --- | --- | --- |
| `helm-orin-nx-16gb-r39.2` | Orin NX 16 GB, P3767-0000 | `0955:7323` |
| `helm-orin-nx-8gb-r39.2` | Orin NX 8 GB, P3767-0001 | `0955:7423` |
| `helm-orin-nano-8gb-r39.2` | Orin Nano 8 GB, P3767-0003 | `0955:7523` |
| `helm-orin-nano-4gb-r39.2` | Orin Nano 4 GB, P3767-0004 | `0955:7623` |
| `helm-orin-nano-8gb-sd-r39.2` | Orin Nano 8 GB dev-kit/SD, P3767-0005 | `0955:7523` |

The original Jetson Nano (T210/P3448) and Xavier NX (T194/P3668) are not in
scope: they use different SoCs, power/pin requirements, and boot stacks from
the P3767 modules for which Helm was designed.

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
- Native Python tooling extracts the selected SKU from NVIDIA's pinned R39.2
  multi-spec bootloader capsule, derives its recovery components, verifies
  every catalog payload, and produces its fixed 64 MiB raw QSPI image.
- Recovery PID matching and target-side module EEPROM matching prevent a
  selected profile from being used on a different module SKU.

All five per-SKU catalogs, QSPI images, and recovery bundles are structurally
verified on macOS. P3767-0001 additionally reproduces the qualified recovery
MB1 BCT, memory BCT, and DCE component byte-for-byte. Hardware RAM-boot, QSPI
write, and cold-boot qualification are still pending, so treat this as
bring-up software and do not use it on irreplaceable hardware. Firmware and
DRAM BCTs are selected by exact NVIDIA module spec; they are never borrowed
from a nearby SKU.

## Native macOS build

Requirements:

- Apple-silicon macOS
- NVIDIA `Jetson_Linux_R39.2.0_aarch64.tbz2`
- access to [`diodeinc/t234-bootkit`](https://github.com/diodeinc/t234-bootkit)
- the qualified, unfused P3767-0001 R39.2 signed seed catalog

Install host tools:

```sh
brew install apko cmake cpio dtc libarchive libusb lz4 pkg-config shellcheck zstd
```

Place the BSP at `build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2`, then:

```sh
./tools/helm-macos/helm-macos bootstrap
./tools/helm-macos/helm-macos build
./tools/helm-macos/helm-macos profiles
```

The BSP is consumed as a data archive. Its Linux host programs are never
executed. The family builder automatically downloads and hash-checks NVIDIA's
official R39.2 bootloader package; its multi-spec capsule remains opaque target
firmware and is not executed on the Mac.

Point the family builder at the qualified 0001 seed, then build one selected
profile or the complete five-profile matrix:

```sh
export HELM_R39_SEED_SIGNED_DIR=/absolute/path/to/qualified-p3767-0001/signed

./tools/helm-macos/helm-macos \
  --profile helm-orin-nano-4gb-r39.2 family-all

./tools/helm-macos/helm-macos family-matrix
```

The seed directory and generated catalogs are profile data, not executable
Linux tooling. They are deliberately excluded from Git. Existing exact-SKU
catalogs can still be supplied directly through `HELM_R39_SIGNED_DIR`. See
[`tools/helm-macos/README.md`](tools/helm-macos/README.md) for its exact scope
and the bundle contract.

## RAM boot and target-side install

With one selected P3767 recovery device connected directly to the Mac:

```sh
profile=helm-orin-nx-8gb-r39.2
./tools/helm-macos/helm-macos --profile "$profile" probe
./tools/helm-macos/helm-macos --profile "$profile" verify
./tools/helm-macos/helm-macos --profile "$profile" boot
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

The last command erases and rewrites all 64 MiB of QSPI. It refuses a module
EEPROM that does not match the bundled P3767 SKU, the wrong device geometry, a
non-block-backed backup path, insufficient backup space, an existing backup
file, a bad image checksum, or incorrect confirmation. It backs up QSPI before
erasing and compares a full-device readback afterward. This destructive path
is implemented and structurally verified, but not yet qualified on a physical
Helm.

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
