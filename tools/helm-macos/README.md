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
- `r39.2-rcm-blob.xml`: fixed R39.2 recovery component order for Helm and
  P3767-0001
- `profile-data/r39.2`: reconstructed version payload whose digest is checked
  against NVIDIA's recorded QSPI flash index

The blob builder's layout is checked byte-for-byte against a retained
NVIDIA-generated two-entry fixture. The complete 17-entry R39.2 firmware-only
input set also reproduces NVIDIA's recorded 24,522,080-byte blob size. The
Alpine recovery kernel and Helm DTB extend it to 19 entries.

USB transport comes from the separately versioned
[`diodeinc/t234-bootkit`](https://github.com/diodeinc/t234-bootkit). The
validated source pin is `5cedc336c859a2561644475aef057feaf41e73c0`.

## Fixed-profile inputs

`HELM_R39_SIGNED_DIR` must point to the fixed, unfused `zerosbk` output for:

- Jetson Linux R39.2
- Orin NX 8 GB `P3767-0001`
- Diode Helm's boot-critical carrier configuration
- the exact MB1/memory/MB2 BCT and firmware set named by
  `r39.2-rcm-blob.xml`

The directory also supplies the five initial BootROM/MB1 transfer files:

```text
br_bct_BR.bct
mb1_t234_prod_aligned_sigheader.bin.encrypt
psc_bl1_t234_prod_aligned_sigheader.bin.encrypt
mb1_bct_MB1_sigheader.bct.encrypt
mem_rcm_sigheader.bct.encrypt
```

The same profile input must include `flash.idx`, `flash.xml.bin`, and
`flash.xml.tmp`; its parent directory must contain `secureflash.xml`. The
native importer verifies every recorded partition size and SHA-1 before
building the 64 MiB raw QSPI image.

These are target firmware/profile data. They are not executed on the Mac and
are excluded from Git. Substituting a nearby SKU, BSP version, fuse policy, or
carrier BCT is unsafe and unsupported. Generating this R39.2 signed catalog
from only a pristine BSP is the remaining profile-onboarding boundary; the
known catalog's runtime use is fully native macOS.

## Commands

```sh
brew install apko cmake cpio dtc libarchive libusb pkg-config zstd

./tools/helm-macos/helm-macos bootstrap
./tools/helm-macos/helm-macos build

HELM_R39_SIGNED_DIR=/absolute/path/to/r39.2-signed \
  ./tools/helm-macos/helm-macos qspi

HELM_R39_SIGNED_DIR=/absolute/path/to/r39.2-signed \
  ./tools/helm-macos/helm-macos bundle

./tools/helm-macos/helm-macos probe
./tools/helm-macos/helm-macos verify
./tools/helm-macos/helm-macos boot
```

`probe` is read-only. `verify` only reads local files. `boot` consumes the
BootROM RCM session and boots Linux in RAM, but does not send a persistent
flash command or write target storage.

After recovery reaches the debug UART:

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
to the exactly confirmed whole namespace. QSPI installation additionally
requires the exact fixed profile, validates MTD geometry and payload integrity,
creates a complete backup on the mounted NVMe, and checks a full readback.
The QSPI catalog and raw image are structurally verified; the first physical
write and cold-boot qualification are still pending.
