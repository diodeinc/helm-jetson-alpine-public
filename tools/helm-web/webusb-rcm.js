import {
  NVIDIA_VENDOR_ID,
  RCM_BOOTLOADER_ARTIFACTS,
  RCM_BOOTROM_ARTIFACTS,
  SUPPORTED_PRODUCT_IDS,
  isValidatedRcmBundle,
} from "./bundle.js";

export const WRITE_CHUNK_SIZE = 0x4000;
export const BOOTROM_UID_DESCRIPTOR_SIZE = 0x82;
export const BOOTLOADER_BANNER_SIZE = 0x44;

export const DEFAULT_TIMEOUTS = Object.freeze({
  usb: 5_000,
  handoff: 30_000,
  banner: 30_000,
  reopen: 5_000,
  poll: 100,
});

export class ApxWebUsbError extends Error {
  constructor(message) {
    super(message);
    this.name = "ApxWebUsbError";
  }
}

export class ApxTimeoutError extends ApxWebUsbError {
  constructor(message) {
    super(message);
    this.name = "ApxTimeoutError";
  }
}

function fail(message) {
  throw new ApxWebUsbError(message);
}

function supportedProductId(productId) {
  return Number.isInteger(productId) && SUPPORTED_PRODUCT_IDS.includes(productId);
}

function formatUsbId(value) {
  return `0x${value.toString(16).padStart(4, "0")}`;
}

export function webUsbFilters(productId = undefined) {
  if (productId !== undefined) {
    if (!supportedProductId(productId)) {
      throw new ApxWebUsbError(`unsupported APX product ID: ${productId}`);
    }
    return [{ vendorId: NVIDIA_VENDOR_ID, productId }];
  }
  return SUPPORTED_PRODUCT_IDS.map((id) => ({
    vendorId: NVIDIA_VENDOR_ID,
    productId: id,
  }));
}

export function assertApxDevice(device, expectedProductId) {
  if (!device || device.vendorId !== NVIDIA_VENDOR_ID ||
      device.productId !== expectedProductId ||
      !supportedProductId(device.productId)) {
    const actual = device
      ? `${formatUsbId(device.vendorId)}:${formatUsbId(device.productId)}`
      : "no device";
    throw new ApxWebUsbError(
      `selected USB device is ${actual}; expected ` +
      `${formatUsbId(NVIDIA_VENDOR_ID)}:${formatUsbId(expectedProductId)}`,
    );
  }
  return device;
}

export async function requestApxDevice(usb, expectedProductId) {
  if (typeof usb?.requestDevice !== "function") {
    throw new ApxWebUsbError(
      "WebUSB is unavailable; use a Chromium browser in a secure context",
    );
  }
  const device = await usb.requestDevice({
    filters: webUsbFilters(expectedProductId),
  });
  return assertApxDevice(device, expectedProductId);
}

export function withTimeout(promise, timeoutMs, label, onTimeout, timers = {}) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new TypeError("timeout must be a positive number of milliseconds");
  }
  const setTimer = timers.setTimeoutFn ?? globalThis.setTimeout;
  const clearTimer = timers.clearTimeoutFn ?? globalThis.clearTimeout;
  let timerId;
  const timeout = new Promise((_, reject) => {
    timerId = setTimer(() => {
      try {
        const cleanup = onTimeout?.();
        Promise.resolve(cleanup).catch(() => {});
      } catch {
        // Timeout cleanup is best-effort; preserve the useful timeout error.
      }
      reject(new ApxTimeoutError(`${label} timed out after ${timeoutMs} ms`));
    }, timeoutMs);
  });
  return Promise.race([Promise.resolve(promise), timeout])
    .finally(() => clearTimer(timerId));
}

function safeProgress(callback, event) {
  try {
    callback?.(Object.freeze(event));
  } catch {
    // UI reporting must not alter the USB protocol state.
  }
}

