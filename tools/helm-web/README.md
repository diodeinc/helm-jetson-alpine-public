# Helm browser flasher

This directory contains a dependency-free ES-module implementation of the
volatile T234 RCM boot used by Helm's pinned native macOS loader. It lets a
Chromium page load the Alpine recovery OS into RAM through WebUSB. The static
application then uses Web Serial to run the existing guarded read-only
preflight and, only after an exact typed phrase, the target-side NVMe/QSPI
provisioner.

This is a developer preview for controlled bench use. The complete browser
flow has been exercised physically on P3767-0001, including the two-grant
BootROM-to-MB1 handoff and verified NVMe/QSPI provisioning. Other SKUs and
broader browser/host combinations remain unqualified. Installation erases
NVMe and rewrites QSPI boot firmware.

Run locally using the instructions below, or serve the generated site from a
trusted HTTPS origin. Publishing this source does not publish a hosted flasher
or include its firmware bundles.

## Build and run the site

Building the recovery bundles requires Apple-silicon macOS, access to the
separate `diodeinc/t234-bootkit` repository, NVIDIA's R39.2 BSP, and the qualified
P3767-0001 signed seed catalog. The seed catalog and generated firmware bundles
are excluded from this repository; a public clone alone is not a complete
firmware build input set. Follow the [native build instructions](../helm-macos/README.md)
to supply those inputs.

Build all five native recovery bundles, then validate every bundle and prepare
the static site:

```sh
./tools/helm-macos/helm-macos family-matrix
tools/helm-web/prepare-site.py
node tools/helm-web/server.mjs --root build/helm-web/site
```

Open <http://127.0.0.1:3190/helm/> for local development. The packager requires the
exact five profile directories, exact file sets and PROFILE schemas, and a
valid SHA256SUMS entry for every payload. It rehashes the payloads before
generating `catalog.json`. The server binds only to localhost by default,
supports bounded range reads for the recovery blob, and supplies a strict CSP
and USB/serial Permissions Policy.

The customer flow is deliberately split into four visible stages:

1. Grant one supported BootROM APX device. Its PID selects the unique module
   profile when possible; shared PID `0x7523` requires an explicit SKU choice.
   Download the selected bundle and verify every digest.
2. Reuse that BootROM grant for stage one, then grant the re-enumerated
   MB1/PSC APX device and complete the volatile RAM boot.
3. Grant the re-enumerated `0955:7020` recovery console through Web Serial and
   run the target-side read-only preflight.
4. Review the NVMe inventory fingerprint, type the exact generated phrase,
   and explicitly start the persistent NVMe/QSPI operation.

It is one physical cable but three browser permission grants: BootROM WebUSB,
MB1/PSC WebUSB after re-enumeration, then recovery Web Serial. Detection does
not add a redundant BootROM chooser; the selected `USBDevice` is retained and
used for the first transfer. Nothing starts the
installer automatically. A timeout or disconnect after the install command is
sent is reported as unknown persistent state, never as success.

## Browser contract

WebUSB requires a supported Chromium browser and a secure context. Use HTTPS
for network access. Local development can use `http://localhost` or
`http://127.0.0.1`.
`navigator.usb.requestDevice()` must run from a customer click.

The static application makes APX selection the first awaited operation in the
prepare-button handler, then uses the selected PID to choose the catalog
profile before downloading and validating it. A shortened version is:

```js
import {
  T234WebUsbRcm,
  profilesByProductId,
  requestAnyApxDevice,
  validateRcmBundle,
} from "./tools/helm-web/index.js";

let bundle;
let bootromDevice;
let rcm;
let handoff;

prepareButton.addEventListener("click", async () => {
  // Must be the first await while the click still has user activation.
  bootromDevice = await requestAnyApxDevice(navigator.usb);
  const candidates = profilesByProductId(bootromDevice.productId);
  const profileId = candidates.length === 1
    ? candidates[0].id
    : await requireExplicit0003Or0005Selection();
  const files = await downloadCatalogBundle(profileId);
  bundle = await validateRcmBundle(files, {
    expectedProfileId: profileId,
    trustedChecksums: approvedDigests[profileId],
    onProgress: showHashProgress,
  });
});

bootButton.addEventListener("click", async () => {
  rcm = new T234WebUsbRcm();
  handoff = await rcm.bootrom(bootromDevice, bundle, {
    onProgress: showUsbProgress,
  });
});

continueButton.addEventListener("click", async () => {
  // Keep the chooser as the first awaited operation in this second click.
  const device = await rcm.requestBootloaderDevice(handoff);
  const result = await rcm.bootloader(device, bundle, handoff, {
    onProgress: showUsbProgress,
  });
  console.log(result.cid, result.banner);
});
```

