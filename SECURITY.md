# Security policy

Helm Jetson Alpine is experimental bring-up software for a controlled bench.
There are no production or long-term-support releases. Security fixes are
developed on `main`; older commits and locally built images do not receive
automatic updates. Rebuild and reinstall an image to apply a target-side fix.

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** option when
private vulnerability reporting is enabled. The form is at
<https://github.com/diodeinc/helm-jetson-alpine-public/security/advisories/new>.
If the form is unavailable, contact a repository maintainer through an
existing private channel. Do not post exploit details, credentials, private
keys, raw device dumps, or identifying serial numbers in public issues.

Include the source commit, affected module SKU, host/browser versions, a
minimal reproduction, and the impact you observed. Redact unrelated device
identifiers and credentials. No response-time or hardware-replacement
commitment is made for this developer preview.

## Device access and destructive operations

- The installed image grants passwordless root on the physical debug UART.
  Recovery grants root over UART and USB CDC. Physical access is trusted;
  these images do not defend against a person with access to those ports.
- Dropbear uses key-only authentication. A default build has no authorized
  SSH keys. If injecting keys, use keys you control and restrict network access
  to the device. Remove the debug login bypass and provision appropriate
  credentials before deployment outside a controlled bench.
- Installation destroys the selected NVMe's data and rewrites QSPI firmware.
  Back up needed data before starting, connect exactly one board, and keep
  power connected throughout the operation. An interrupted install may need
  recovery. Confirmation prompts do not make interrupted writes recoverable.
- Use the fresh RAM recovery environment for installation. The NVMe guard
  detects direct mounts; it is not a comprehensive detector of swap,
  device-mapper, LVM, or RAID users of a disk.
- P3767-0001 has completed physical provisioning and cold boot. The other
  four module SKUs have structural validation only. Browser support remains
  a developer preview pending broader host/browser and exact-SKU testing.

## Build and release trust

Use reviewed source and firmware inputs from their authorized suppliers.
The native and browser workflows verify recovery-bundle hashes before boot.
The browser fetches both its catalog and payloads from the same origin, so
control of that origin is control of the firmware the user is offered.
Checksums alone do not establish who published an artifact.

Keep build and deployment credentials restricted, use reviewed immutable
release artifacts, and record the source commit and input hashes for an image.
The supplied BSP archive currently has no pinned digest in the builder; verify
it against a trusted supplier digest before building release artifacts.
Manually supplied `helm-install --archive` files must come from a trusted
source; archive integrity checks are not publisher authentication.

## Repository maintenance

Require reviewed pull requests and passing CI on `main`. Keep Actions tokens
read-only, checkout actions pinned, and workflow credential persistence off.
Enable GitHub secret scanning and push protection, and private vulnerability
reporting when available. The local/CI secret scan complements those settings.

If a credential is exposed, revoke or rotate it before removing it from source.
Inspect the full Git history, Actions logs, and release artifacts; editing the
latest file does not remove historical copies. See
[the publication checklist](docs/public-release.md).