async function closeDeviceBestEffort(device, interfaceNumber = null) {
  if (!device) {
    return;
  }
  if (device.opened && interfaceNumber !== null &&
      typeof device.releaseInterface === "function") {
    try {
      await device.releaseInterface(interfaceNumber);
    } catch {
      // Continue to close the device handle.
    }
  }
  if (device.opened && typeof device.close === "function") {
    try {
      await device.close();
    } catch {
      // Cleanup is best-effort after the first actionable failure.
    }
  }
}

function endpointPair(configuration) {
  for (const usbInterface of configuration?.interfaces ?? []) {
    for (const alternate of usbInterface.alternates ?? []) {
      let bulkIn = null;
      let bulkOut = null;
      for (const endpoint of alternate.endpoints ?? []) {
        if (endpoint.type !== "bulk") {
          continue;
        }
        if (endpoint.direction === "in") {
          bulkIn = endpoint.endpointNumber;
        } else if (endpoint.direction === "out") {
          bulkOut = endpoint.endpointNumber;
        }
      }
      if (bulkIn !== null && bulkOut !== null) {
        return Object.freeze({
          interfaceNumber: usbInterface.interfaceNumber,
          alternateSetting: alternate.alternateSetting,
          bulkIn,
          bulkOut,
        });
      }
    }
  }
  return null;
}

function configurationWithBulkPair(device) {
  if (device.configuration) {
    const endpoints = endpointPair(device.configuration);
    return endpoints
      ? { configurationValue: device.configuration.configurationValue, endpoints }
      : null;
  }
  for (const configuration of device.configurations ?? []) {
    const endpoints = endpointPair(configuration);
    if (endpoints) {
      return { configurationValue: configuration.configurationValue, endpoints };
    }
  }
  return null;
}

async function openBulkSession(device, timeoutMs, timeoutRunner) {
  let interfaceNumber = null;
  try {
    await timeoutRunner(
      device.open(),
      timeoutMs,
      "opening APX USB device",
      () => closeDeviceBestEffort(device),
    );

    let selected = configurationWithBulkPair(device);
    if (!selected && device.configuration) {
      fail("active APX configuration has no interface with bulk IN and OUT");
    }
    if (!selected) {
      fail("APX device exposes no configuration with bulk IN and OUT");
    }
    if (!device.configuration) {
      await timeoutRunner(
        device.selectConfiguration(selected.configurationValue),
        timeoutMs,
        "selecting APX USB configuration",
        () => closeDeviceBestEffort(device),
      );
      selected = configurationWithBulkPair(device);
      if (!selected) {
        fail("selected APX configuration has no interface with bulk IN and OUT");
      }
    }

    interfaceNumber = selected.endpoints.interfaceNumber;
    await timeoutRunner(
      device.claimInterface(interfaceNumber),
      timeoutMs,
      "claiming APX USB interface",
      () => closeDeviceBestEffort(device, interfaceNumber),
    );

    const claimedInterface = device.configuration.interfaces.find(
      (entry) => entry.interfaceNumber === interfaceNumber,
    );
    const activeAlternate = claimedInterface?.alternate?.alternateSetting;
    if (activeAlternate !== selected.endpoints.alternateSetting) {
      await timeoutRunner(
        device.selectAlternateInterface(
          interfaceNumber,
          selected.endpoints.alternateSetting,
        ),
        timeoutMs,
        "selecting APX USB alternate interface",
        () => closeDeviceBestEffort(device, interfaceNumber),
      );
    }

    return Object.freeze({
      device,
      interfaceNumber,
      bulkIn: selected.endpoints.bulkIn,
      bulkOut: selected.endpoints.bulkOut,
    });
  } catch (error) {
    await closeDeviceBestEffort(device, interfaceNumber);
    throw error;
  }
}

async function closeSession(session) {
  await closeDeviceBestEffort(session?.device, session?.interfaceNumber ?? null);
}

function dataBytes(data) {
  if (!(data instanceof DataView)) {
    fail("WebUSB transfer returned no data");
  }
  return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
}

