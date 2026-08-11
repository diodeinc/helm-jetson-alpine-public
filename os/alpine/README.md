# Helm Alpine Linux

This directory builds Alpine Linux 3.24 for the Diode Helm carrier and NVIDIA
Jetson Orin NX/Nano modules. The primary profile is Orin NX 8 GB
(`P3767-0001`) with the Jetson Linux R39.2 6.8.12-tegra kernel.

The build runs directly on Apple-silicon macOS. It does not start a container
or VM, run an NVIDIA host executable, access USB, or write target storage.

## Included system

- OpenRC, DHCP, chrony, key-only Dropbear, eudev, and a debug serial console
- NVIDIA kernel, in-tree/out-of-tree modules, and target firmware
- NVMe-capable normal and recovery initramfs images
- C4 x4 NVMe and C8 PCIe Realtek GbE (`r8168`)
- USB2 recovery/device mode and both Helm USB 3 host ports
- PWM fan/tachometer kernel support
- green heartbeat and orange disk-activity/panic GPIO LEDs
- I2C, GPIO, USB, PCIe, NVMe, MTD, and network inspection tools
- full Helm DTBs for P3767 SKUs 0000, 0001, 0003, 0004, and 0005
- guarded target-side NVMe and QSPI installers

The image deliberately omits a desktop, container runtime, CUDA, and NVIDIA's
glibc graphics/compute userspace.

## Build

```sh
brew install apko cpio dtc libarchive zstd
mkdir -p build/downloads
# Place Jetson_Linux_R39.2.0_aarch64.tbz2 in build/downloads.
./os/alpine/build.sh
```

Use another BSP location when needed:

```sh
HELM_L4T_ARCHIVE=/absolute/path/Jetson_Linux_R39.2.0_aarch64.tbz2 \
  ./os/alpine/build.sh
```

To enable remote root login, inject an SSH public-key file:

```sh
HELM_SSH_AUTHORIZED_KEYS_FILE=/absolute/path/to/authorized_keys \
  ./os/alpine/build.sh
```

The debug UART remains a passwordless root bring-up console. SSH is key-only;
a build without an injected key has no remote login.

## Artifacts

The native outputs are under `build/helm-alpine-native/out/`:

- `*-rootfs.tar.zst`: root-owned Alpine filesystem installed onto NVMe
- `*-boot.tar.zst`: kernel, normal initramfs, extlinux menu, and Helm DTBs
- `*-initramfs.gz`: normal `HELM_ROOT` discovery and switch-root initramfs
- `*-recovery-initramfs.gz`: base RAM recovery with the embedded OS payload
- `*-recovery-boot.img`: base T234-compatible Android boot image for RCM
- `SHA256SUMS`, `packages.txt`, and `helm-release`: integrity and build manifests

There is intentionally no host-generated ext4 disk image. The RAM-booted
Jetson creates GPT/ext4 itself, which preserves Linux ownership and removes
the need for a privileged Linux image builder on the Mac.

## Recovery installers

Recovery boots to a serial shell without writing anything. Inspect first:

```sh
helm-install inspect
```

After installing an NVMe, run the destructive action with exact confirmation:

```sh
helm-install install --device /dev/nvme0n1 --confirm /dev/nvme0n1
```

The installer validates its embedded archive, refuses mounted/non-NVMe/small
targets, writes one GPT Linux partition, formats it as ext4 with label
`HELM_ROOT`, extracts Alpine, validates the kernel/init/extlinux files, syncs,
and unmounts. NVIDIA UEFI can load extlinux and the kernel directly from that
ext4 filesystem, so this layout needs no separate EFI System Partition.

`helm-install` never writes `/dev/mtd0` or QSPI. The native `helm-macos bundle`
step augments this base recovery image with a fixed 64 MiB QSPI image and the
separate `helm-qspi-install` command. After the NVMe install succeeds, mount
it so the QSPI backup lands on persistent storage:

```sh
mkdir -p /mnt/helm-root
mount /dev/nvme0n1p1 /mnt/helm-root
helm-qspi-install inspect
helm-qspi-install install --device /dev/mtd0 \
  --backup /mnt/helm-root/root/qspi-before.img \
  --confirm helm-orin-nx-8gb-r39.2
```

The QSPI installer checks the exact 64 MiB/64 KiB MTD geometry and image
checksum, requires an unused backup path on a mounted block filesystem, backs
up the complete device, uses `flashcp`, and verifies a full readback. It is a
destructive bring-up path whose first physical write and cold boot have not
yet been qualified.

## First boot

Connect the onboard MCP2221 debug UART at 115200 8N1. After boot:

```sh
helm-info
helm-led green heartbeat
helm-led orange activity
cat /run/helm-peripherals
```

The `helm-peripherals` OpenRC service regenerates module dependencies, loads
carrier drivers, initializes GPIO LED triggers, and records detected GPIO,
NVMe, LED, and thermal devices. The default hostname is `helm`; `dhcpcd`
requests an address on available interfaces.
