const PROFILE_PATTERN = /^helm-orin-(?:nx-(?:8gb|16gb)|nano-(?:4gb|8gb|8gb-sd))-r39\.2$/;
const DEVICE_PATTERN = /^\/dev\/nvme[0-9]n[0-9]$/;
const TOKEN_PATTERN = /^[0-9a-f]{32}$/;
const INVENTORY_PATTERN = /^[0-9a-f]{64}$/;

const SUCCESS_PATTERN = /^HELM_PROVISION_SUCCESS session=([0-9a-f]{32}) profile=([^ ]+) device=([^ ]+)$/;
const FAILURE_PATTERN = /^HELM_PROVISION_FAILURE session=([0-9a-f]{32}) rc=([0-9]+)$/;
const PREFLIGHT_PATTERN = /^HELM_PROVISION_PREFLIGHT_SUCCESS session=([0-9a-f]{32}) profile=([^ ]+) device=([^ ]+) inventory=([0-9a-f]{64})$/;

export const RECOVERY_USB_VENDOR_ID = 0x0955;
export const RECOVERY_USB_PRODUCT_ID = 0x7020;

export class RecoveryProtocolError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "RecoveryProtocolError";
  }
}

export class PersistentStateUnknownError extends RecoveryProtocolError {
  constructor(message, options) {
    super(message, options);
    this.name = "PersistentStateUnknownError";
  }
}

export function validateRequest(profile, device, token) {
  if (!PROFILE_PATTERN.test(profile)) {
    throw new RecoveryProtocolError(`unsupported recovery profile: ${profile}`);
  }
  if (!DEVICE_PATTERN.test(device)) {
    throw new RecoveryProtocolError(`refusing non-NVMe whole-disk path: ${device}`);
  }
  if (!TOKEN_PATTERN.test(token)) {
    throw new RecoveryProtocolError("invalid recovery session token");
  }
}