async function readBootromUid(session, timeoutMs, timeoutRunner) {
  const result = await timeoutRunner(
    session.device.controlTransferIn({
      requestType: "standard",
      recipient: "device",
      request: 0x06,
      value: (0x03 << 8) | 3,
      index: 0,
    }, BOOTROM_UID_DESCRIPTOR_SIZE),
    timeoutMs,
    "reading BootROM UID descriptor",
    () => closeSession(session),
  );
  if (result?.status !== "ok") {
    fail(`BootROM UID descriptor transfer returned ${result?.status ?? "no status"}`);
  }
  const descriptor = dataBytes(result.data);
  if (descriptor.byteLength < 2 || descriptor[1] !== 0x03 || descriptor[0] < 2) {
    fail(`BootROM returned an invalid UID descriptor (${descriptor.byteLength} bytes)`);
  }

  const length = Math.min(descriptor[0], descriptor.byteLength);
  let value = "0x";
  for (let index = 2; index + 1 < length; index += 2) {
    const low = descriptor[index];
    const high = descriptor[index + 1];
    if (high === 0 && low >= 0x20 && low <= 0x7e) {
      value += String.fromCharCode(low);
    } else {
      value += high.toString(16).padStart(2, "0");
      value += low.toString(16).padStart(2, "0");
    }
  }
  return value;
}

async function transferArtifact(
  session,
  artifact,
  file,
  transferState,
  timeoutMs,
  timeoutRunner,
  onProgress,
) {
  let artifactBytesSent = 0;
  while (artifactBytesSent < file.size) {
    const end = Math.min(artifactBytesSent + WRITE_CHUNK_SIZE, file.size);
    const data = new Uint8Array(
      await file.slice(artifactBytesSent, end).arrayBuffer(),
    );
    if (data.byteLength !== end - artifactBytesSent) {
      fail(`short local read from ${artifact.name}`);
    }

    let chunkOffset = 0;
    while (chunkOffset < data.byteLength) {
      const pending = data.subarray(chunkOffset);
      const result = await timeoutRunner(
        session.device.transferOut(session.bulkOut, pending),
        timeoutMs,
        `sending ${artifact.name}`,
        () => closeSession(session),
      );
      if (result?.status !== "ok") {
        fail(`${artifact.name} bulk OUT returned ${result?.status ?? "no status"}`);
      }
      const written = result.bytesWritten;
      if (!Number.isSafeInteger(written) || written <= 0 ||
          written > pending.byteLength) {
        fail(`USB made invalid progress while sending ${artifact.name}`);
      }
      chunkOffset += written;
      artifactBytesSent += written;
      transferState.bytesSent += written;
      safeProgress(onProgress, {
        type: "transfer",
        stage: transferState.stage,
        kind: artifact.kind,
        file: artifact.name,
        artifactBytesSent,
        artifactBytesTotal: file.size,
        bundleBytesSent: transferState.bytesSent,
        bundleBytesTotal: transferState.bytesTotal,
      });
    }
  }
}

async function transferArtifactSet(
  session,
  definitions,
  bundle,
  transferState,
  timeoutMs,
  timeoutRunner,
  onProgress,
) {
  for (const artifact of definitions) {
    await transferArtifact(
      session,
      artifact,
      bundle.artifacts[artifact.name],
      transferState,
      timeoutMs,
      timeoutRunner,
      onProgress,
    );
  }
}

export function parseBootloaderBanner(value) {
  const banner = value instanceof Uint8Array
    ? value
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (banner.byteLength !== BOOTLOADER_BANNER_SIZE) {
    throw new ApxWebUsbError(
      `bootloader banner is ${banner.byteLength} bytes; expected ` +
      `${BOOTLOADER_BANNER_SIZE}`,
    );
  }

  let version = "";
  for (let index = 0; index < 64 && banner[index] !== 0; index += 1) {
    const byte = banner[index];
    version += byte >= 0x20 && byte <= 0x7e ? String.fromCharCode(byte) : ".";
  }
  const view = new DataView(banner.buffer, banner.byteOffset, banner.byteLength);
  return Object.freeze({
    version,
    lastBootError: view.getUint32(64, true),
  });
}

