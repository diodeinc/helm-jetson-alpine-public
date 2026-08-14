import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  PROFILE_DEFINITIONS,
  RCM_ARTIFACTS,
  RCM_BOOTLOADER_ARTIFACTS,
  RCM_BOOTROM_ARTIFACTS,
  SUPPORTED_PRODUCT_IDS,
  BundleValidationError,
  validateRcmBundle,
} from "../bundle.js";
import { Sha256, sha256File, sha256Hex } from "../sha256.js";
import {
  ApxTimeoutError,
  ApxWebUsbError,
  BOOTLOADER_BANNER_SIZE,
  T234WebUsbRcm,
  WRITE_CHUNK_SIZE,
  parseBootloaderBanner,
  requestApxDevice,
  webUsbFilters,
  withTimeout,
} from "../webusb-rcm.js";

const encoder = new TextEncoder();

function bytes(value) {
  return typeof value === "string" ? encoder.encode(value) : new Uint8Array(value);
}

function makeFile(name, value) {
  return new File([bytes(value)], name, { type: "application/octet-stream" });
}

function profileText(definition) {
  return [
    `PROFILE=${definition.bundleProfile}`,
    "BSP_RELEASE=39.2",
    `BOARD=${definition.board}`,
    "CARRIER=Diode_Helm",
    "SIGNING=zerosbk",
    "HOST_BUILD=macos-native",
    "HOST_BOOT_PERSISTENT_WRITE=none",
    "TARGET_INSTALLERS=guarded-nvme-and-qspi",
    `USB_PID=0x${definition.productId.toString(16).padStart(4, "0")}`,
    `QSPI_PROFILE=${definition.id}`,
    `KERNEL_DTB=${definition.kernelDtb}`,
    "",
  ].join("\n");
}

function buildFixture(profileId, overrides = {}) {
  const definition = PROFILE_DEFINITIONS.find(({ id }) => id === profileId);
  assert.ok(definition);
  const content = Object.fromEntries(
    RCM_ARTIFACTS.map(({ name }, index) => [
      name,
      bytes(overrides[name] ?? `${index}:${name}:fixture`),
    ]),
  );
  content.PROFILE = bytes(overrides.PROFILE ?? profileText(definition));
  const checksumLines = [...RCM_ARTIFACTS.map(({ name }) => name), "PROFILE"]
    .map((name) => `${sha256Hex(content[name])}  ${name}`);
  content.SHA256SUMS = bytes(`${checksumLines.join("\n")}\n`);
  if (overrides.SHA256SUMS !== undefined) {
    content.SHA256SUMS = bytes(overrides.SHA256SUMS);
  }
  return {
    definition,
    content,
    files: Object.entries(content).map(([name, value]) => makeFile(name, value)),
  };
}

