# Native Helm recovery from macOS

This is the supported host workflow. Every host executable is a native arm64
Mach-O program or a portable script running on macOS. There is no Docker,
Linux VM, USB/IP bridge, Rosetta process, or NVIDIA Linux host utility in the
build, bundle, verification, or RCM transfer path.

The Linux boundary is on the Jetson: macOS sends a recovery OS into RAM, and
that OS uses normal target drivers and storage tools to install NVMe and QSPI.

## Components

- `helm-macos`: doctor/build/QSPI/bundle/probe/verify/boot orchestration
- `mkbootimg.py`: owned Android boot image v0 builder, matching NVIDIA's T234
  addresses, page layout, and SHA-1 image ID
- `build-rcm-blob.py`: owned T234 blob builder with type mapping, component
  validation, record layout, and SHA-512 header generation
- `build-r39-family-inputs.py`: pinned multi-spec BUP parser, exact-SKU QSPI
  catalog builder, and native MB1/memory/DCE recovery-component derivation
- `r39.2-rcm-blob.xml`: fixed R39.2 recovery component order with per-profile
  aliases for module-specific firmware
- `profiles`: allowlisted P3767 SKU, recovery PID, DTB, BPMP, and DCE mappings
- `profile-data/r39.2/<profile>`: retained profile payloads whose digests are
  checked against NVIDIA's recorded QSPI flash index
- `t234-bootkit-orin-family.patch`: reproducible four-PID extension applied to
  a clean archive of the pinned native loader source

The blob builder's layout is checked byte-for-byte against a retained
NVIDIA-generated two-entry fixture. The complete 17-entry R39.2 firmware-only
input set also reproduces NVIDIA's recorded 24,522,080-byte blob size. The
Alpine recovery kernel and Helm DTB extend it to 19 entries.

