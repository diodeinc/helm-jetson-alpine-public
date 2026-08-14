import {
  PROFILE_DEFINITIONS,
  RCM_ARTIFACTS,
  T234WebUsbRcm,
  profileById,
  validateRcmBundle,
} from "./modules/index.js";
import {
  PersistentStateUnknownError,
  RecoverySession,
} from "./modules/recovery-protocol.mjs";
import { WebSerialTransport } from "./modules/web-serial-transport.mjs";

const TARGET_DEVICE = "/dev/nvme0n1";
const SHARED_APX_PRODUCT_ID = 0x7523;
const MAXIMUM_LOG_LENGTH = 200_000;
const BUNDLE_NAMES = Object.freeze([
  ...RCM_ARTIFACTS.map(({ name }) => name),
  "PROFILE",
  "SHA256SUMS",
]);
const HASHED_BUNDLE_NAMES = new Set(BUNDLE_NAMES.filter((name) => name !== "SHA256SUMS"));

const elements = Object.freeze({
  singleBoardAck: requireElement("single-board-ack"),
  profileSelect: requireElement("profile-select"),
  sharedPidNote: requireElement("shared-pid-note"),
  prepareButton: requireElement("prepare-button"),
  bundleSummary: requireElement("bundle-summary"),
  bootButton: requireElement("boot-button"),
  recoveryButton: requireElement("recovery-button"),
  installButton: requireElement("install-button"),
  inventoryPanel: requireElement("inventory-panel"),
  inventoryDevice: requireElement("inventory-device"),
  inventoryProfile: requireElement("inventory-profile"),
  inventoryHash: requireElement("inventory-hash"),
  confirmationPanel: requireElement("confirmation-panel"),
  confirmationPhrase: requireElement("confirmation-phrase"),
  confirmationInput: requireElement("confirmation-input"),
  compatibilityAlert: requireElement("compatibility-alert"),
  compatibilityMessage: requireElement("compatibility-message"),
  statusTitle: requireElement("status-title"),
  statusDetail: requireElement("status-detail"),
  statusChip: requireElement("status-chip"),
  progressPanel: requireElement("progress-panel"),
  progressTitle: requireElement("progress-title"),
  progressPercent: requireElement("progress-percent"),
  progressBar: requireElement("progress-bar"),
  progressDetail: requireElement("progress-detail"),
  sessionLog: requireElement("session-log"),
  logCount: requireElement("log-count"),
});

const state = {
  catalogProfiles: new Map(),
  catalogReady: false,
  browserReady: false,
  selectedProfile: null,
  preparedBundle: null,
  busy: null,
  bootComplete: false,
  transport: null,
  recoverySession: null,
  preflight: null,
  recoveryDisconnected: false,
  installBoundaryCrossed: false,
  installComplete: false,
  persistentStateUnknown: false,
  logText: "",
  logEntries: 0,
};

function requireElement(id) {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`missing required page element: ${id}`);
  }
  return element;
}

function setFeature(name, result, label) {
  const row = document.querySelector(`[data-feature="${name}"]`);
  if (row === null) {
    return;
  }
  row.dataset.result = result ? "pass" : "fail";
  const value = row.querySelector("strong");
  if (value !== null) {
    value.textContent = label ?? (result ? "Ready" : "Missing");
  }
}

function setStatus(title, detail, tone = "ready", chip = "Ready") {
  elements.statusTitle.textContent = title;
  elements.statusDetail.textContent = detail;
  elements.statusChip.dataset.tone = tone;
  elements.statusChip.lastChild.textContent = chip;
}

function setStep(id, stepState, label) {
  const step = requireElement(id);
  step.dataset.state = stepState;
  const stateLabel = step.querySelector(".step-state");
  if (stateLabel !== null) {
    stateLabel.textContent = label;
  }
}

function showProgress(title, percent, detail) {
  const bounded = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  elements.progressPanel.hidden = false;
  elements.progressTitle.textContent = title;
  elements.progressBar.value = bounded;
  elements.progressBar.textContent = `${Math.round(bounded)}%`;
  elements.progressPercent.textContent = `${Math.round(bounded)}%`;
  elements.progressDetail.textContent = detail;
}

