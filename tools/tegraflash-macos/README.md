# Helm TegraFlash for macOS

This directory makes NVIDIA's Jetson Linux R39.2 recovery stack usable from
Apple-silicon macOS. It combines two pieces:

- `helm-apx-bridge`, a native arm64 Mach-O USB/IP server built from Rust,
  `libusb`, and the MIT-licensed `jiegec/usbip` source
- a pinned Ubuntu arm64 container that runs NVIDIA's unchanged T234 Python and
  Linux host utilities through Docker Desktop's built-in CPU emulation

The USB path is not emulated by an ad-hoc protocol shim. The native process
owns the physical APX interface and Docker Desktop's supported USB/IP path
presents it to NVIDIA's tools as a normal Linux USB device. The bridge exports
only NVIDIA vendor ID `0955` by default and scans for matching recovery
devices.

## Build and inspect

```sh
./tools/tegraflash-macos/helm-flash build
./tools/tegraflash-macos/helm-flash doctor
```

The native binaries are installed under `build/macos-tools/bin`. The recovery
container is tagged `helm-tegraflash-r39.2:macos`.

## Generate a flash payload without touching Helm

Build the Alpine filesystem first, then prepare NVIDIA's sparse APP image and
signed recovery payloads:

```sh
./os/alpine/build.sh
./tools/tegraflash-macos/helm-flash prepare
```

`prepare` deliberately does not start the USB bridge. It runs `flash.sh
--no-flash` with the offline P3767-0001 board specification and verifies the
result. The board remains in its existing recovery state and QSPI/NVMe are not
written.

## USB bridge

For a descriptor-only bridge test, start:

```sh
./tools/tegraflash-macos/helm-flash bridge
```

Then follow Docker's USB/IP procedure from another terminal. Merely listing or
attaching the device reads descriptors. Running `tegrarcm_v2 --uid` consumes
BootROM's initial recovery greeting, and running the generated flash command
writes storage; those operations are intentionally not automated here yet.

T234 resets and re-enumerates during recovery. Hot replacement on the same
macOS USB path is not yet reliable: stop the bridge, detach the Docker USB/IP
client, restart the bridge, and attach again after a reset. If BootROM no longer
answers its special identity descriptor during a retry, restart the native
binary with the identity captured before the reset:

```sh
build/macos-tools/bin/helm-apx-bridge \
  --vid 0x0955 --serial '<captured BootROM identity>'
```

Treat that identity as device-specific diagnostic data and do not commit it.

The dependency is pinned to `jiegec/usbip` commit
`5561aa4abf372f5d3eb05c65b1474a9489b735f8` for reproducibility.