The file input is normally a directory picker:

```html
<input id="bundleInput" type="file" webkitdirectory multiple>
```

Bundle validation is deliberately strict. One directory must contain exactly
the six non-empty wire artifacts below, plus `PROFILE` and `SHA256SUMS`.
Metadata is bound to the selected SKU and all seven covered files are hashed
incrementally, so the roughly 159 MiB blob is not copied into memory at once.
The local checksum file detects accidental corruption; it is not an
authenticity boundary unless the application also supplies trusted digests.
Fetched artifacts work too: wrap each response body in `new File([blob],
filename)` or provide an object with `name`, `size`, and `slice()`; a
`webkitRelativePath` property is not required.

`trustedChecksums` is a plain object (or `Map`) with exactly seven entries.
Each value is a lowercase 64-hex SHA-256 digest:

```js
const trustedChecksums = {
  "br_bct_BR.bct": "<64 lowercase hex>",
  "mb1_t234_prod_aligned_sigheader.bin.encrypt": "<64 lowercase hex>",
  "psc_bl1_t234_prod_aligned_sigheader.bin.encrypt": "<64 lowercase hex>",
  "mb1_bct_MB1_sigheader.bct.encrypt": "<64 lowercase hex>",
  "mem_rcm_sigheader.bct.encrypt": "<64 lowercase hex>",
  "blob.bin": "<64 lowercase hex>",
  "PROFILE": "<64 lowercase hex>",
};
```

Do not add `SHA256SUMS` to that object. The trusted values anchor the bundle;
the locally supplied checksum file must independently agree with them.

The included static application takes these trusted digests from
`catalog.json` on the same origin as the page. There is no independent release
signature verification. The checks detect corruption or a payload that differs
from the catalog; replacing both the catalog and payload can pass them. Trust
therefore depends on the reviewed build inputs and control of the HTTPS origin
and deployment credentials. A compromised frontend could also change the
verification or install flow.

## Public rollout prerequisites

- Record physical qualification for every advertised SKU and browser/host
  combination, including provisioning, cold boot, and recovery after failures.
- Use a dedicated trusted HTTPS origin, preserve the server's security headers,
  and restrict who can build and deploy releases.
- Publish reviewed release artifacts with recorded checksums and source
  revisions. Confirm redistribution rights for the NVIDIA inputs and generated
  firmware before making bundles public.
