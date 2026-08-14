import {
  RECOVERY_USB_PRODUCT_ID,
  RECOVERY_USB_VENDOR_ID,
  RecoveryProtocolError,
} from "./recovery-protocol.mjs";

class LineFramer {
  #buffer = "";
  #maximumLength;

  constructor(maximumLength) {
    this.#maximumLength = maximumLength;
  }

  push(text) {
    this.#buffer += text;
    if (this.#buffer.length > this.#maximumLength && !this.#buffer.includes("\n")) {
      throw new RecoveryProtocolError("recovery console produced an overlong line");
    }
    const lines = [];
    let newline;
    while ((newline = this.#buffer.indexOf("\n")) !== -1) {
      let line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      if (line.length > this.#maximumLength) {
        throw new RecoveryProtocolError("recovery console produced an overlong line");
      }
      lines.push(line);
    }
    return lines;
  }
}

export class WebSerialTransport {
  #serial;
  #port = null;
  #reader = null;
  #writer = null;
  #decoder = new TextDecoder("utf-8", { fatal: false });
  #framer;
  #waiters = new Set();
  #onOutput;
  #readTask = null;
  #closed = false;

  constructor({
    serial = globalThis.navigator?.serial,
    onOutput = () => {},
    maximumLineLength = 65_536,
  } = {}) {
    this.#serial = serial;
    this.#onOutput = onOutput;
    this.#framer = new LineFramer(maximumLineLength);
  }

  async requestAndOpen() {
    if (!this.#serial || typeof this.#serial.requestPort !== "function") {
      throw new RecoveryProtocolError(
        "Web Serial is unavailable; use a supported desktop Chromium browser",
      );
    }
    if (globalThis.isSecureContext === false) {
      throw new RecoveryProtocolError("Web Serial requires a trusted HTTPS origin");
    }
    if (this.#port !== null) {
      throw new RecoveryProtocolError("recovery serial transport is already open");
    }

    const port = await this.#serial.requestPort({
      filters: [
        {
          usbVendorId: RECOVERY_USB_VENDOR_ID,
          usbProductId: RECOVERY_USB_PRODUCT_ID,
        },
      ],
    });
    const info = port.getInfo();
    if (
      info.usbVendorId !== RECOVERY_USB_VENDOR_ID ||
      info.usbProductId !== RECOVERY_USB_PRODUCT_ID
    ) {
      throw new RecoveryProtocolError("selected serial port is not Helm recovery 0955:7020");
    }

    try {
      await port.open({
        baudRate: 115_200,
        dataBits: 8,
        stopBits: 1,
        parity: "none",
        flowControl: "none",
        bufferSize: 65_536,
      });
      if (port.readable === null || port.writable === null) {
        throw new RecoveryProtocolError("Helm recovery serial streams are unavailable");
      }
      this.#port = port;
      this.#reader = port.readable.getReader();
      this.#writer = port.writable.getWriter();
      this.#readTask = this.#pump();
    } catch (error) {
      try {
        await port.close();
      } catch {
        // Preserve the original open error.
      }
      throw error;
    }
  }

  async #pump() {
    try {
      while (!this.#closed) {
        const { value, done } = await this.#reader.read();
        if (done) {
          if (!this.#closed) {
            throw new RecoveryProtocolError("recovery console disconnected");
          }
          break;
        }
        const text = this.#decoder.decode(value, { stream: true });
        this.#onOutput(text);
        for (const line of this.#framer.push(text)) {
          this.#dispatch(line);
        }
      }
    } catch (error) {
      this.#failWaiters(error);
    }
  }

  #dispatch(line) {
    for (const waiter of Array.from(this.#waiters)) {
      let matches = false;
      try {
        matches = waiter.predicate(line);
      } catch (error) {
        this.#settle(waiter, "reject", error);
        continue;
      }
      if (matches) {
        this.#settle(waiter, "resolve", line);
      }
    }
  }

  #settle(waiter, action, value) {
    if (!this.#waiters.delete(waiter)) {
      return;
    }
    clearTimeout(waiter.timer);
    waiter.signal?.removeEventListener("abort", waiter.abortHandler);
    waiter[action](value);
  }

  #failWaiters(error) {
    const failure =
      error instanceof Error
        ? error
        : new RecoveryProtocolError("recovery console failed");
    for (const waiter of Array.from(this.#waiters)) {
      this.#settle(waiter, "reject", failure);
    }
  }

  waitForLine(predicate, { timeoutMs, signal } = {}) {
    if (this.#reader === null || this.#closed) {
      return Promise.reject(new RecoveryProtocolError("recovery serial transport is not open"));
    }
    if (typeof predicate !== "function") {
      return Promise.reject(new TypeError("line predicate must be a function"));
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      return Promise.reject(new TypeError("timeoutMs must be positive"));
    }
    if (signal?.aborted) {
      return Promise.reject(new RecoveryProtocolError("recovery console wait was cancelled"));
    }
    return new Promise((resolve, reject) => {
      const waiter = {
        predicate,
        resolve,
        reject,
        timer: null,
        signal,
        abortHandler: null,
      };
      waiter.abortHandler = () => {
        this.#settle(
          waiter,
          "reject",
          new RecoveryProtocolError("recovery console wait was cancelled"),
        );
      };
      waiter.timer = setTimeout(() => {
        this.#settle(
          waiter,
          "reject",
          new RecoveryProtocolError("timed out waiting for the target recovery console"),
        );
      }, timeoutMs);
      this.#waiters.add(waiter);
      signal?.addEventListener("abort", waiter.abortHandler, { once: true });
    });
  }

  async writeAscii(text) {
    if (this.#writer === null || this.#closed) {
      throw new RecoveryProtocolError("recovery serial transport is not open");
    }
    if (typeof text !== "string" || /[^\x00-\x7f]/.test(text)) {
      throw new RecoveryProtocolError("recovery command must contain only ASCII bytes");
    }
    await this.#writer.write(new TextEncoder().encode(text));
  }

  async close() {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.#failWaiters(new RecoveryProtocolError("recovery serial transport closed"));
    try {
      await this.#reader?.cancel();
    } catch {
      // Continue closing the remaining browser resources.
    }
    await this.#readTask;
    try {
      await this.#writer?.close();
    } catch {
      // The port may already have disconnected.
    }
    this.#reader?.releaseLock();
    this.#writer?.releaseLock();
    this.#reader = null;
    this.#writer = null;
    const port = this.#port;
    this.#port = null;
    if (port !== null) {
      try {
        await port.close();
      } catch {
        // Closing a disconnected physical device is best-effort.
      }
    }
  }
}
