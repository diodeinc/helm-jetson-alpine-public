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
- The install archive retains NVIDIA's pinned T23x UEFI OS launcher; recovery
  writes it to the NVMe FAT ESP fallback path and installs the Helm extlinux
  menu, kernel, initramfs, and DTBs on the `APP` root partition. The installer
  validates the family bundle's P3767 `BOARD_SKU` and selects its matching
  premerged Helm DTB as the installed default.
- The separate `t234-bootkit` native libusb loader sends the six-file RCM
  bundle from macOS. The host-side `boot` operation is volatile and does not
  write QSPI or NVMe.
- Native Python tooling extracts the selected SKU from NVIDIA's pinned R39.2
  multi-spec bootloader capsule, derives its recovery components, verifies
  every catalog payload, and produces its fixed 64 MiB raw QSPI image.
- Recovery PID matching and target-side module EEPROM matching prevent a
  selected profile from being used on a different module SKU.
- A dependency-free browser flasher verifies and RAM-boots the same bundles
  through WebUSB, then drives the guarded recovery preflight/install protocol
  through Web Serial. It is a developer preview pending physical browser
  qualification; the native macOS path remains the qualified path.

All five per-SKU catalogs, QSPI images, and recovery bundles are structurally
verified on macOS. P3767-0001 additionally reproduces the qualified recovery
MB1 BCT, memory BCT, and DCE component byte-for-byte. That SKU has completed a
physical RAM boot, full QSPI write/readback, NVMe installation, and cold boot
on Helm. A second P3767-0001 also completed the complete guarded `provision`
command from macOS with only power and recovery USB connected: recovery CDC,
module/geometry preflight, NVMe installation, QSPI backup, write, full
readback, and the session-bound success marker all passed without UART. The
other four SKUs remain structurally verified but not physically qualified.
Treat this as bring-up software and do not use it on irreplaceable hardware.
Firmware and DRAM BCTs are selected by exact NVIDIA module spec; they are
never borrowed from a nearby SKU.

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
executed. The builder automatically downloads and hash-checks NVIDIA's official
R39.2 bootloader package; it extracts the T23x UEFI launcher and multi-spec
capsule as data, and neither is executed on the Mac.

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

For the customer path, connect one supported Helm directly to the Mac through
its recovery USB port, put it in force recovery, and run one guarded command:

```sh
./helm
```

The guided command requires a real controlling terminal and read-only probes
exactly one supported APX device. It selects the profile automatically for the
unique recovery PIDs. P3767-0003 and P3767-0005 share PID `0955:7523`, so that
case never defaults: it requires the exact profile printed on the module. The
target NVMe prompt defaults to `/dev/nvme0n1`.

The selected bundle is verified before a volatile RAM boot. The same USB port
then re-enumerates as recovery CDC; the host requires the macOS physical USB
location recorded in APX mode plus VID:PID `0955:7020`, product
`Helm Alpine Recovery Console`, and serial `helm-recovery`, rejecting MCP2221,
other serial devices, and a recovery gadget on another port. Recovery displays
read-only module, NVMe, QSPI-geometry, and payload inspection first. It binds
the observed NVMe model, serial, WWID, size, device path, and profile to a
SHA-256 inventory fingerprint and requires an exact typed phrase containing
its 12-hex prefix before sending the full fingerprint with the install command.
Cancellation at this point has written no persistent storage.

After confirmation, the target repeats every preflight and refuses if the NVMe
inventory changed. It installs Alpine, saves the complete pre-write QSPI backup
on the new root filesystem, writes QSPI, verifies a full readback, syncs,
unmounts, and emits a session-bound success marker. A timeout, disconnect,
target error, or mismatched marker is a failure, never inferred success. Leave
power connected unless the command prints verified success.

Guided mode fails closed if the NVMe does not expose those stable identity
fields. The fully explicit form remains available for noninteractive automation:

```sh
profile=helm-orin-nx-8gb-r39.2
./tools/helm-macos/helm-macos --profile "$profile" provision \
  --device /dev/nvme0n1 --confirm-device /dev/nvme0n1 \
  --confirm-profile "$profile"
```

## Browser flasher preview

The tailnet-only browser prototype is available in current desktop Chrome or
Chromium at
<https://preview.example.invalid/helm>. Connect exactly one Helm,
keep it powered, and use its recovery USB port. It is still one physical cable,
but the browser asks for BootROM WebUSB access, MB1/PSC WebUSB access after APX
re-enumerates, and finally the recovery Web Serial port. The first chooser also
detects the module profile for the three unambiguous APX product IDs and reuses
that authorization for the BootROM transfer.

The page downloads only the selected exact-SKU bundle, verifies every file
against the same-origin catalog and embedded SHA256SUMS, and performs a
volatile RAM boot. It then runs the same target-side read-only module/NVMe/QSPI
preflight and requires the same inventory-bound exact phrase before any
persistent write. P3767-0003 and P3767-0005 still require an explicit module
choice because both use PID `0955:7523`.

The complete browser flow has been exercised physically on P3767-0001, including
the two-grant T234 BootROM-to-MB1 handoff and verified NVMe/QSPI provisioning.
It remains a developer preview until it has broader Chrome/macOS and exact-SKU
qualification. A public rollout also needs its own trusted origin (for example
`flash.diode.com`); the current host is private to the tailnet, and the raw
`192.0.2.1` URL is not a valid WebUSB HTTPS origin.

For manual recovery or diagnosis, the underlying non-writing steps remain:

```sh
profile=helm-orin-nx-8gb-r39.2
./tools/helm-macos/helm-macos --profile "$profile" probe
./tools/helm-macos/helm-macos --profile "$profile" verify
./tools/helm-macos/helm-macos --profile "$profile" boot
```

`boot` transfers the recovery environment into RAM and then the same recovery
USB port re-enumerates as a CDC serial console plus NCM network interface. Open
the new `/dev/cu.usbmodem*` at 115200 baud (the onboard debug UART remains an
optional bring-up fallback), then inspect the device and explicitly install.
Plain `boot` never invokes an installer and never writes NVMe or QSPI:

```sh
helm-install inspect
helm-install install --device /dev/nvme0n1 --confirm /dev/nvme0n1
```

The second command destroys that NVMe. It refuses non-NVMe paths, mounted
targets, devices below 2 GiB, and confirmation text that does not exactly
match the selected whole disk. It creates GPT entry 1 as the ext4 `APP` /
`HELM_ROOT` filesystem and GPT entry 2 as a 64 MiB FAT `esp` / `HELM_ESP`,
extracts the verified OS archive, and installs the UEFI fallback launcher. It
does not touch QSPI.

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
erasing and compares a full-device readback afterward. The combined
`provision` path has passed this destructive backup/write/readback sequence on
a physical P3767-0001 Helm without UART.

## Repository layout

- `helm`: zero-argument guided provisioning entry point
- `os/alpine`: native image build, OpenRC system, recovery initramfs, installer,
  Helm device tree, and peripheral helpers
- `tools/apx-macos`: read-only native APX discovery and inspection
- `tools/helm-macos`: native build/bundle/RCM/QSPI orchestration and owned
  image-format builders
- `tools/helm-web`: WebUSB RCM transport, guarded Web Serial protocol, static
  flasher, deterministic site packager, and isolated static server

See [`os/alpine/README.md`](os/alpine/README.md) for image contents and first
boot behavior. See the
[`P3767-0001 bring-up record`](docs/p3767-0001-bringup.md) for measured USB,
Ethernet, PCIe/NVMe, fan, and LED results from the physical Helm board.

## Security

The bring-up image enables passwordless root on the physical debug UART.
Dropbear is key-only and has no usable remote login unless an authorized key
is injected at build time. Remove the UART bypass and provision unique
credentials before deploying outside a controlled bench.