- Replace the bring-up image's bench console access and configure device
  credentials before using installed devices outside a controlled bench. See
  the [repository security notes](../../README.md#security).

## Exact transfer sequence

The implementation mirrors
`t234-bootkit` commit `5cedc336c859a2561644475aef057feaf41e73c0` with
`tools/helm-macos/t234-bootkit-orin-family.patch` applied:

1. Select one supported T234 APX PID, require it to match the validated bundle
   profile, and claim the first interface alternate with bulk IN and bulk OUT.
2. Read standard USB string descriptor 3, at most `0x82` bytes, for the
   BootROM CID.
3. Send these raw signed files to bulk OUT, in order, with writes no larger
   than `0x4000` bytes:
   - `br_bct_BR.bct`
   - `mb1_t234_prod_aligned_sigheader.bin.encrypt`
   - `psc_bl1_t234_prod_aligned_sigheader.bin.encrypt`
   - `mb1_bct_MB1_sigheader.bct.encrypt`
4. Close the handle and pause. T234 has no USB serial number, so Chromium may
   revoke the BootROM grant when the device re-enumerates.
5. From a second customer click, request exactly `0955:<profile PID>` again
   and select the re-enumerated MB1/PSC APX device.
6. Claim it, read exactly `0x44` bytes from bulk IN, and parse the 64-byte
   version followed by the little-endian last-boot-error value.
7. Close and reopen it, then send `mem_rcm_sigheader.bct.encrypt` and
   `blob.bin` raw to bulk OUT in that order.

No length prefix, command record, or per-image wire wrapper is added. Partial
bulk writes and reads are continued until complete. Every operation has a
bounded timeout, and a timeout closes the device as WebUSB's transfer methods
do not expose native cancellation.

## Supported modules

Five module profiles are supported through four distinct APX PIDs:

| Module | Profile | APX PID |
| --- | --- | --- |
| P3767-0000 Orin NX 16GB | `helm-orin-nx-16gb-r39.2` | `0x7323` |
| P3767-0001 Orin NX 8GB | `helm-orin-nx-8gb-r39.2` | `0x7423` |
| P3767-0003 Orin Nano 8GB | `helm-orin-nano-8gb-r39.2` | `0x7523` |
| P3767-0004 Orin Nano 4GB | `helm-orin-nano-4gb-r39.2` | `0x7623` |
| P3767-0005 Orin Nano 8GB SD | `helm-orin-nano-8gb-sd-r39.2` | `0x7523` |

The browser cannot distinguish P3767-0003 from P3767-0005 by APX descriptor.
PIDs `0x7323`, `0x7423`, and `0x7623` each select their sole catalog profile.
PID `0x7523` never guesses: the operator must choose P3767-0003 or P3767-0005.
Closing the detection chooser leaves the full manual profile selector
available. Bundle validation still refuses the shared PID unless the
application gives an explicit expected profile, and the target-side EEPROM
guard remains the authority before any persistent QSPI write.

## Remaining browser boundary

WebUSB does not expose a stable physical-port path. T234 also advertises
`iSerialNumber=0`; manually reading descriptor 3 for the CID does not turn it
into a WebUSB serial. Chromium therefore revokes the ephemeral BootROM grant
when APX disconnects and normally requires a second customer chooser gesture
for the MB1/PSC stage. The code always requires that fresh chooser action, but
does not use JavaScript object identity as a handoff signal: Chromium/macOS can
reuse the same `USBDevice` wrapper when the underlying APX transport changes.
Instead, it requires the exact 68-byte MB1/PSC banner before either remaining
artifact is sent. A stale BootROM only times out and receives no stage-two
payload. WebUSB still exposes neither a stable serial nor a port path here, so
the exactly-one-board acknowledgment and the operator's second chooser
selection remain the physical binding. Multiple authorized exact-PID matches
fail closed.

The included `web-serial-transport.mjs`, `recovery-protocol.mjs`, and static
application implement the post-RCM phase. They retain distinct random tokens
for readiness, preflight, and install; bind the full target inventory hash;
and accept only the requested final session marker. The browser never replaces
the module EEPROM, NVMe geometry, payload, QSPI backup, or full-readback guards
inside recovery.

After the destructive boundary, the page reduces to the selected device and
profile, four session-bound target stages, elapsed time, and an always-visible
raw session log. On desktop the log is a sticky console on the right; narrow
screens place it below the workflow. The stages are NVMe install, QSPI backup,
QSPI write, and full QSPI
readback verification. They are deliberately indeterminate: elapsed time is
not presented as device progress, and only the validated final success marker
may change the result to complete.

Web Serial exposes only the USB VID/PID, not the product, serial, or macOS
physical `locationID` used by the native guided CLI. The browser application
therefore requires the operator to connect exactly one Helm, and its device
chooser remains part of the trust boundary. A future all-WebUSB recovery path
should expose a narrow vendor-specific RPC interface rather than the current
physical-USB root console.

## Tests

Run the hardware-free source tests with Node 20 or newer:

```sh
node --test \
  tools/helm-web/test/rcm.test.mjs \
  tools/helm-web/recovery.test.mjs \
  tools/helm-web/test/server.test.mjs
tools/helm-web/prepare-site.py self-test
```

The suite covers profile/PID binding, unique and ambiguous APX detection,
strict manifests, incremental hashing, exact and family-wide device filters,
the serialless two-grant handoff, partial transfers, the complete six-file wire order,
the 68-byte banner, progress, duplicate-device rejection, timeout cleanup,
token-bound recovery markers, exact confirmation, unknown-state handling,
static path confinement, headers, HEAD requests, and bounded byte ranges.

The WebUSB protocol and generated bundles are source- and simulation-verified.
The complete browser flow has also passed on a physical P3767-0001, including
the two-grant BootROM-to-MB1 handoff and verified NVMe/QSPI provisioning.
These tests do not establish physical qualification for the other four SKUs
or broader browser/host combinations.
