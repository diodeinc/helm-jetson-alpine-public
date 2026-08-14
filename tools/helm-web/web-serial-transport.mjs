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

  reset() {
    this.#buffer = "";
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
        // Leave enough host-side slack for bootloader/install diagnostics even
        // if rendering the visible session log briefly stalls the main thread.
        bufferSize: 1_048_576,
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
      while (!this.#closed && this.#port !== null && this.#port.readable !== null) {
        const readable = this.#port.readable;
        const reader = this.#reader ?? readable.getReader();
        this.#reader = reader;
        let readFailure = null;
        let streamEnded = false;
        try {
          while (!this.#closed) {
            let result;
            try {
              result = await reader.read();
            } catch (error) {
              readFailure = error;
              break;
            }
            if (result.done) {
              streamEnded = true;
              break;
            }
            const text = this.#decoder.decode(result.value, { stream: true });
            for (const line of this.#framer.push(text)) {
              this.#dispatch(line);
            }
            try {
              this.#onOutput(text);
            } catch {
              // Visible logging is nonessential. It must never stop the protocol
              // reader or a persistent operation already running on the target.
            }
          }
        } finally {
          if (this.#reader === reader) {
            this.#reader = null;
          }
          reader.releaseLock();
        }

        if (this.#closed) {
          break;
        }
        if (readFailure !== null) {
          // Chrome replaces port.readable after recoverable serial conditions
          // such as buffer overrun, framing, or parity errors. Keep existing
          // marker waiters alive and attach a reader to that replacement.
          const replacement = this.#port?.readable ?? null;
          if (replacement !== null && replacement !== readable) {
            // Bytes may have been lost at the serial error boundary. Discard
            // any partial UTF-8 sequence or unterminated line from the failed
            // stream so it cannot corrupt a marker on the replacement.
            this.#decoder = new TextDecoder("utf-8", { fatal: false });
            this.#framer.reset();
            continue;
          }
          throw readFailure;
        }
        if (streamEnded) {
          throw new RecoveryProtocolError("recovery console disconnected");
        }
      }
      if (!this.#closed) {
        throw new RecoveryProtocolError("recovery console disconnected");
      }
    } catch (error) {
      if (!this.#closed) {
        this.#failWaiters(error);
      }
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

  ownsPort(port) {
    return this.#port !== null && port === this.#port;
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