function showIndeterminateProgress(title, detail) {
  elements.progressPanel.hidden = false;
  elements.progressTitle.textContent = title;
  elements.progressBar.removeAttribute("value");
  elements.progressBar.textContent = "Working";
  elements.progressPercent.textContent = "Working";
  elements.progressDetail.textContent = detail;
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) {
    return "unknown size";
  }
  const units = ["B", "KiB", "MiB", "GiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

function sanitizeLogText(value) {
  let output = "";
  for (const character of String(value)) {
    const codepoint = character.codePointAt(0);
    if (
      character === "\n" ||
      character === "\r" ||
      character === "\t" ||
      (codepoint >= 0x20 && codepoint <= 0x7e)
    ) {
      output += character;
    } else {
      output += `\\x${codepoint.toString(16).padStart(2, "0")}`;
    }
  }
  return output;
}

function appendLog(message, level = "info") {
  const timestamp = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const clean = sanitizeLogText(message).replace(/\s+$/u, "");
  const entry = `[${timestamp}] ${level.toUpperCase().padEnd(6)} ${clean}\n`;
  state.logText += entry;
  if (state.logText.length > MAXIMUM_LOG_LENGTH) {
    state.logText = `[older output removed]\n${state.logText.slice(-MAXIMUM_LOG_LENGTH)}`;
  }
  state.logEntries += 1;
  elements.sessionLog.textContent = state.logText;
  elements.sessionLog.scrollTop = elements.sessionLog.scrollHeight;
  elements.logCount.textContent = `${state.logEntries} ${state.logEntries === 1 ? "entry" : "entries"}`;
}

function formatError(error) {
  if (error instanceof DOMException && error.name === "NotFoundError") {
    return "The browser chooser was closed without selecting a device.";
  }
  if (error instanceof Error && error.message.length > 0) {
    return error.message;
  }
  return "An unexpected provisioning error occurred.";
}

function parseProductId(value) {
  if (Number.isInteger(value)) {
    return value;
  }
  if (typeof value === "string" && /^(?:0x[0-9a-f]+|[0-9]+)$/i.test(value)) {
    return Number.parseInt(value, value.toLowerCase().startsWith("0x") ? 16 : 10);
  }
  throw new Error(`invalid catalog USB product ID: ${String(value)}`);
}

function sameOriginUrl(value, description) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`catalog is missing ${description}`);
  }
  const url = new URL(value, window.location.href);
  if (url.origin !== window.location.origin || url.username !== "" || url.password !== "") {
    throw new Error(`${description} must stay on this HTTPS origin`);
  }
  return url.href;
}

function normalizeCatalog(catalog) {
  if (catalog?.version !== 1 || !Array.isArray(catalog.profiles)) {
    throw new Error("catalog.json must use Helm catalog version 1");
  }
  if (catalog.profiles.length !== PROFILE_DEFINITIONS.length) {
    throw new Error(`catalog must contain exactly ${PROFILE_DEFINITIONS.length} supported profiles`);
  }

  const profiles = new Map();
  for (const entry of catalog.profiles) {
    if (typeof entry?.id !== "string" || profiles.has(entry.id)) {
      throw new Error("catalog contains a missing or duplicate profile ID");
    }
    const canonical = profileById(entry.id);
    const productId = parseProductId(entry.productId);
    if (productId !== canonical.productId || entry.board !== canonical.board || entry.sku !== canonical.sku) {
      throw new Error(`catalog identity does not match built-in profile ${entry.id}`);
    }
    if (!Array.isArray(entry.files) || entry.files.length !== BUNDLE_NAMES.length) {
      throw new Error(`${entry.id} must contain exactly ${BUNDLE_NAMES.length} bundle files`);
    }

    const files = new Map();
    for (const file of entry.files) {
      const name = file?.name;
      if (!BUNDLE_NAMES.includes(name) || files.has(name)) {
        throw new Error(`${entry.id} contains an unexpected or duplicate file: ${String(name)}`);
      }
      if (!Number.isSafeInteger(file.size) || file.size <= 0) {
        throw new Error(`${entry.id}/${name} has an invalid size`);
      }
      if (typeof file.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(file.sha256)) {
        throw new Error(`${entry.id}/${name} has an invalid SHA-256 digest`);
      }
      files.set(name, Object.freeze({
        name,
        size: file.size,
        sha256: file.sha256,
        url: sameOriginUrl(file.url, `${entry.id}/${name} URL`),
      }));
    }
    for (const name of BUNDLE_NAMES) {
      if (!files.has(name)) {
        throw new Error(`${entry.id} is missing ${name}`);
      }
    }
    profiles.set(entry.id, Object.freeze({
      ...canonical,
      path: entry.path,
      files: Object.freeze(BUNDLE_NAMES.map((name) => files.get(name))),
    }));
  }

  for (const definition of PROFILE_DEFINITIONS) {
    if (!profiles.has(definition.id)) {
      throw new Error(`catalog is missing ${definition.id}`);
    }
  }
  return profiles;
}