USB transport comes from the separately versioned
[`diodeinc/t234-bootkit`](https://github.com/diodeinc/t234-bootkit). The
validated source pin is `5cedc336c859a2561644475aef057feaf41e73c0`; the
tracked family patch is applied only to a build-directory copy, so an arbitrary
or dirty sibling checkout cannot alter the loader that is compiled.

## Module profiles and exact-SKU inputs

`helm-macos profiles` lists the five supported configurations:

| Profile | P3767 SKU | USB PID |
| --- | --- | --- |
| `helm-orin-nx-16gb-r39.2` | 0000 | `0x7323` |
| `helm-orin-nx-8gb-r39.2` | 0001 | `0x7423` |
| `helm-orin-nano-8gb-r39.2` | 0003 | `0x7523` |
| `helm-orin-nano-4gb-r39.2` | 0004 | `0x7623` |
| `helm-orin-nano-8gb-sd-r39.2` | 0005 | `0x7523` |

The default is `helm-orin-nx-8gb-r39.2`. The family workflow combines two
fixed inputs:

- A qualified, unfused P3767-0001 R39.2 `zerosbk` catalog supplies the known
  boot-component policy headers, common recovery payloads, and fixed QSPI
  partition layout. Its used files are individually SHA-256 pinned.
- NVIDIA's official
  [`nvidia-l4t-bootloader` R39.2 package](https://repo.download.nvidia.com/jetson/som/pool/main/n/nvidia-l4t-bootloader/nvidia-l4t-bootloader_39.2.0-20260601141651_arm64.deb)
  supplies `TEGRA_BL_3767.Cap`, whose multi-spec BUP contains firmware, cold
  MB1/memory BCTs, GPTs, and version data for all five P3767 module specs. The
  package, capsule, and embedded BUP hashes are pinned.

`HELM_R39_SEED_SIGNED_DIR` selects the qualified seed. `prepare` downloads the
official package when absent, extracts it with native libarchive, and creates
an exact-SKU catalog under `build/helm-macos/family-inputs/`. The generator
never executes a file from either NVIDIA archive.

The directory also supplies the five initial BootROM/MB1 transfer files:

```text
br_bct_BR.bct
mb1_t234_prod_aligned_sigheader.bin.encrypt
psc_bl1_t234_prod_aligned_sigheader.bin.encrypt
mb1_bct_MB1_sigheader.bct.encrypt
mem_rcm_sigheader.bct.encrypt
```

For QSPI, the generator retains the qualified fixed 64 MiB partition layout,
replaces every populated partition with the selected BUP payload, and records
its exact size and SHA-1. For RCM, it selects that SKU's BPMP and DCE firmware,
converts the first memory slot into the recovery memory BCH, and applies the
qualified fixed-layout cold-to-recovery MB1-BCT transformation. Native `lz4`
decompresses the DCE source before its recovery BCH is rebuilt. On P3767-0001,
all three rebuilt recovery components must match the qualified seed
byte-for-byte or preparation fails.

Each derived directory carries `HELM_R39_FAMILY_INPUT_V1` identity metadata
and a complete `SHA256SUMS`. Bundle construction rechecks those files, the
selected BPMP/DCE names, and the SKU recorded in `qspi_bootblob_ver.txt` before
creating an image. Copying a 0001 catalog into another profile therefore fails
before output creation. Direct exact-SKU catalogs remain supported through
`HELM_R39_SIGNED_DIR`; those retain the stricter original requirement for
`flash.idx`, `flash.xml.bin`, `flash.xml.tmp`, and parent `secureflash.xml`.

These are target firmware/profile data. They are not executed on the Mac and
are excluded from Git. P3767-0003 and 0005 share a recovery PID, so the
target-side installer additionally reads the module EEPROM and requires the
exact SKU. Substituting a nearby SKU, BSP version, fuse policy, or carrier BCT
is unsafe and unsupported.

All five derived catalogs, fixed QSPI images, and native recovery bundles pass
local structural verification. P3767-0001 has also completed physical RAM
boot, full QSPI write/readback, NVMe installation, and cold boot; physical
qualification of the other four exact-SKU bundles remains a separate gate.

## Commands

```sh
brew install apko cmake cpio dtc libarchive libusb lz4 pkg-config zstd

./tools/helm-macos/helm-macos bootstrap
./tools/helm-macos/helm-macos build
./tools/helm-macos/helm-macos profiles

export HELM_R39_SEED_SIGNED_DIR=/absolute/path/to/qualified-p3767-0001/signed

./tools/helm-macos/helm-macos \
  --profile helm-orin-nx-8gb-r39.2 prepare
./tools/helm-macos/helm-macos \
  --profile helm-orin-nx-8gb-r39.2 family-bundle

# Build Alpine once and produce all five exact-SKU bundles.
./tools/helm-macos/helm-macos family-matrix

# Guided one-command customer provisioning.
./helm

./tools/helm-macos/helm-macos --profile helm-orin-nx-8gb-r39.2 probe
./tools/helm-macos/helm-macos --profile helm-orin-nx-8gb-r39.2 verify
./tools/helm-macos/helm-macos --profile helm-orin-nx-8gb-r39.2 boot

# Fully explicit noninteractive provisioning.
profile=helm-orin-nx-8gb-r39.2
./tools/helm-macos/helm-macos --profile "$profile" provision \
  --device /dev/nvme0n1 \
  --confirm-device /dev/nvme0n1 \
  --confirm-profile "$profile"
```

`./helm` is the guided path. It requires a controlling terminal, finds exactly
one supported APX device, and maps its recovery PID to a profile. PID `7523`
is shared by P3767-0003 and P3767-0005, so the tool requires one exact profile
instead of guessing. It prompts for the target NVMe namespace, defaulting to
`/dev/nvme0n1`, and rechecks the sole APX identity immediately before boot.

`probe` is read-only. `verify` only reads local files. `boot` consumes the
BootROM RCM session and boots Linux in RAM, but does not send a persistent
flash command or write target storage. `provision` is the deliberately
destructive, single-recovery-cable customer path: it first performs the same
exact-bundle verification and volatile boot, excludes every serial device that
existed before boot, and connects only when the new serial node's nearest IOKit
`IOUSBHostDevice` parent exactly matches `0955:7020`, product
`Helm Alpine Recovery Console`, and serial `helm-recovery`. Guided mode also
requires the physical macOS USB `locationID` recorded for APX before boot. A
newly connected MCP2221 or any other `usbmodem` is rejected; guided mode also
rejects an exact recovery gadget on another port. Its serial transport uses
Python's standard library; no `pyserial` or other package is required.

In guided mode, target-side `helm-provision preflight` first reports the module,
NVMe, QSPI geometry, and payload checks without writing. The host derives an
exact confirmation phrase from a SHA-256 fingerprint binding the selected
profile and NVMe path/model/serial/WWID/size; missing stable identity fields
fail closed. The typed phrase contains its 12-hex prefix; only after that phrase
matches does the host send `helm-provision install` with the full fingerprint.
The target repeats the preflight and refuses changed inventory before writing.
It then installs NVMe, mounts partition 1 for the mandatory full QSPI backup,
installs and readback-verifies QSPI, syncs, and unmounts. The host accepts only
a complete success line carrying its random session token and requested
profile/device. Timeout or disconnect after writes start leaves state unknown;
keep power connected and inspect the recovery console, using UART only if CDC
is unavailable.

After recovery re-enumerates the same cable as a CDC serial port, open the new
`/dev/cu.usbmodem*` at 115200 baud. The onboard debug UART is an optional
bring-up fallback. In that recovery shell:

```sh
helm-install inspect
helm-install install --device /dev/nvme0n1 --confirm /dev/nvme0n1
mkdir -p /mnt/helm-root
mount /dev/nvme0n1p1 /mnt/helm-root
helm-qspi-install inspect
helm-qspi-install install --device /dev/mtd0 \
  --backup /mnt/helm-root/root/qspi-before.img \
  --confirm helm-orin-nx-8gb-r39.2
```

The two `install` commands are destructive. NVMe installation is restricted
to the exactly confirmed whole namespace and selects the premerged Helm DTB
matching the family bundle's validated P3767 `BOARD_SKU`. QSPI installation
additionally requires the exact profile, validates the module EEPROM, MTD
geometry, and payload integrity, creates a complete backup on the mounted NVMe,
and checks a full readback. All five QSPI catalogs and raw images are
structurally verified. P3767-0001 has passed physical write/readback and cold
boot. A second P3767-0001 passed the complete guarded `provision` command with
recovery USB only and no UART, including QSPI backup, write, full readback, and
the host-verified session marker. The other four exact-SKU images have not yet
been physically qualified.
