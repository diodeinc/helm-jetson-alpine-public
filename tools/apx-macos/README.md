# Helm APX tools for macOS

`apxctl` is a small native macOS utility for detecting and inspecting NVIDIA
Tegra devices in USB recovery (APX/RCM) mode. It is built against upstream
`libusb` and does not require a Linux VM, Rosetta, or a kernel extension.

## Build

```sh
brew install cmake libusb pkg-config
./tools/apx-macos/build.sh
```

The release binary and static transport library are installed under
`build/macos-tools/` by default. Override the destinations with
`HELM_APX_BUILD_DIR` and `HELM_APX_INSTALL_DIR`.

## Use

```sh
build/macos-tools/bin/apxctl list
build/macos-tools/bin/apxctl inspect --pid 0x7423
build/macos-tools/bin/apxctl wait --pid 0x7423 --timeout 30
build/macos-tools/bin/apxctl inspect --pid 0x7423 --json
```

Those commands only read USB descriptors. They do not consume BootROM data,
load code, reset the board, or write storage.

Known recovery product IDs are resolved to NVIDIA module SKUs. In particular,
`0955:7423` is reported as Jetson Orin NX 8GB (`P3767-0001`).

The chip UID is BootROM's first 16-byte bulk-IN greeting. Reading it changes
the protocol position, so it is deliberately guarded:

```sh
build/macos-tools/bin/apxctl cid --pid 0x7423 --consume-greeting
```

After that command, re-enter recovery mode before running another RCM client.

## NVIDIA tool boundary

NVIDIA's current Orin host package includes `tegraflash_impl_t234.py` and
`tegrasign_v3.py`, but its T234 implementations of `tegrarcm_v2`,
`tegrahost_v2`, `tegraparser_v2`, and `tegrabct_v2` are prebuilt 32-bit Linux
ELF executables. The public `NVIDIA/tegrarcm` source predates T234. Therefore
`apxctl` provides the native macOS USB transport and safe diagnostics, but it
does not claim to be a drop-in implementation of NVIDIA's closed Orin signing,
BCT generation, and flash protocol.