function renderProfileOptions() {
  elements.profileSelect.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select the exact module SKU…";
  elements.profileSelect.append(placeholder);
  for (const definition of PROFILE_DEFINITIONS) {
    const profile = state.catalogProfiles.get(definition.id);
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = `${profile.label} · ${profile.board}`;
    elements.profileSelect.append(option);
  }
  elements.profileSelect.disabled = false;
}

function selectedCatalogProfile() {
  const profile = state.catalogProfiles.get(elements.profileSelect.value);
  return profile ?? null;
}

function clearPreparedBundle() {
  state.preparedBundle = null;
  state.selectedProfile = selectedCatalogProfile();
  setStep("step-prepare", "current", "Ready");
  setStep("step-boot", "locked", "Locked");
  elements.progressPanel.hidden = true;
}

function handleProfileChange() {
  if (state.bootComplete || state.busy !== null) {
    return;
  }
  clearPreparedBundle();
  const profile = state.selectedProfile;
  elements.sharedPidNote.hidden = profile?.productId !== SHARED_APX_PRODUCT_ID;
  if (profile === null) {
    elements.bundleSummary.textContent = "No profile selected";
    setStatus("Select the exact Helm profile", "The browser will verify it against the APX USB identity.", "ready", "Ready");
  } else {
    const bytes = profile.files.reduce((total, file) => total + file.size, 0);
    elements.bundleSummary.textContent = `${profile.board} · ${formatBytes(bytes)} download`;
    setStatus(
      "Profile selected",
      `${profile.label} (${profile.board}) is ready to download and verify.`,
      "ready",
      "Ready",
    );
    appendLog(`Selected ${profile.id} (${profile.board}, APX 0955:${profile.productId.toString(16).padStart(4, "0")}).`);
  }
  updateControls();
}

function updateControls() {
  const idle = state.busy === null;
  const acknowledged = elements.singleBoardAck.checked;
  const supported = state.browserReady && state.catalogReady;
  elements.profileSelect.disabled = !state.catalogReady || !idle || state.bootComplete;
  elements.prepareButton.disabled = !supported || !idle || !acknowledged || state.selectedProfile === null || state.preparedBundle !== null || state.bootComplete;
  elements.bootButton.disabled = !supported || !idle || !acknowledged || state.preparedBundle === null || state.bootComplete;
  elements.recoveryButton.disabled = !supported || !idle || !state.bootComplete || state.preflight !== null || state.persistentStateUnknown;
  const exactConfirmation = state.preflight !== null &&
    elements.confirmationInput.value === state.preflight.confirmation;
  elements.confirmationInput.dataset.match = String(exactConfirmation);
  elements.confirmationInput.disabled = !idle || state.preflight === null || state.installBoundaryCrossed;
  elements.installButton.disabled = !idle || !exactConfirmation || state.installBoundaryCrossed || state.recoveryDisconnected;
}

async function downloadBundleFile(file, completedBytes, totalBytes) {
  const response = await fetch(file.url, {
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error(`download failed for ${file.name} (HTTP ${response.status})`);
  }
  if (new URL(response.url).origin !== window.location.origin) {
    throw new Error(`download escaped this HTTPS origin: ${file.name}`);
  }

  let received = 0;
  let blob;
  if (response.body !== null) {
    const reader = response.body.getReader();
    const chunks = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      received += value.byteLength;
      if (received > file.size) {
        await reader.cancel();
        throw new Error(`${file.name} is larger than the authenticated catalog entry`);
      }
      chunks.push(value);
      const percent = ((completedBytes + received) / totalBytes) * 100;
      showProgress(
        "Downloading recovery bundle",
        percent,
        `${file.name} · ${formatBytes(received)} of ${formatBytes(file.size)}`,
      );
    }
    blob = new Blob(chunks, { type: "application/octet-stream" });
  } else {
    blob = await response.blob();
    received = blob.size;
  }
  if (received !== file.size || blob.size !== file.size) {
    throw new Error(`${file.name} is ${formatBytes(blob.size)}; expected ${formatBytes(file.size)}`);
  }
  return new File([blob], file.name, { type: "application/octet-stream" });
}

