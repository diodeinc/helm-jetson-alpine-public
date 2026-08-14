import assert from "node:assert/strict";
import test from "node:test";

import {
  PersistentStateUnknownError,
  RECOVERY_USB_PRODUCT_ID,
  RECOVERY_USB_VENDOR_ID,
  RecoveryProtocolError,
  RecoverySession,
  confirmationPhrase,
  installCommand,
  parseMarker,
  preflightCommand,
  randomSessionToken,
} from "./recovery-protocol.mjs";
import { WebSerialTransport } from "./web-serial-transport.mjs";

const PROFILE = "helm-orin-nx-8gb-r39.2";
const DEVICE = "/dev/nvme0n1";
const READY_TOKEN = "0".repeat(32);
const PREFLIGHT_TOKEN = "1".repeat(32);
const INSTALL_TOKEN = "2".repeat(32);
const INVENTORY = "abcdef0123456789".repeat(4);

test("protocol accepts only complete token-bound marker lines", () => {
  assert.deepEqual(
    parseMarker(
      `HELM_PROVISION_PREFLIGHT_SUCCESS session=${PREFLIGHT_TOKEN} profile=${PROFILE} device=${DEVICE} inventory=${INVENTORY}`,
    ),
    {
      kind: "preflight-success",
      session: PREFLIGHT_TOKEN,
      profile: PROFILE,
      device: DEVICE,
      inventory: INVENTORY,
    },
  );
  assert.equal(
    parseMarker(
      `printf 'HELM_PROVISION_SUCCESS session=${PREFLIGHT_TOKEN} profile=${PROFILE} device=${DEVICE}'`,
    ),
    null,
  );
  assert.equal(
    parseMarker(
      `HELM_PROVISION_SUCCESS session=${PREFLIGHT_TOKEN} profile=${PROFILE} device=${DEVICE}\nextra`,
    ),
    null,
  );
});

test("commands preserve the target-side confirmation contract", () => {
  assert.equal(
    preflightCommand(PROFILE, DEVICE, PREFLIGHT_TOKEN),
    `helm-provision preflight --device ${DEVICE} --confirm-profile ${PROFILE} --session ${PREFLIGHT_TOKEN}\n`,
  );
  assert.match(installCommand(PROFILE, DEVICE, INSTALL_TOKEN, INVENTORY), /--confirm-inventory [0-9a-f]{64}/);
  assert.equal(
    confirmationPhrase(PROFILE, DEVICE, INVENTORY),
    `ERASE ${DEVICE} AND FLASH QSPI FOR ${PROFILE} INVENTORY abcdef012345`,
  );
  assert.throws(
    () => installCommand(PROFILE, "/dev/nvme0n1p1", INSTALL_TOKEN, INVENTORY),
    RecoveryProtocolError,
  );
  assert.throws(
    () => installCommand(PROFILE, DEVICE, INSTALL_TOKEN, "A".repeat(64)),
    RecoveryProtocolError,
  );
});

test("session tokens use 16 bytes of browser randomness", () => {
  const token = randomSessionToken({
    getRandomValues(bytes) {
      bytes.forEach((_, index) => {
        bytes[index] = index;
      });
      return bytes;
    },
  });
  assert.equal(token, "000102030405060708090a0b0c0d0e0f");
});

class FakePort {
  constructor(onWrite, info = {
    usbVendorId: RECOVERY_USB_VENDOR_ID,
    usbProductId: RECOVERY_USB_PRODUCT_ID,
  }) {
    this.info = info;
    this.writes = [];
    this.openOptions = null;
    this.closed = false;
    this.readable = new ReadableStream({
      start: (controller) => {
        this.controller = controller;
      },
      cancel: () => {},
    });
    this.writable = new WritableStream({
      write: async (chunk) => {
        const text = new TextDecoder().decode(chunk);
        this.writes.push(text);
        await onWrite(text, this);
      },
    });
  }

  getInfo() {
    return this.info;
  }

  async open(options) {
    this.openOptions = options;
  }

  async close() {
    this.closed = true;
  }

  emit(text) {
    this.controller.enqueue(new TextEncoder().encode(text));
  }
}

function fakeSerialFor(port) {
  return {
    requestOptions: null,
    async requestPort(options) {
      this.requestOptions = options;
      return port;
    },
  };
}

