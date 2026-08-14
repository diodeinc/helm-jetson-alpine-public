# Helm browser flasher

This directory contains a dependency-free ES-module implementation of the
volatile T234 RCM boot used by Helm's pinned native macOS loader. It lets a
Chromium page load the Alpine recovery OS into RAM through WebUSB. The static
application then uses Web Serial to run the existing guarded read-only
preflight and, only after an exact typed phrase, the target-side NVMe/QSPI
provisioner.

The tailnet-only developer preview is hosted at
<https://preview.example.invalid/helm>. A public customer release
needs a dedicated public HTTPS origin such as `flash.diode.com`; the raw
`192.0.2.1` address cannot present the certificate needed for WebUSB.

## Build and run the site

Build all five native recovery bundles first, then validate every bundle and
prepare the static site:

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

1. Select the exact module, download its bundle, and verify every digest.
2. Grant BootROM APX access, then grant the re-enumerated MB1/PSC APX device
   and complete the volatile RAM boot.
3. Grant the re-enumerated `0955:7020` recovery console through Web Serial and
   run the target-side read-only preflight.
4. Review the NVMe inventory fingerprint, type the exact generated phrase,
   and explicitly start the persistent NVMe/QSPI operation.

It is one physical cable but three browser permission grants: BootROM WebUSB,
MB1/PSC WebUSB, then recovery Web Serial. Nothing starts the
installer automatically. A timeout or disconnect after the install command is
sent is reported as unknown persistent state, never as success.

## Browser contract

WebUSB requires a supported Chromium browser and a secure context. Use HTTPS
for a network address; plain `http://192.0.2.1` is not a WebUSB-capable
origin. `http://localhost` is the browser's development-only exception.
`navigator.usb.requestDevice()` must run from a customer click.

Validate the bundle before the click so hashing the large recovery blob does
not consume the browser's transient user activation:

```js
import {
  T234WebUsbRcm,
  validateRcmBundle,
} from "./tools/helm-web/index.js";

let bundle;
let rcm;
let handoff;

bundleInput.addEventListener("change", async () => {
  bundle = await validateRcmBundle(bundleInput.files, {
    // Required for the shared 0x7523 PID; recommended for every profile.
    expectedProfileId: profileSelect.value,
    // Production should pass a digest allowlist delivered by trusted site code.
    trustedChecksums: approvedDigests[profileSelect.value],
    onProgress: showHashProgress,
  });
});

bootButton.addEventListener("click", async () => {
  rcm = new T234WebUsbRcm();
  const device = await rcm.requestDevice(bundle);
  handoff = await rcm.bootrom(device, bundle, {
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

## Exact transfer sequence

The implementation mirrors
`t234-bootkit` commit `5cedc336c859a2561644475aef057feaf41e73c0` with
`tools/helm-macos/t234-bootkit-orin-family.patch` applied:

1. Select exactly `0955:<profile PID>` and claim the first interface alternate
   with bulk IN and bulk OUT.
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
Bundle validation therefore refuses PID `0x7523` unless the application gives
an explicit expected profile. The target-side EEPROM guard must remain the
authority before any persistent QSPI write.

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
profile, four session-bound target stages, elapsed time, and a collapsed raw
log. The stages are NVMe install, QSPI backup, QSPI write, and full QSPI
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

The suite covers profile/PID binding, strict manifests, incremental hashing,
exact device filters, the serialless two-grant handoff, partial transfers, the complete six-file wire order,
the 68-byte banner, progress, duplicate-device rejection, timeout cleanup,
token-bound recovery markers, exact confirmation, unknown-state handling,
static path confinement, headers, HEAD requests, and bounded byte ranges.

The WebUSB protocol and generated bundles are source- and simulation-verified.
The two-grant BootROM-to-MB1 flow still needs physical qualification in desktop
Chrome on every supported host/platform combination.