async function downloadAndValidate(profile) {
  const totalBytes = profile.files.reduce((total, file) => total + file.size, 0);
  const downloadedFiles = [];
  let completedBytes = 0;
  for (const file of profile.files) {
    appendLog(`Downloading ${file.name} (${formatBytes(file.size)}).`);
    const downloaded = await downloadBundleFile(file, completedBytes, totalBytes);
    downloadedFiles.push(downloaded);
    completedBytes += file.size;
  }

  const trustedChecksums = Object.fromEntries(
    profile.files
      .filter(({ name }) => HASHED_BUNDLE_NAMES.has(name))
      .map(({ name, sha256 }) => [name, sha256]),
  );
  let hashingFile = null;
  const bundle = await validateRcmBundle(downloadedFiles, {
    expectedProfileId: profile.id,
    trustedChecksums,
    onProgress(progress) {
      if (progress.file !== hashingFile) {
        hashingFile = progress.file;
        appendLog(`Verifying SHA-256 for ${progress.file}.`);
      }
      const percent = (progress.bundleBytesHashed / progress.bundleBytesTotal) * 100;
      showProgress(
        "Verifying recovery bundle",
        percent,
        `${progress.file} · ${formatBytes(progress.fileBytesHashed)} of ${formatBytes(progress.fileBytesTotal)}`,
      );
    },
  });
  if (bundle.profile.id !== profile.id || bundle.profile.productId !== profile.productId) {
    throw new Error("validated bundle identity changed unexpectedly");
  }
  return bundle;
}

async function handlePrepare() {
  const profile = state.selectedProfile;
  if (profile === null || state.busy !== null || !elements.singleBoardAck.checked) {
    return;
  }
  state.busy = "prepare";
  state.preparedBundle = null;
  setStep("step-prepare", "working", "Verifying");
  setStatus("Downloading and verifying", "No USB access is requested during file preparation.", "working", "Working");
  showProgress("Downloading recovery bundle", 0, "Starting authenticated same-origin download");
  appendLog(`Preparing trusted bundle for ${profile.id}.`);
  updateControls();
  try {
    const bundle = await downloadAndValidate(profile);
    state.preparedBundle = bundle;
    setStep("step-prepare", "complete", "Verified");
    setStep("step-boot", "current", "Ready");
    showProgress("Recovery bundle verified", 100, `${formatBytes(bundle.totalBytes)} of signed RCM artifacts are ready in browser memory`);
    setStatus("Bundle verified", "Put Helm in force-recovery mode, then choose its APX device.", "success", "Verified");
    appendLog(`Bundle verified for ${bundle.profile.id}; USB transfer has not started.`, "ok");
  } catch (error) {
    state.preparedBundle = null;
    setStep("step-prepare", "failed", "Failed");
    setStatus("Bundle preparation failed", `${formatError(error)} No device or persistent storage was changed.`, "danger", "Failed");
    appendLog(formatError(error), "error");
  } finally {
    state.busy = null;
    updateControls();
  }
}

function handleRcmProgress(progress) {
  if (progress.type === "phase") {
    showIndeterminateProgress("RAM-booting recovery", progress.message);
    appendLog(progress.message);
    return;
  }
  if (progress.type === "transfer") {
    const percent = (progress.bundleBytesSent / progress.bundleBytesTotal) * 100;
    showProgress(
      "RAM-booting recovery",
      percent,
      `${progress.file} · ${formatBytes(progress.artifactBytesSent)} of ${formatBytes(progress.artifactBytesTotal)}`,
    );
    return;
  }
  if (progress.type === "cid") {
    appendLog(`BootROM CID: ${progress.cid}`);
    return;
  }
  if (progress.type === "banner") {
    appendLog(`Bootloader: ${progress.version || "unknown"}; last boot error ${progress.lastBootError}.`);
  }
}

