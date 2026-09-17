# Component licensing and distribution

This inventory describes licensing boundaries. It does not grant rights to
third-party material or certify a generated firmware image for redistribution.

## Source in this repository

- `os/alpine/device-tree/helm-p3768.dtso` is marked `GPL-2.0-only`; retain that
  license designation and apply its terms when distributing the overlay or
  derived device trees. The [GPL version 2 text](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)
  is available from the Free Software Foundation.
- `tools/helm-macos/vendor/t234-bootkit/` contains six source files from
  `diodeinc/t234-bootkit` at `5cedc336c859a2561644475aef057feaf41e73c0`.
  `tools/helm-macos/t234-bootkit-orin-family.patch` is applied during builds.
  The upstream snapshot has no license file or per-file license notices;
  copying it here does not establish a source license. Its
  [provenance note](tools/helm-macos/vendor/t234-bootkit/README.md) records the
  upstream description of the USB implementation. Resolve the source license
  and preserve applicable attribution before distributing source or binaries.
- `tools/helm-macos/profile-data/` retains NVIDIA-derived version metadata.
  Review its provenance and applicable terms as part of a source release.
- Existing copyright and license notices take precedence for the files they
  cover. A project license for Diode-owned code does not relicense third-party
  components, firmware, or branding.

## Inputs and generated artifacts

The build uses Alpine packages, NVIDIA Jetson Linux R39.2 kernel/modules,
firmware, device trees, bootloader components, and a qualified seed catalog.
These inputs are not distributed as part of this source tree. Generated
root filesystems, QSPI images, recovery bundles, and browser payloads contain
components from those inputs and need a separate distribution review.

Before distributing an image or hosting browser payloads:

1. Retain the exact licenses and notices supplied with each input version,
   including the R39.2 BSP, bootloader package, and qualified seed. Resolve
   permission for any transformed or rebuilt firmware component.
2. Inventory the resolved Alpine packages and kernel/modules. Provide the
   corresponding source and other materials required by their licenses using
   a distribution method that satisfies those terms.
3. Ship required license texts, copyright notices, and attribution with the
   artifacts. Record how recipients obtain any required source.
4. Record the source commit, input and output hashes, package versions, and
   qualification results for the exact artifact being released.

Passing checksum or hardware tests does not complete this licensing review.
