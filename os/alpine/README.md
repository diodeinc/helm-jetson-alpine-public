# Helm Alpine Linux

This builds a small Alpine Linux 3.24 aarch64 system for the Diode Helm
carrier and NVIDIA Jetson Orin NX/Nano modules. It combines Alpine userspace
with NVIDIA's Jetson Linux 39.2 kernel, out-of-tree modules, firmware, and
per-SKU device trees.

The build runs natively on Apple silicon through Docker Desktop. It does not
flash, reset, or otherwise communicate with a Helm connected in recovery mode.

## What is included

- OpenRC, DHCP, chrony, key-only Dropbear SSH, eudev, and a serial console
- NVIDIA 6.8.12-tegra kernel, modules, firmware, and an NVMe-capable initramfs
- C4 x4 NVMe and the module's C8 PCIe Realtek GbE PHY (`r8168`)
- USB2 recovery/device mode plus both Helm USB 3 host ports
- PWM fan and tachometer support through the kernel thermal framework
- green heartbeat and orange disk-activity/panic GPIO LEDs
- I2C, GPIO, USB, PCIe, NVMe, and network inspection tools
- full DTBs for every P3767 SKU plus a firmware-selected dynamic overlay

This deliberately omits a desktop, container runtime, CUDA, and NVIDIA's
glibc-only graphics/compute userland. Alpine is a strong fit for a small,
appliance-style OS; use an NVIDIA Ubuntu container or a separate glibc layer
later if CUDA is required.

## Build on macOS

Requirements:

- Apple-silicon Mac
- Docker Desktop with its Linux engine running
- `Jetson_Linux_R39.2.0_aarch64.tbz2` from NVIDIA

Place the BSP archive at
`build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2`, or provide another path:

```sh
HELM_L4T_ARCHIVE=/absolute/path/Jetson_Linux_R39.2.0_aarch64.tbz2 \
  ./os/alpine/build.sh
```

To enable remote root login, inject an SSH public-key file at build time:

```sh
HELM_SSH_AUTHORIZED_KEYS_FILE="$HOME/.ssh/authorized_keys" \
  ./os/alpine/build.sh
```

The debug UART is intentionally a passwordless root bring-up console. SSH is
key-only. A build without an injected key therefore has no remote login.

Set `HELM_ROOTFS_SIZE` if the default 2 GiB APP filesystem is too small:

```sh
HELM_ROOTFS_SIZE=8G ./os/alpine/build.sh
```

## Artifacts

Build products are written to `build/helm-alpine/out/`:

- `*-rootfs.ext4.zst`: compressed APP-partition filesystem image
- `*-rootfs.tar.zst`: root filesystem archive for image assembly
- `*-boot.tar.zst`: kernel, initramfs, extlinux menu, overlay, and full DTBs
- `SHA256SUMS`: checksums for the three distributable archives/images
- `packages.txt`, `helm-release`, and `ext4-info.txt`: build manifests

The uncompressed `*.ext4` is retained for local inspection and for conversion
to NVIDIA's sparse APP image format. It is a partition image, not a complete
NVMe disk image.

## Boot and deployment boundary

The attached recovery device reports NVIDIA USB ID `0955:7423`. NVIDIA's R39.2
recovery table identifies that as Orin NX 8GB (`P3767-0001`), so this build
defaults to its exact full Helm DTB. The extlinux menu also includes full DTBs
for `P3767-0000`, `-0003`, `-0004`, and `-0005`; `helm-auto` retains firmware
SKU selection and applies the carrier overlay dynamically if the image is
reused on another module.

Jetson's QSPI/UEFI, MB1 configuration, partition layout, and signed recovery
payload still use NVIDIA's official tooling. This Alpine builder never writes
storage. On Apple-silicon macOS,
`tools/tegraflash-macos/helm-flash prepare` can use the BSP to assemble the
sparse APP image and signed recovery payloads without attaching USB. The live
write path remains an explicit, experimental NVIDIA flash invocation through
the native bridge and is intentionally not automated; see the macOS recovery
guide before using it. Do not write `*-rootfs.ext4` to an entire NVMe device;
it is only the APP filesystem.

No storage is written unless a separate, explicit flashing command is run.

## First boot

Connect the onboard MCP2221 debug USB serial adapter at 115200 8N1. After boot:

```sh
helm-info
helm-led green heartbeat
helm-led orange activity
cat /run/helm-peripherals
```

The default hostname is `helm`. `dhcpcd` requests an address on available
interfaces, the green LED heartbeats, and the orange LED tracks disk activity.