async function handleBoot() {
  if (state.preparedBundle === null || state.busy !== null || !elements.singleBoardAck.checked) {
    return;
  }
  const bundle = state.preparedBundle;
  state.busy = "boot";
  setStep("step-boot", "working", "Choose APX");
  setStatus("Choose the matching APX device", "The browser chooser is the physical-device trust boundary.", "working", "USB grant");
  appendLog(`Requesting APX 0955:${bundle.profile.productId.toString(16).padStart(4, "0")} for ${bundle.profile.board}.`);
  updateControls();
  const rcm = new T234WebUsbRcm({ usb: navigator.usb });
  try {
    // Keep requestDevice as the first awaited operation in this click handler:
    // WebUSB permission requires a live, transient user activation.
    const device = await rcm.requestDevice(bundle);
    appendLog(`Authorized APX 0955:${device.productId.toString(16).padStart(4, "0")}.`);
    setStatus("RAM boot in progress", "Keep power and USB connected while APX re-enumerates.", "working", "Transferring");
    const result = await rcm.boot(device, bundle, { onProgress: handleRcmProgress });
    state.bootComplete = true;
    state.preparedBundle = null;
    setStep("step-boot", "complete", "RAM booted");
    setStep("step-recovery", "current", "Ready");
    showProgress("Recovery RAM boot complete", 100, `${formatBytes(result.bytesSent)} transferred; waiting for Helm recovery USB`);
    setStatus("Recovery is booting", "When Chrome lists Helm recovery 0955:7020, grant serial access for read-only preflight.", "success", "RAM booted");
    appendLog(`RAM boot complete (${formatBytes(result.bytesSent)}). No persistent writes started.`, "ok");
  } catch (error) {
    const cancelled = error instanceof DOMException && error.name === "NotFoundError";
    setStep("step-boot", "current", cancelled ? "Ready" : "Failed");
    setStatus(
      cancelled ? "APX chooser closed" : "RAM boot did not complete",
      `${formatError(error)} No persistent writes were started.`,
      cancelled ? "warning" : "danger",
      cancelled ? "No selection" : "Failed",
    );
    appendLog(`${formatError(error)} No persistent writes were started.`, cancelled ? "warn" : "error");
  } finally {
    state.busy = null;
    updateControls();
  }
}

async function closeRecoveryBestEffort() {
  const transport = state.transport;
  state.transport = null;
  state.recoverySession = null;
  if (transport !== null) {
    try {
      await transport.close();
    } catch (error) {
      appendLog(`Recovery console close warning: ${formatError(error)}`, "warn");
    }
  }
}

function revealPreflight(preflight) {
  elements.inventoryDevice.textContent = preflight.device;
  elements.inventoryProfile.textContent = preflight.profile;
  elements.inventoryHash.textContent = preflight.inventory;
  elements.confirmationPhrase.textContent = preflight.confirmation;
  elements.confirmationInput.value = "";
  elements.inventoryPanel.hidden = false;
  elements.confirmationPanel.hidden = false;
}