async function readBootloaderBanner(session, timeoutMs, timeoutRunner) {
  const banner = new Uint8Array(BOOTLOADER_BANNER_SIZE);
  let offset = 0;
  while (offset < banner.byteLength) {
    const result = await timeoutRunner(
      session.device.transferIn(session.bulkIn, banner.byteLength - offset),
      timeoutMs,
      "reading bootloader banner",
      () => closeSession(session),
    );
    if (result?.status !== "ok") {
      fail(`bootloader banner bulk IN returned ${result?.status ?? "no status"}`);
    }
    const bytes = dataBytes(result.data);
    if (bytes.byteLength <= 0 || bytes.byteLength > banner.byteLength - offset) {
      fail("USB made invalid progress while reading bootloader banner");
    }
    banner.set(bytes, offset);
    offset += bytes.byteLength;
  }
  return parseBootloaderBanner(banner);
}

function deviceIdentity(device) {
  const serialNumber = typeof device.serialNumber === "string" &&
    device.serialNumber.length > 0
    ? device.serialNumber
    : null;
  return Object.freeze({
    vendorId: device.vendorId,
    productId: device.productId,
    serialNumber,
  });
}

function identityMatches(device, identity) {
  return device && device.vendorId === identity.vendorId &&
    device.productId === identity.productId &&
    (identity.serialNumber === null || device.serialNumber === identity.serialNumber);
}

async function assertAuthorizedSelection(usb, selectedDevice, identity) {
  const authorized = await usb.getDevices();
  if (identity.serialNumber !== null) {
    const exactMatches = authorized.filter((device) => identityMatches(device, identity));
    if (exactMatches.length !== 1 || exactMatches[0] !== selectedDevice) {
      fail(
        "the browser's selected APX object is not the sole authorized device " +
        "with that VID, PID, and serial",
      );
    }
    return;
  }

  const pidMatches = authorized.filter((device) =>
    device.vendorId === identity.vendorId && device.productId === identity.productId,
  );
  if (pidMatches.length !== 1 || pidMatches[0] !== selectedDevice) {
    fail(
      "the selected APX device has no stable USB serial, so it must be the only " +
      "authorized device with that VID/PID",
    );
  }
}

