# Pinned T234 loader and build helpers

This directory contains the six source files Helm uses from
`diodeinc/t234-bootkit`, revision
`5cedc336c859a2561644475aef057feaf41e73c0`:

| File | Purpose |
| --- | --- |
| `macos-rcm/Makefile` | Native loader build against system libusb |
| `macos-rcm/src/jetson_rcm.c` | T234 recovery USB transfer |
| `macos-rcm/scripts/build-bch.py` | Boot component header construction |
| `macos-rcm/scripts/apply-bct-delta.py` | Fixed-layout BCT edits |
| `tools/import-nvidia-flash-index.py` | Flash-index catalog import |
| `tools/qspi-flash.py` | QSPI image inspection and assembly |

These files are byte-for-byte copies with their original executable modes.
`SHA256SUMS` records their contents before Helm's family patch is applied.
No upstream Git history, submodules, research data, or firmware binaries are
included. The Python helpers use the standard library; the native loader
requires a C compiler, `make`, `pkg-config`, and libusb.

From the repository root, run:

```sh
./tools/helm-macos/helm-macos bootstrap
./tools/helm-macos/helm-macos self-test
```

The wrapper checks the exact manifest file set and every source hash before
staging these files under `build/helm-macos/work/t234-bootkit`. It then applies
`tools/helm-macos/t234-bootkit-orin-family.patch` to that copy and builds the
loader. Edit or update the source pin and manifest together when intentionally
upgrading the dependency. The checks detect unexpected local changes; they
are not an independent signature of the repository's contents.

The upstream helper CLIs retain their original defaults and release-specific
examples. Use Helm's wrapper for the R39.2 profiles, firmware digest checks,
and guarded installation flow. In particular, `qspi-flash.py --execute` is
not Helm's target-side installer and should not be used as the Helm flashing
procedure. Compiling this source does not generate the required firmware seed
or qualify a board.

## Provenance and licensing

The upstream `macos-rcm/README.md` at the pinned revision describes the USB
transfer sequence as recovered from NVIDIA's `tegraflash_impl_t234.py` and
symbols in the unstripped R35.6.1 `tegrarcm_v2`. This records upstream's
description; it does not claim a clean-room implementation.

The upstream snapshot provides no root license and these six files have no
copyright or SPDX notices. No license has been inferred or assigned here.
See [component licensing](../../../../THIRD_PARTY.md) for the source and
firmware distribution boundaries.