async function handleRecovery() {
  if (!state.bootComplete || state.busy !== null || state.preflight !== null) {
    return;
  }
  state.busy = "recovery";
  state.recoveryDisconnected = false;
  setStep("step-recovery", "working", "Choose port");
  setStatus("Choose Helm recovery", "Chrome will request the second and final hardware grant.", "working", "Serial grant");
  showIndeterminateProgress("Connecting to recovery", "Waiting for authorized Helm recovery 0955:7020");
  appendLog("Requesting recovery serial 0955:7020.");
  updateControls();
  const transport = new WebSerialTransport({
    serial: navigator.serial,
    onOutput(output) {
      appendLog(output, "target");
    },
  });
  state.transport = transport;
  try {
    // requestAndOpen() invokes requestPort() before its first await, preserving
    // the user activation from this button click.
    await transport.requestAndOpen();
    appendLog("Recovery serial port authorized and open.", "ok");
    const session = new RecoverySession(transport);
    state.recoverySession = session;
    setStatus("Waiting for the recovery shell", "The target may take a minute to initialize USB and NVMe.", "working", "Connecting");
    await session.waitUntilReady({ timeoutMs: 120_000, probeIntervalMs: 2_000 });
    appendLog("Recovery shell is ready; starting read-only preflight.", "ok");
    setStatus("Inspecting the target", `Checking ${TARGET_DEVICE} and the selected profile without writing.`, "working", "Preflight");
    showIndeterminateProgress("Read-only preflight", `Fingerprinting ${TARGET_DEVICE}; persistent writes remain disabled`);
    const preflight = await session.preflight(state.selectedProfile.id, TARGET_DEVICE);
    state.preflight = preflight;
    revealPreflight(preflight);
    setStep("step-recovery", "complete", "Inspected");
    setStep("step-install", "current", "Confirmation");
    showProgress("Read-only preflight complete", 100, `${TARGET_DEVICE} fingerprint ${preflight.inventory.slice(0, 12)}…`);
    setStatus("Target verified — review before erasing", "Persistent writes are still disabled until the exact phrase and red button.", "warning", "Awaiting phrase");
    appendLog(`Preflight passed: profile=${preflight.profile}, device=${preflight.device}, inventory=${preflight.inventory}.`, "ok");
  } catch (error) {
    const cancelled = error instanceof DOMException && error.name === "NotFoundError";
    setStep("step-recovery", "failed", cancelled ? "No selection" : "Failed");
    setStatus(
      cancelled ? "Recovery chooser closed" : "Recovery preflight failed",
      `${formatError(error)} No persistent writes were started.`,
      cancelled ? "warning" : "danger",
      cancelled ? "No selection" : "Failed",
    );
    appendLog(`${formatError(error)} No persistent writes were started.`, cancelled ? "warn" : "error");
    await closeRecoveryBestEffort();
  } finally {
    state.busy = null;
    updateControls();
  }
}

function markPersistentStateUnknown(reason) {
  if (state.installComplete || state.persistentStateUnknown) {
    return;
  }
  state.persistentStateUnknown = true;
  setStep("step-install", "failed", "State unknown");
  setStatus(
    "Persistent state is unknown",
    `${reason} Leave Helm powered and inspect recovery before any retry.`,
    "danger",
    "Do not retry",
  );
  showIndeterminateProgress("Manual recovery required", "Keep power connected; do not assume NVMe or QSPI is complete");
  appendLog(`${reason} Persistent state is unknown; keep Helm powered and do not retry blindly.`, "fatal");
  updateControls();
}

async function handleInstall() {
  if (
    state.preflight === null ||
    state.recoverySession === null ||
    state.busy !== null ||
    state.installBoundaryCrossed ||
    elements.confirmationInput.value !== state.preflight.confirmation
  ) {
    return;
  }
  const typedConfirmation = elements.confirmationInput.value;
  state.busy = "install";
  // From this point onward the page must conservatively assume that the target
  // may receive enough of the command to begin persistent storage writes.
  state.installBoundaryCrossed = true;
  setStep("step-install", "working", "Writing");
  setStatus("Erasing NVMe and flashing QSPI", "Do not disconnect USB or power. Persistent writes are in progress.", "warning", "Writing");
  showIndeterminateProgress("Provisioning persistent storage", "Target output is visible in the session log; this can take several minutes");
  appendLog("EXACT CONFIRMATION ACCEPTED. Persistent provisioning command is being sent.", "write");
  updateControls();
  try {
    const result = await state.recoverySession.install(typedConfirmation);
    state.installComplete = true;
    setStep("step-install", "complete", "Complete");
    showProgress("Helm provisioning complete", 100, `${result.profile} installed on ${result.device}; target returned the session-bound success marker`);
    setStatus("Helm is provisioned", "The target reported a clean final success marker. It is safe to follow the documented reboot procedure.", "success", "Complete");
    appendLog(`Provisioning succeeded for ${result.profile} on ${result.device}.`, "ok");
    await closeRecoveryBestEffort();
  } catch (error) {
    const message = error instanceof PersistentStateUnknownError
      ? error.message
      : formatError(error);
    markPersistentStateUnknown(message);
  } finally {
    state.busy = null;
    updateControls();
  }
}

