# Helm Alpine Linux

This directory builds Alpine Linux 3.24 for the Diode Helm carrier and all five
supported Jetson Orin NX/Nano P3767 modules: 0000, 0001, 0003, 0004, and 0005.
It uses the Jetson Linux R39.2 6.8.12-tegra kernel.

The build runs directly on Apple-silicon macOS. It does not start a container
or VM, run an NVIDIA host executable, access USB, or write target storage.

## Included system

- OpenRC, DHCP, chrony, key-only Dropbear, eudev, and a debug serial console
- NVIDIA kernel, in-tree/out-of-tree modules, and target firmware
- NVMe-capable normal and recovery initramfs images, including module EEPROM
  identification for guarded QSPI provisioning
- NVIDIA's pinned T23x UEFI OS launcher retained at
  `/usr/lib/helm/BOOTAA64.EFI` and installed to the FAT ESP fallback path
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
# Place NVIDIA's pinned nvidia-l4t-bootloader R39.2 arm64 package there too.
./os/alpine/build.sh
```

Use another BSP location when needed:

```sh
HELM_L4T_ARCHIVE=/absolute/path/Jetson_Linux_R39.2.0_aarch64.tbz2 \
HELM_R39_BOOTLOADER_DEB=/absolute/path/to/nvidia-l4t-bootloader_39.2.0_arm64.deb \
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

There is intentionally no host-generated disk image. The RAM-booted Jetson
creates its ext4 `APP` root and FAT ESP itself, which preserves Linux ownership
and removes the need for a privileged Linux image builder on the Mac.

## Recovery installers

Recovery boots to a serial shell without writing anything. Inspect first:

```sh
helm-install inspect
```

The native Mac wrapper can drive the complete guarded sequence over this same
recovery cable:

```sh
profile=helm-orin-nx-8gb-r39.2
./tools/helm-macos/helm-macos --profile "$profile" provision \
  --device /dev/nvme0n1 --confirm-device /dev/nvme0n1 \
  --confirm-profile "$profile"
```

It invokes `helm-provision` only after the RAM recovery CDC console answers a
session-token probe. `helm-provision` requires the exact bundle profile and
whole-device confirmation, preflights `helm-install` and `helm-qspi-install`
without writes, then performs NVMe installation followed by the backed-up and
readback-verified QSPI installation. It syncs and unmounts before printing its
token-bound success marker. The recovery init script never invokes this command
automatically, so plain RAM recovery remains non-writing.

Recovery switches USB2 pad 0 through the stable
`/sys/class/usb_role/*/role` class link and verifies `device` mode both before
and after binding the UDC. Its composite gadget identifies itself as
`0955:7020`, product `Helm Alpine Recovery Console`, serial `helm-recovery`.
The recovery image also carries and loads `pwm-tegra` followed by `pwm-fan`
before forcing the detected fan control to full speed.

After installing an NVMe, run the destructive action with exact confirmation:

```sh
helm-install install --device /dev/nvme0n1 --confirm /dev/nvme0n1
```

The installer validates its embedded archive and refuses mounted, non-NVMe, or
small targets. GPT entry 1 remains `APP` at `/dev/nvme0n1p1`, formatted ext4 as
`HELM_ROOT`; entry 2 is a physically earlier 64 MiB FAT EFI System Partition
named `esp` and labeled `HELM_ESP`. Alpine is extracted to `APP`, and the
verified launcher is copied to `/EFI/BOOT/BOOTAA64.EFI` on the ESP. Before any
partition is changed, the installer requires exactly one `BOARD_SKU` in the
family bundle's `/opt/helm/payload/helm-profile`, accepts only P3767 SKUs
`0000`, `0001`, `0003`, `0004`, and `0005`, and verifies that the matching
premerged Helm DTB is present. It then makes that SKU's explicit full-DTB
extlinux entry the installed default. The installer also resets stale NVIDIA
rootfs health state before unmounting.

`helm-install` never writes `/dev/mtd0` or QSPI. The native `helm-macos
family-bundle` step augments this shared base recovery image with the selected
module's fixed 64 MiB QSPI image, profile metadata, and the separate
`helm-qspi-install` command. After the NVMe install succeeds, mount it so the
QSPI backup lands on persistent storage:

```sh
mkdir -p /mnt/helm-root
mount /dev/nvme0n1p1 /mnt/helm-root
helm-qspi-install inspect
helm-qspi-install install --device /dev/mtd0 \
  --backup /mnt/helm-root/root/qspi-before.img \
  --confirm helm-orin-nx-8gb-r39.2
```

The confirmation value is the profile selected while bundling. The QSPI
installer reads the module EEPROM and refuses a P3767 SKU mismatch before it
checks the exact 64 MiB device size, the pinned kernel's 4 KiB MTD erase size,
and the image checksum. NVIDIA's separate 64 KiB QSPI layout stride remains in
the catalog for partition placement and BCT redundancy; it is not the
kernel-reported erase unit. The installer requires an unused backup path on a
mounted block filesystem, backs up the complete device, uses `flashcp` (which
reads the live MTD geometry), and verifies a full readback. Its catalogs are
structurally verified for all five module SKUs; P3767-0001 has also passed
physical QSPI write/readback, NVMe installation, and cold boot on Helm.

## First boot

Connect the onboard MCP2221 debug UART at 115200 8N1. After boot:

```sh
helm-info
helm-led demo
helm-led green heartbeat
helm-led orange activity
cat /run/helm-peripherals
```

The `helm-peripherals` OpenRC service regenerates module dependencies, loads
carrier drivers, starts the fan, initializes GPIO LED triggers, and records
detected GPIO, NVMe, LED, and thermal devices. `helm-boot-success` refreshes
NVIDIA's rootfs retry counter after the default runlevel is reached. The
installer selects the explicit full DTB matching the bundle's module SKU; an
overlay-only default is intentionally not used because R39 L4TLauncher does not
apply an extlinux overlay unless the entry also supplies an `FDT` path. Full
DTBs for the other supported SKUs remain available as manual recovery entries.
The default hostname is `helm`; `dhcpcd` requests an address on available
interfaces.