test("Web Serial transport filters and verifies Helm recovery identity", async () => {
  const outputs = [];
  const port = new FakePort(async () => {});
  const serial = fakeSerialFor(port);
  const transport = new WebSerialTransport({ serial, onOutput: (text) => outputs.push(text) });
  await transport.requestAndOpen();
  assert.deepEqual(serial.requestOptions, {
    filters: [{ usbVendorId: 0x0955, usbProductId: 0x7020 }],
  });
  assert.equal(port.openOptions.baudRate, 115_200);
  const line = transport.waitForLine((candidate) => candidate === "target ready", {
    timeoutMs: 1_000,
  });
  port.emit("target ");
  port.emit("ready\r\n");
  assert.equal(await line, "target ready");
  assert.equal(outputs.join(""), "target ready\r\n");
  await transport.close();
  assert.equal(port.closed, true);
});

test("Web Serial transport rejects a mismatched chooser result", async () => {
  const port = new FakePort(async () => {}, { usbVendorId: 0x0955, usbProductId: 0x7423 });
  const transport = new WebSerialTransport({ serial: fakeSerialFor(port) });
  await assert.rejects(() => transport.requestAndOpen(), /not Helm recovery/);
});

test("Web Serial marker waiters can be cancelled without a later timeout", async () => {
  const port = new FakePort(async () => {});
  const transport = new WebSerialTransport({ serial: fakeSerialFor(port) });
  await transport.requestAndOpen();
  const controller = new AbortController();
  const waiting = transport.waitForLine(() => false, {
    timeoutMs: 10_000,
    signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(waiting, /wait was cancelled/);
  await transport.close();
});

test("guided Web Serial flow preflights before exact confirmation and install", async () => {
  const tokens = [READY_TOKEN, PREFLIGHT_TOKEN, INSTALL_TOKEN];
  const port = new FakePort(async (command, device) => {
    const ready = /HELM_PROVISION_READY session=([0-9a-f]{32})/.exec(command);
    if (ready) {
      device.emit(`\r\nHELM_PROVISION_READY session=${ready[1]}\r\n`);
      return;
    }
    if (command.startsWith("helm-provision preflight ")) {
      device.emit(
        `HELM_PROVISION_PREFLIGHT_SUCCESS session=${PREFLIGHT_TOKEN} profile=${PROFILE} device=${DEVICE} inventory=${INVENTORY}\r\n`,
      );
      return;
    }
    if (command.startsWith("helm-provision install ")) {
      device.emit(
        `HELM_PROVISION_SUCCESS session=${INSTALL_TOKEN} profile=${PROFILE} device=${DEVICE}\r\n`,
      );
    }
  });
  const transport = new WebSerialTransport({ serial: fakeSerialFor(port) });
  await transport.requestAndOpen();
  const session = new RecoverySession(transport, {
    tokenFactory: () => tokens.shift(),
  });
  await session.waitUntilReady({ timeoutMs: 1_000, probeIntervalMs: 100 });
  const preflight = await session.preflight(PROFILE, DEVICE, { timeoutMs: 1_000 });
  assert.equal(preflight.inventory, INVENTORY);
  assert.equal(port.writes.filter((line) => line.startsWith("helm-provision install ")).length, 0);
  await assert.rejects(() => session.install(`${preflight.confirmation} `), /confirmation did not match/);
  assert.equal(port.writes.filter((line) => line.startsWith("helm-provision install ")).length, 0);
  assert.deepEqual(await session.install(preflight.confirmation, { timeoutMs: 1_000 }), {
    profile: PROFILE,
    device: DEVICE,
  });
  assert.match(port.writes.at(-1), new RegExp(`--confirm-inventory ${INVENTORY}`));
  await transport.close();
});

test("any error after install transmission is reported as unknown target state", async () => {
  let pendingResolve;
  const transport = {
    async writeAscii(command) {
      if (command.startsWith("helm-provision preflight ")) {
        pendingResolve(
          `HELM_PROVISION_PREFLIGHT_SUCCESS session=${PREFLIGHT_TOKEN} profile=${PROFILE} device=${DEVICE} inventory=${INVENTORY}`,
        );
        return;
      }
      throw new Error("USB disconnected");
    },
    waitForLine() {
      return new Promise((resolve) => {
        pendingResolve = resolve;
      });
    },
  };
  const tokens = [PREFLIGHT_TOKEN, INSTALL_TOKEN];
  const session = new RecoverySession(transport, { tokenFactory: () => tokens.shift() });
  const preflight = await session.preflight(PROFILE, DEVICE, { timeoutMs: 1_000 });
  await assert.rejects(
    () => session.install(preflight.confirmation, { timeoutMs: 1_000 }),
    PersistentStateUnknownError,
  );
});