function handleSerialDisconnect() {
  if (state.transport === null || state.installComplete) {
    return;
  }
  state.recoveryDisconnected = true;
  if (state.installBoundaryCrossed) {
    markPersistentStateUnknown("The recovery USB connection disconnected after the write boundary.");
    return;
  }
  if (state.preflight !== null) {
    setStep("step-recovery", "failed", "Disconnected");
    setStep("step-install", "locked", "Locked");
    setStatus("Recovery disconnected", "Reconnect and RAM boot again before provisioning. No persistent writes were started.", "danger", "Disconnected");
    appendLog("Recovery serial disconnected before install. No persistent writes were started.", "error");
    updateControls();
  }
}

function installEventHandlers() {
  elements.singleBoardAck.addEventListener("change", () => {
    appendLog(elements.singleBoardAck.checked
      ? "Operator confirmed exactly one Helm is connected."
      : "Single-board confirmation cleared.");
    updateControls();
  });
  elements.profileSelect.addEventListener("change", handleProfileChange);
  elements.prepareButton.addEventListener("click", handlePrepare);
  elements.bootButton.addEventListener("click", handleBoot);
  elements.recoveryButton.addEventListener("click", handleRecovery);
  elements.confirmationInput.addEventListener("input", updateControls);
  elements.installButton.addEventListener("click", handleInstall);
  navigator.serial?.addEventListener?.("disconnect", handleSerialDisconnect);
  window.addEventListener("beforeunload", (event) => {
    if (state.installBoundaryCrossed && !state.installComplete) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

async function initialize() {
  installEventHandlers();
  appendLog("Helm browser flasher initialized.");

  const secure = window.isSecureContext === true;
  const usb = typeof navigator.usb?.requestDevice === "function";
  const serial = typeof navigator.serial?.requestPort === "function";
  const fileApi = typeof File === "function" && typeof Blob === "function";
  const secureRandom = typeof globalThis.crypto?.getRandomValues === "function";
  setFeature("secure", secure, secure ? "Ready" : "Required");
  setFeature("usb", usb, usb ? "Ready" : "Missing");
  setFeature("serial", serial, serial ? "Ready" : "Missing");
  state.browserReady = secure && usb && serial && fileApi && secureRandom;

  if (!state.browserReady) {
    const reasons = [];
    if (!secure) reasons.push("open this page from its trusted HTTPS address");
    if (!usb) reasons.push("WebUSB is unavailable");
    if (!serial) reasons.push("Web Serial is unavailable");
    if (!fileApi || !secureRandom) reasons.push("required secure browser APIs are unavailable");
    elements.compatibilityAlert.hidden = false;
    elements.compatibilityMessage.textContent = `${reasons.join("; ")}. Use current desktop Chrome or Chromium.`;
    setStatus("Unsupported browser context", "Use current desktop Chrome or Chromium on this trusted HTTPS origin.", "danger", "Blocked");
    appendLog(`Browser compatibility failed: ${reasons.join("; ")}.`, "error");
  }

  try {
    const response = await fetch("./catalog.json", {
      cache: "no-store",
      credentials: "same-origin",
      redirect: "error",
    });
    if (!response.ok) {
      throw new Error(`catalog request returned HTTP ${response.status}`);
    }
    if (new URL(response.url).origin !== window.location.origin) {
      throw new Error("catalog request escaped this HTTPS origin");
    }
    const catalog = await response.json();
    state.catalogProfiles = normalizeCatalog(catalog);
    state.catalogReady = true;
    setFeature("catalog", true, "Ready");
    renderProfileOptions();
    appendLog(`Catalog loaded with ${state.catalogProfiles.size} verified profiles.`, "ok");
    if (state.browserReady) {
      setStatus("Ready to prepare a Helm", "Confirm that exactly one board is connected, then choose its exact module profile.", "ready", "Ready");
    }
  } catch (error) {
    setFeature("catalog", false, "Invalid");
    elements.compatibilityAlert.hidden = false;
    elements.compatibilityMessage.textContent = `Firmware catalog validation failed: ${formatError(error)}`;
    setStatus("Firmware catalog unavailable", "Provisioning is blocked before any USB or storage operation.", "danger", "Blocked");
    appendLog(`Catalog validation failed: ${formatError(error)}`, "error");
  } finally {
    updateControls();
  }
}

void initialize();