async function defaultSleep(milliseconds) {
  await new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

async function waitForHandoffDevice(usb, previousDevice, identity, options) {
  const started = options.now();
  let observedDetach = false;
  while (options.now() - started < options.timeoutMs) {
    const authorized = await usb.getDevices();
    const matches = authorized.filter((device) => identityMatches(device, identity));
    if (matches.length > 1) {
      fail(
        `found ${matches.length} authorized APX devices with ` +
        `${formatUsbId(identity.productId)}; expected exactly one`,
      );
    }
    if (matches.length === 0) {
      observedDetach = true;
    } else {
      const sawTransition = observedDetach || options.didDisconnect();
      if (identity.serialNumber !== null) {
        if (matches[0] !== previousDevice || sawTransition) {
          return matches[0];
        }
      } else if (sawTransition) {
        // With no serial or physical path, accepting a merely different object
        // could silently switch boards. Require an observed disconnect first.
        return matches[0];
      }
    }
    await options.sleep(options.pollMs);
  }
  throw new ApxTimeoutError(
    `T234 device did not return for bootloader stage after ${options.timeoutMs} ms`,
  );
}

export class T234WebUsbRcm {
  constructor(options = {}) {
    this.usb = options.usb ?? globalThis.navigator?.usb;
    if (typeof this.usb?.requestDevice !== "function" ||
        typeof this.usb?.getDevices !== "function") {
      throw new ApxWebUsbError(
        "WebUSB is unavailable; use a Chromium browser in a secure context",
      );
    }
    this.timeouts = Object.freeze({ ...DEFAULT_TIMEOUTS, ...options.timeouts });
    this.timeoutRunner = options.timeoutRunner ?? withTimeout;
    this.sleep = options.sleep ?? defaultSleep;
    this.now = options.now ?? (() => Date.now());
  }

  async requestDevice(bundle) {
    if (!isValidatedRcmBundle(bundle)) {
      throw new ApxWebUsbError("bundle must pass validateRcmBundle() first");
    }
    return requestApxDevice(this.usb, bundle.profile.productId);
  }

  async boot(device, bundle, options = {}) {
    if (!isValidatedRcmBundle(bundle)) {
      throw new ApxWebUsbError("bundle must pass validateRcmBundle() first");
    }
    assertApxDevice(device, bundle.profile.productId);

    const identity = deviceIdentity(device);
    await assertAuthorizedSelection(this.usb, device, identity);
    let disconnected = false;
    const disconnectHandler = (event) => {
      if (event.device === device || identityMatches(event.device, identity)) {
        disconnected = true;
      }
    };
    this.usb.addEventListener?.("disconnect", disconnectHandler);

    const transferState = {
      stage: "bootrom",
      bytesSent: 0,
      bytesTotal: bundle.totalBytes,
    };
    let cid;
    let banner;
    try {
      safeProgress(options.onProgress, {
        type: "phase",
        phase: "bootrom",
        message: "Reading BootROM CID and sending four signed artifacts",
      });
      let session = await openBulkSession(
        device,
        this.timeouts.usb,
        this.timeoutRunner,
      );
      try {
        cid = await readBootromUid(
          session,
          this.timeouts.usb,
          this.timeoutRunner,
        );
        safeProgress(options.onProgress, { type: "cid", cid });
        await transferArtifactSet(
          session,
          RCM_BOOTROM_ARTIFACTS,
          bundle,
          transferState,
          this.timeouts.usb,
          this.timeoutRunner,
          options.onProgress,
        );
      } finally {
        await closeSession(session);
      }

      safeProgress(options.onProgress, {
        type: "phase",
        phase: "handoff",
        message: "Waiting for MB1/PSC bootloader handoff",
      });
      const bootloaderDevice = await waitForHandoffDevice(
        this.usb,
        device,
        identity,
        {
          timeoutMs: this.timeouts.handoff,
          pollMs: this.timeouts.poll,
          sleep: this.sleep,
          now: this.now,
          didDisconnect: () => disconnected,
        },
      );
      assertApxDevice(bootloaderDevice, bundle.profile.productId);

      session = await openBulkSession(
        bootloaderDevice,
        this.timeouts.usb,
        this.timeoutRunner,
      );
      try {
        banner = await readBootloaderBanner(
          session,
          this.timeouts.banner,
          this.timeoutRunner,
        );
        safeProgress(options.onProgress, { type: "banner", ...banner });
      } finally {
        await closeSession(session);
      }

      safeProgress(options.onProgress, {
        type: "phase",
        phase: "bootloader",
        message: "Sending memory BCT and recovery blob",
      });
      transferState.stage = "bootloader";
      session = await openBulkSession(
        bootloaderDevice,
        this.timeouts.reopen,
        this.timeoutRunner,
      );
      try {
        await transferArtifactSet(
          session,
          RCM_BOOTLOADER_ARTIFACTS,
          bundle,
          transferState,
          this.timeouts.usb,
          this.timeoutRunner,
          options.onProgress,
        );
      } finally {
        await closeSession(session);
      }

      if (transferState.bytesSent !== transferState.bytesTotal) {
        fail(
          `RCM transfer sent ${transferState.bytesSent} of ` +
          `${transferState.bytesTotal} bytes`,
        );
      }
      const result = Object.freeze({
        profile: bundle.profile,
        cid,
        banner,
        bytesSent: transferState.bytesSent,
      });
      safeProgress(options.onProgress, { type: "complete", ...result });
      return result;
    } finally {
      this.usb.removeEventListener?.("disconnect", disconnectHandler);
    }
  }
}