export function randomSessionToken(cryptoProvider = globalThis.crypto) {
  if (typeof cryptoProvider?.getRandomValues !== "function") {
    throw new RecoveryProtocolError("secure browser randomness is unavailable");
  }
  const bytes = cryptoProvider.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function parseMarker(line) {
  if (typeof line !== "string" || /[\r\n]/.test(line)) {
    return null;
  }
  let match = SUCCESS_PATTERN.exec(line);
  if (match) {
    return {
      kind: "success",
      session: match[1],
      profile: match[2],
      device: match[3],
    };
  }
  match = PREFLIGHT_PATTERN.exec(line);
  if (match) {
    return {
      kind: "preflight-success",
      session: match[1],
      profile: match[2],
      device: match[3],
      inventory: match[4],
    };
  }
  match = FAILURE_PATTERN.exec(line);
  if (match) {
    return {
      kind: "failure",
      session: match[1],
      returncode: Number.parseInt(match[2], 10),
    };
  }
  return null;
}

export function readyCommand(token) {
  validateRequest("helm-orin-nx-8gb-r39.2", "/dev/nvme0n1", token);
  const marker = `HELM_PROVISION_READY session=${token}`;
  return `printf '\\n${marker}\\n'\n`;
}

export function preflightCommand(profile, device, token) {
  validateRequest(profile, device, token);
  return `helm-provision preflight --device ${device} --confirm-profile ${profile} --session ${token}\n`;
}

export function installCommand(profile, device, token, inventory) {
  validateRequest(profile, device, token);
  if (!INVENTORY_PATTERN.test(inventory)) {
    throw new RecoveryProtocolError("invalid NVMe inventory fingerprint");
  }
  return `helm-provision install --device ${device} --confirm-device ${device} --confirm-profile ${profile} --confirm-inventory ${inventory} --session ${token}\n`;
}

export function confirmationPhrase(profile, device, inventory) {
  validateRequest(profile, device, "0".repeat(32));
  if (!INVENTORY_PATTERN.test(inventory)) {
    throw new RecoveryProtocolError("invalid NVMe inventory fingerprint");
  }
  return `ERASE ${device} AND FLASH QSPI FOR ${profile} INVENTORY ${inventory.slice(0, 12)}`;
}

function validatePreflightMarker(marker, profile, device) {
  if (marker.kind === "failure") {
    throw new RecoveryProtocolError(
      `target preflight failed (target rc=${marker.returncode}); no persistent writes started`,
    );
  }
  if (
    marker.kind !== "preflight-success" ||
    marker.profile !== profile ||
    marker.device !== device ||
    !INVENTORY_PATTERN.test(marker.inventory ?? "")
  ) {
    throw new RecoveryProtocolError("target preflight identity does not match this request");
  }
}

function validateFinalMarker(marker, profile, device) {
  if (marker.kind === "failure") {
    throw new RecoveryProtocolError(`target provisioning failed (target rc=${marker.returncode})`);
  }
  if (
    marker.kind !== "success" ||
    marker.profile !== profile ||
    marker.device !== device
  ) {
    throw new RecoveryProtocolError("target did not return the requested final success marker");
  }
}

async function writeAndWaitForLine(transport, text, predicate, timeoutMs) {
  const controller = new AbortController();
  const pending = transport.waitForLine(predicate, {
    timeoutMs,
    signal: controller.signal,
  });
  // If the write itself fails, aborting the already-registered waiter must not
  // leave a detached rejecting promise behind.
  void pending.catch(() => {});
  try {
    await transport.writeAscii(text);
    return await pending;
  } finally {
    controller.abort();
  }
}

export class RecoverySession {
  #transport;
  #tokenFactory;
  #usedTokens = new Set();
  #preflight = null;
  #installAttempted = false;

  constructor(transport, { tokenFactory = randomSessionToken } = {}) {
    if (
      typeof transport?.writeAscii !== "function" ||
      typeof transport?.waitForLine !== "function"
    ) {
      throw new TypeError("RecoverySession requires a recovery transport");
    }
    this.#transport = transport;
    this.#tokenFactory = tokenFactory;
  }

  #nextToken() {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const token = this.#tokenFactory();
      validateRequest("helm-orin-nx-8gb-r39.2", "/dev/nvme0n1", token);
      if (!this.#usedTokens.has(token)) {
        this.#usedTokens.add(token);
        return token;
      }
    }
    throw new RecoveryProtocolError("could not create a unique recovery session token");
  }

  async waitUntilReady({ timeoutMs = 30_000, probeIntervalMs = 2_000 } = {}) {
    const token = this.#nextToken();
    const expected = `HELM_PROVISION_READY session=${token}`;
    const controller = new AbortController();
    const ready = this.#transport.waitForLine((line) => line === expected, {
      timeoutMs,
      signal: controller.signal,
    });
    void ready.catch(() => {});
    let writeFailureReject;
    const writeFailure = new Promise((_, reject) => {
      writeFailureReject = reject;
    });
    let active = true;
    let probeInFlight = false;
    const sendProbe = () => {
      if (!active || probeInFlight) {
        return;
      }
      probeInFlight = true;
      this.#transport
        .writeAscii(readyCommand(token))
        .catch((error) => {
          if (active) {
            writeFailureReject(error);
          }
        })
        .finally(() => {
          probeInFlight = false;
        });
    };
    sendProbe();
    const timer = setInterval(sendProbe, probeIntervalMs);
    try {
      await Promise.race([ready, writeFailure]);
    } finally {
      active = false;
      clearInterval(timer);
      controller.abort();
    }
  }

  async preflight(profile, device, { timeoutMs = 300_000 } = {}) {
    if (this.#preflight !== null) {
      throw new RecoveryProtocolError("this recovery session already completed preflight");
    }
    const token = this.#nextToken();
    validateRequest(profile, device, token);
    const markerLine = await writeAndWaitForLine(
      this.#transport,
      preflightCommand(profile, device, token),
      (line) => parseMarker(line)?.session === token,
      timeoutMs,
    );
    const marker = parseMarker(markerLine);
    if (marker === null) {
      throw new RecoveryProtocolError("internal marker parsing error");
    }
    validatePreflightMarker(marker, profile, device);
    this.#preflight = Object.freeze({
      profile,
      device,
      inventory: marker.inventory,
      confirmation: confirmationPhrase(profile, device, marker.inventory),
    });
    return this.#preflight;
  }

  async install(typedConfirmation, { timeoutMs = 1_800_000 } = {}) {
    if (this.#preflight === null) {
      throw new RecoveryProtocolError("read-only preflight must pass before install");
    }
    if (this.#installAttempted) {
      throw new RecoveryProtocolError("install was already attempted in this browser session");
    }
    if (typedConfirmation !== this.#preflight.confirmation) {
      throw new RecoveryProtocolError("confirmation did not match; no persistent writes were started");
    }

    const { profile, device, inventory } = this.#preflight;
    const token = this.#nextToken();
    validateRequest(profile, device, token);
    this.#installAttempted = true;
    try {
      // Once this write is attempted the browser cannot prove whether the
      // target shell received enough of the command to begin persistent I/O.
      const markerLine = await writeAndWaitForLine(
        this.#transport,
        installCommand(profile, device, token, inventory),
        (line) => parseMarker(line)?.session === token,
        timeoutMs,
      );
      const marker = parseMarker(markerLine);
      if (marker === null) {
        throw new RecoveryProtocolError("internal marker parsing error");
      }
      validateFinalMarker(marker, profile, device);
      return Object.freeze({ profile, device });
    } catch (error) {
      throw new PersistentStateUnknownError(
        "provisioning did not finish cleanly; leave Helm powered and inspect recovery before retrying",
        { cause: error },
      );
    }
  }
}