function concatenate(chunks) {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const output = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

function expectedSet(content, artifacts) {
  return concatenate(artifacts.map(({ name }) => content[name]));
}

function usbConfiguration() {
  const alternate = {
    alternateSetting: 0,
    endpoints: [
      { endpointNumber: 1, direction: "out", type: "bulk", packetSize: 512 },
      { endpointNumber: 1, direction: "in", type: "bulk", packetSize: 512 },
    ],
  };
  const usbInterface = {
    interfaceNumber: 0,
    alternates: [alternate],
    alternate,
  };
  return {
    configurationValue: 1,
    interfaces: [usbInterface],
  };
}

class FakeDevice {
  constructor(productId, options = {}) {
    this.vendorId = 0x0955;
    this.productId = productId;
    this.serialNumber = options.serialNumber ?? "fixture-cid";
    this.configuration = null;
    this.configurations = [usbConfiguration()];
    this.opened = false;
    this.claimed = false;
    this.calls = [];
    this.output = [];
    this.maximumWrite = options.maximumWrite ?? Number.POSITIVE_INFINITY;
    this.banner = options.banner ?? new Uint8Array();
    this.bannerOffset = 0;
    this.maximumRead = options.maximumRead ?? Number.POSITIVE_INFINITY;
  }

  async open() {
    this.calls.push("open");
    this.opened = true;
  }

  async close() {
    this.calls.push("close");
    this.opened = false;
  }

  async selectConfiguration(value) {
    this.calls.push(`configuration:${value}`);
    this.configuration = this.configurations.find(
      ({ configurationValue }) => configurationValue === value,
    );
  }

  async claimInterface(number) {
    this.calls.push(`claim:${number}`);
    this.claimed = true;
  }

  async releaseInterface(number) {
    this.calls.push(`release:${number}`);
    this.claimed = false;
  }

  async selectAlternateInterface(number, alternate) {
    this.calls.push(`alternate:${number}:${alternate}`);
  }

  async controlTransferIn(setup, length) {
    this.calls.push({ control: setup, length });
    const uid = new Uint8Array([8, 3, 0x43, 0, 0x49, 0, 0x44, 0]);
    return { status: "ok", data: new DataView(uid.buffer) };
  }

  async transferOut(endpoint, value) {
    const input = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    const count = Math.min(input.byteLength, this.maximumWrite);
    this.calls.push({ out: endpoint, requested: input.byteLength, written: count });
    this.output.push(input.slice(0, count));
    return { status: "ok", bytesWritten: count };
  }

  async transferIn(endpoint, length) {
    const count = Math.min(
      length,
      this.maximumRead,
      this.banner.byteLength - this.bannerOffset,
    );
    this.calls.push({ in: endpoint, requested: length, returned: count });
    const value = this.banner.slice(this.bannerOffset, this.bannerOffset + count);
    this.bannerOffset += count;
    return { status: "ok", data: new DataView(value.buffer) };
  }
}

class FakeUsb {
  constructor(selected, authorized) {
    this.selected = selected;
    this.authorized = authorized;
    this.request = null;
    this.listeners = new Map();
  }

  async requestDevice(options) {
    this.request = options;
    return this.selected;
  }

  async getDevices() {
    return typeof this.authorized === "function"
      ? this.authorized()
      : this.authorized;
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  removeEventListener(name, callback) {
    if (this.listeners.get(name) === callback) {
      this.listeners.delete(name);
    }
  }
}

test("incremental SHA-256 matches standard vectors and Node crypto", async () => {
  assert.equal(
    sha256Hex(new Uint8Array()),
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
  assert.equal(
    sha256Hex(encoder.encode("abc")),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  );
  const value = Uint8Array.from({ length: 1025 }, (_, index) => index & 0xff);
  const incremental = new Sha256();
  incremental.update(value.subarray(0, 1));
  incremental.update(value.subarray(1, 65));
  incremental.update(value.subarray(65));
  assert.equal(
    incremental.hexDigest(),
    createHash("sha256").update(value).digest("hex"),
  );
  assert.equal(
    await sha256File(makeFile("fixture", value), { chunkSize: 17 }),
    createHash("sha256").update(value).digest("hex"),
  );
});

test("profile table has five SKUs and four exact APX product IDs", () => {
  assert.equal(PROFILE_DEFINITIONS.length, 5);
  assert.deepEqual(SUPPORTED_PRODUCT_IDS, [0x7323, 0x7423, 0x7523, 0x7623]);
  assert.deepEqual(webUsbFilters(0x7423), [
    { vendorId: 0x0955, productId: 0x7423 },
  ]);
  assert.throws(() => webUsbFilters(0x7023), ApxWebUsbError);
});

test("strict bundle validation binds profile metadata and every digest", async () => {
  const fixture = buildFixture("helm-orin-nx-8gb-r39.2");
  assert.ok(fixture.files.every((file) => !("webkitRelativePath" in file)));
  const progress = [];
  const bundle = await validateRcmBundle(fixture.files, {
    expectedProfileId: fixture.definition.id,
    hashChunkSize: 7,
    onProgress: (event) => progress.push(event),
  });
  assert.equal(bundle.profile.board, "P3767-0001");
  assert.equal(bundle.profile.productId, 0x7423);
  assert.equal(bundle.totalBytes, RCM_ARTIFACTS.reduce(
    (total, { name }) => total + fixture.content[name].byteLength,
    0,
  ));
  assert.ok(progress.length > RCM_ARTIFACTS.length);
  assert.equal(progress.at(-1).bundleBytesHashed, progress.at(-1).bundleBytesTotal);

  const corrupt = buildFixture("helm-orin-nx-8gb-r39.2");
  const blobIndex = corrupt.files.findIndex(({ name }) => name === "blob.bin");
  corrupt.files[blobIndex] = makeFile("blob.bin", "corrupt after checksumming");
  await assert.rejects(
    validateRcmBundle(corrupt.files, { expectedProfileId: fixture.definition.id }),
    /SHA-256 mismatch for blob\.bin/,
  );

  const unexpected = fixture.files.slice(0, -1);
  unexpected.push(makeFile("NOT-A-BUNDLE-FILE", "x"));
  await assert.rejects(
    validateRcmBundle(unexpected, { expectedProfileId: fixture.definition.id }),
    /unexpected bundle file/,
  );
});

test("shared 0x7523 PID requires the customer to choose 0003 or 0005", async () => {
  const fixture = buildFixture("helm-orin-nano-8gb-r39.2");
  await assert.rejects(validateRcmBundle(fixture.files), /expectedProfileId is required/);
  await assert.rejects(
    validateRcmBundle(fixture.files, {
      expectedProfileId: "helm-orin-nano-8gb-sd-r39.2",
    }),
    BundleValidationError,
  );
  const bundle = await validateRcmBundle(fixture.files, {
    expectedProfileId: fixture.definition.id,
  });
  assert.equal(bundle.profile.sku, "0003");
});

test("device chooser uses an exact VID/PID filter and validates its result", async () => {
  const right = new FakeDevice(0x7423);
  const usb = new FakeUsb(right, [right]);
  assert.equal(await requestApxDevice(usb, 0x7423), right);
  assert.deepEqual(usb.request, {
    filters: [{ vendorId: 0x0955, productId: 0x7423 }],
  });

  const wrong = new FakeDevice(0x7323);
  await assert.rejects(
    requestApxDevice(new FakeUsb(wrong, [wrong]), 0x7423),
    /expected 0x0955:0x7423/,
  );
});

test("68-byte banner parser preserves the native loader semantics", () => {
  const banner = new Uint8Array(BOOTLOADER_BANNER_SIZE);
  banner.set(encoder.encode("MB1\u0001fixture"));
  new DataView(banner.buffer).setUint32(64, 0x78563412, true);
  assert.deepEqual(parseBootloaderBanner(banner), {
    version: "MB1.fixture",
    lastBootError: 0x78563412,
  });
  assert.throws(() => parseBootloaderBanner(new Uint8Array(67)), /expected 68/);
});

test("WebUSB boot mirrors the exact two-stage six-file RCM stream", async () => {
  const largeBlob = Uint8Array.from(
    { length: WRITE_CHUNK_SIZE + 19 },
    (_, index) => (index * 17) & 0xff,
  );
  const fixture = buildFixture("helm-orin-nx-8gb-r39.2", {
    "blob.bin": largeBlob,
  });
  const bundle = await validateRcmBundle(fixture.files, {
    expectedProfileId: fixture.definition.id,
    hashChunkSize: 127,
  });

  const bannerBytes = new Uint8Array(BOOTLOADER_BANNER_SIZE);
  bannerBytes.set(encoder.encode("MB1/PSC test"));
  new DataView(bannerBytes.buffer).setUint32(64, 7, true);
  const bootrom = new FakeDevice(0x7423, { maximumWrite: 13 });
  const bootloader = new FakeDevice(0x7423, {
    maximumWrite: 3_000,
    maximumRead: 11,
    banner: bannerBytes,
  });
  let deviceQuery = 0;
  const usb = new FakeUsb(bootrom, () => {
    deviceQuery += 1;
    return deviceQuery === 1 ? [bootrom] : [bootloader];
  });
  const progress = [];
  const transport = new T234WebUsbRcm({ usb });
  const result = await transport.boot(bootrom, bundle, {
    onProgress: (event) => progress.push(event),
  });

  assert.equal(result.cid, "0xCID");
  assert.equal(result.banner.version, "MB1/PSC test");
  assert.equal(result.banner.lastBootError, 7);
  assert.equal(result.bytesSent, bundle.totalBytes);
  assert.deepEqual(
    concatenate(bootrom.output),
    expectedSet(fixture.content, RCM_BOOTROM_ARTIFACTS),
  );
  assert.deepEqual(
    concatenate(bootloader.output),
    expectedSet(fixture.content, RCM_BOOTLOADER_ARTIFACTS),
  );
  assert.ok(
    bootloader.calls
      .filter((entry) => typeof entry === "object" && "out" in entry)
      .every(({ requested }) => requested <= WRITE_CHUNK_SIZE),
  );
  assert.equal(
    bootloader.calls.filter((entry) => entry === "open").length,
    2,
  );
  assert.equal(progress.at(-1).type, "complete");
  assert.equal(usb.listeners.size, 0);
});

test("handoff fails closed when more than one matching APX device appears", async () => {
  const fixture = buildFixture("helm-orin-nx-8gb-r39.2");
  const bundle = await validateRcmBundle(fixture.files, {
    expectedProfileId: fixture.definition.id,
  });
  const bootrom = new FakeDevice(0x7423);
  let deviceQuery = 0;
  const usb = new FakeUsb(bootrom, () => {
    deviceQuery += 1;
    return deviceQuery === 1 ? [bootrom] : [
      new FakeDevice(0x7423),
      new FakeDevice(0x7423),
    ];
  });
  await assert.rejects(
    new T234WebUsbRcm({ usb }).boot(bootrom, bundle),
    /expected exactly one/,
  );
});

test("serialless handoff cannot switch to a new object without a disconnect", async () => {
  const fixture = buildFixture("helm-orin-nx-8gb-r39.2");
  const bundle = await validateRcmBundle(fixture.files, {
    expectedProfileId: fixture.definition.id,
  });
  const bootrom = new FakeDevice(0x7423, { serialNumber: "" });
  const unrelated = new FakeDevice(0x7423, { serialNumber: "" });
  let deviceQuery = 0;
  const usb = new FakeUsb(bootrom, () => {
    deviceQuery += 1;
    return deviceQuery === 1 ? [bootrom] : [unrelated];
  });
  let clock = 0;
  const transport = new T234WebUsbRcm({
    usb,
    timeouts: { handoff: 3, poll: 1 },
    now: () => clock,
    sleep: async (milliseconds) => { clock += milliseconds; },
  });
  await assert.rejects(
    transport.boot(bootrom, bundle),
    /did not return for bootloader stage/,
  );
  assert.equal(unrelated.calls.length, 0);
});

test("timeouts are deterministic and invoke transport cleanup", async () => {
  let fire;
  let cleared = null;
  let cleaned = false;
  const pending = withTimeout(
    new Promise(() => {}),
    123,
    "fixture transfer",
    () => { cleaned = true; },
    {
      setTimeoutFn(callback) {
        fire = callback;
        return 99;
      },
      clearTimeoutFn(timerId) {
        cleared = timerId;
      },
    },
  );
  fire();
  await assert.rejects(pending, ApxTimeoutError);
  assert.equal(cleaned, true);
  assert.equal(cleared, 99);
});
