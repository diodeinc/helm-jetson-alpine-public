import { sha256File } from "./sha256.js";

export const NVIDIA_VENDOR_ID = 0x0955;

export const RCM_BOOTROM_ARTIFACTS = Object.freeze([
  Object.freeze({ kind: "bct_br", name: "br_bct_BR.bct" }),
  Object.freeze({ kind: "mb1", name: "mb1_t234_prod_aligned_sigheader.bin.encrypt" }),
  Object.freeze({ kind: "psc_bl1", name: "psc_bl1_t234_prod_aligned_sigheader.bin.encrypt" }),
  Object.freeze({ kind: "bct_mb1", name: "mb1_bct_MB1_sigheader.bct.encrypt" }),
]);

export const RCM_BOOTLOADER_ARTIFACTS = Object.freeze([
  Object.freeze({ kind: "bct_mem", name: "mem_rcm_sigheader.bct.encrypt" }),
  Object.freeze({ kind: "blob", name: "blob.bin" }),
]);

export const RCM_ARTIFACTS = Object.freeze([
  ...RCM_BOOTROM_ARTIFACTS,
  ...RCM_BOOTLOADER_ARTIFACTS,
]);

const PROFILE_METADATA_NAME = "PROFILE";
const CHECKSUMS_NAME = "SHA256SUMS";
const VALIDATED_BUNDLE = Symbol("validated Helm RCM bundle");
const HASHED_BUNDLE_NAMES = Object.freeze([
  ...RCM_ARTIFACTS.map(({ name }) => name),
  PROFILE_METADATA_NAME,
]);
const ALL_BUNDLE_NAMES = Object.freeze([...HASHED_BUNDLE_NAMES, CHECKSUMS_NAME]);

export const RCM_PROFILE_METADATA_NAME = PROFILE_METADATA_NAME;
export const RCM_CHECKSUMS_NAME = CHECKSUMS_NAME;
export const RCM_HASHED_BUNDLE_NAMES = HASHED_BUNDLE_NAMES;
export const RCM_BUNDLE_FILE_NAMES = ALL_BUNDLE_NAMES;

function profile(definition) {
  return Object.freeze({
    ...definition,
    bundleProfile: `${definition.id}-ram-recovery`,
    kernelDtb: `helm-p3767-${definition.sku}.dtb`,
  });
}

// There are five supported module profiles but four distinct APX product IDs:
// P3767-0003 and P3767-0005 both enumerate as 0955:7523.
export const PROFILE_DEFINITIONS = Object.freeze([
  profile({
    id: "helm-orin-nx-16gb-r39.2",
    board: "P3767-0000",
    sku: "0000",
    productId: 0x7323,
    label: "Jetson Orin NX 16GB",
  }),
  profile({
    id: "helm-orin-nx-8gb-r39.2",
    board: "P3767-0001",
    sku: "0001",
    productId: 0x7423,
    label: "Jetson Orin NX 8GB",
  }),
  profile({
    id: "helm-orin-nano-8gb-r39.2",
    board: "P3767-0003",
    sku: "0003",
    productId: 0x7523,
    label: "Jetson Orin Nano 8GB",
  }),
  profile({
    id: "helm-orin-nano-4gb-r39.2",
    board: "P3767-0004",
    sku: "0004",
    productId: 0x7623,
    label: "Jetson Orin Nano 4GB",
  }),
  profile({
    id: "helm-orin-nano-8gb-sd-r39.2",
    board: "P3767-0005",
    sku: "0005",
    productId: 0x7523,
    label: "Jetson Orin Nano 8GB dev-kit/SD",
  }),
]);

export const SUPPORTED_PRODUCT_IDS = Object.freeze(
  Array.from(new Set(PROFILE_DEFINITIONS.map(({ productId }) => productId))),
);

const PROFILES_BY_ID = new Map(PROFILE_DEFINITIONS.map((entry) => [entry.id, entry]));
const PROFILES_BY_PRODUCT_ID = new Map(SUPPORTED_PRODUCT_IDS.map((productId) => [
  productId,
  Object.freeze(PROFILE_DEFINITIONS.filter((entry) => entry.productId === productId)),
]));
const NO_PROFILES = Object.freeze([]);
const PROFILE_KEYS = Object.freeze([
  "PROFILE",
  "BSP_RELEASE",
  "BOARD",
  "CARRIER",
  "SIGNING",
  "HOST_BUILD",
  "HOST_BOOT_PERSISTENT_WRITE",
  "TARGET_INSTALLERS",
  "USB_PID",
  "QSPI_PROFILE",
  "KERNEL_DTB",
]);

export class BundleValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "BundleValidationError";
  }
}

function fail(message) {
  throw new BundleValidationError(message);
}

function requireFileShape(file) {
  if (typeof file?.name !== "string" || file.name.length === 0 ||
      !Number.isSafeInteger(file.size) || file.size <= 0 ||
      typeof file.slice !== "function") {
    fail("bundle entries must be named, non-empty File-compatible objects");
  }
}

function normalizeFiles(input) {
  const files = Array.from(input ?? []);
  if (files.length !== ALL_BUNDLE_NAMES.length) {
    fail(
      `bundle must contain exactly ${ALL_BUNDLE_NAMES.length} files ` +
      `(six wire artifacts, PROFILE, and SHA256SUMS); found ${files.length}`,
    );
  }

  const byName = new Map();
  let directory = null;
  for (const file of files) {
    requireFileShape(file);
    const relativePath = file.webkitRelativePath || file.name;
    if (typeof relativePath !== "string" || relativePath.includes("\\")) {
      fail(`invalid bundle path for ${file.name}`);
    }
    const parts = relativePath.split("/");
    if (parts.some((part) => part.length === 0 || part === "." || part === "..")) {
      fail(`invalid bundle path: ${relativePath}`);
    }
    const name = parts.at(-1);
    const parent = parts.slice(0, -1).join("/");
    if (name !== file.name) {
      fail(`bundle path basename does not match File.name: ${relativePath}`);
    }
    if (directory === null) {
      directory = parent;
    } else if (directory !== parent) {
      fail("all bundle files must come from the same directory");
    }
    if (!ALL_BUNDLE_NAMES.includes(name)) {
      fail(`unexpected bundle file: ${name}`);
    }
    if (byName.has(name)) {
      fail(`duplicate bundle file: ${name}`);
    }
    byName.set(name, file);
  }

  for (const name of ALL_BUNDLE_NAMES) {
    if (!byName.has(name)) {
      fail(`missing bundle file: ${name}`);
    }
  }
  return byName;
}

async function readSmallText(file, maximumBytes) {
  if (file.size > maximumBytes) {
    fail(`${file.name} exceeds the ${maximumBytes}-byte safety limit`);
  }
  const bytes = new Uint8Array(await file.slice(0, file.size).arrayBuffer());
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail(`${file.name} is not valid UTF-8`);
  }
}

function nonEmptyLines(text, filename) {
  if (text.includes("\r") || !text.endsWith("\n")) {
    fail(`${filename} must use LF line endings and end with a newline`);
  }
  const lines = text.slice(0, -1).split("\n");
  if (lines.length === 0 || lines.some((line) => line.length === 0)) {
    fail(`${filename} contains an empty line`);
  }
  return lines;
}

function parseProfile(text) {
  const values = Object.create(null);
  for (const line of nonEmptyLines(text, PROFILE_METADATA_NAME)) {
    const match = /^([A-Z][A-Z0-9_]*)=([^\0\n]+)$/.exec(line);
    if (!match) {
      fail(`invalid PROFILE line: ${line}`);
    }
    const [, key, value] = match;
    if (Object.hasOwn(values, key)) {
      fail(`duplicate PROFILE key: ${key}`);
    }
    values[key] = value;
  }

  const keys = Object.keys(values);
  if (keys.length !== PROFILE_KEYS.length ||
      PROFILE_KEYS.some((key) => !Object.hasOwn(values, key))) {
    fail(`PROFILE must contain exactly: ${PROFILE_KEYS.join(", ")}`);
  }

  const definition = PROFILE_DEFINITIONS.find(
    ({ bundleProfile }) => bundleProfile === values.PROFILE,
  );
  if (!definition) {
    fail(`unsupported bundle profile: ${values.PROFILE}`);
  }

  const expected = {
    PROFILE: definition.bundleProfile,
    BSP_RELEASE: "39.2",
    BOARD: definition.board,
    CARRIER: "Diode_Helm",
    SIGNING: "zerosbk",
    HOST_BUILD: "macos-native",
    HOST_BOOT_PERSISTENT_WRITE: "none",
    TARGET_INSTALLERS: "guarded-nvme-and-qspi",
    USB_PID: `0x${definition.productId.toString(16).padStart(4, "0")}`,
    QSPI_PROFILE: definition.id,
    KERNEL_DTB: definition.kernelDtb,
  };
  for (const key of PROFILE_KEYS) {
    if (values[key] !== expected[key]) {
      fail(`PROFILE ${key} is ${values[key]}; expected ${expected[key]}`);
    }
  }
  return { values: Object.freeze({ ...values }), definition };
}

function parseChecksums(text) {
  const checksums = Object.create(null);
  for (const line of nonEmptyLines(text, CHECKSUMS_NAME)) {
    const match = /^([0-9a-f]{64})  ([A-Za-z0-9._+-]+)$/.exec(line);
    if (!match) {
      fail(`invalid SHA256SUMS line: ${line}`);
    }
    const [, digest, name] = match;
    if (!HASHED_BUNDLE_NAMES.includes(name)) {
      fail(`SHA256SUMS covers an unexpected file: ${name}`);
    }
    if (Object.hasOwn(checksums, name)) {
      fail(`SHA256SUMS repeats: ${name}`);
    }
    checksums[name] = digest;
  }
  if (Object.keys(checksums).length !== HASHED_BUNDLE_NAMES.length ||
      HASHED_BUNDLE_NAMES.some((name) => !Object.hasOwn(checksums, name))) {
    fail("SHA256SUMS must cover each wire artifact and PROFILE exactly once");
  }
  return Object.freeze({ ...checksums });
}

function normalizeTrustedChecksums(value) {
  if (value === undefined || value === null) {
    return null;
  }
  const entries = value instanceof Map ? Array.from(value.entries()) : Object.entries(value);
  const normalized = Object.create(null);
  for (const [name, digest] of entries) {
    if (!HASHED_BUNDLE_NAMES.includes(name) ||
        typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) {
      fail(`invalid trusted checksum entry: ${name}`);
    }
    normalized[name] = digest;
  }
  if (Object.keys(normalized).length !== HASHED_BUNDLE_NAMES.length ||
      HASHED_BUNDLE_NAMES.some((name) => !Object.hasOwn(normalized, name))) {
    fail("trusted checksums must cover each wire artifact and PROFILE exactly once");
  }
  return normalized;
}

export function profileById(profileId) {
  const definition = PROFILES_BY_ID.get(profileId);
  if (!definition) {
    throw new BundleValidationError(`unsupported Helm profile: ${profileId}`);
  }
  return definition;
}

export function profilesByProductId(productId) {
  return PROFILES_BY_PRODUCT_ID.get(productId) ?? NO_PROFILES;
}

export function uniqueProfileByProductId(productId) {
  const profiles = profilesByProductId(productId);
  return profiles.length === 1 ? profiles[0] : null;
}

export function isValidatedRcmBundle(value) {
  return value?.[VALIDATED_BUNDLE] === true;
}

export async function validateRcmBundle(input, options = {}) {
  const files = normalizeFiles(input);
  const profileText = await readSmallText(files.get(PROFILE_METADATA_NAME), 16 * 1024);
  const checksumText = await readSmallText(files.get(CHECKSUMS_NAME), 64 * 1024);
  const { values: metadata, definition } = parseProfile(profileText);
  const checksums = parseChecksums(checksumText);
  const trustedChecksums = normalizeTrustedChecksums(options.trustedChecksums);

  if (options.expectedProfileId !== undefined &&
      definition.id !== options.expectedProfileId) {
    fail(
      `selected profile ${options.expectedProfileId} does not match bundle ` +
      `${definition.id}`,
    );
  }
  const profilesForPid = PROFILE_DEFINITIONS.filter(
    ({ productId }) => productId === definition.productId,
  );
  if (profilesForPid.length > 1 && options.expectedProfileId === undefined) {
    fail(
      "APX PID 0x7523 is shared by P3767-0003 and P3767-0005; " +
      "expectedProfileId is required",
    );
  }

  const hashBytesTotal = HASHED_BUNDLE_NAMES.reduce(
    (total, name) => total + files.get(name).size,
    0,
  );
  let hashBytesBeforeFile = 0;
  for (const name of HASHED_BUNDLE_NAMES) {
    const file = files.get(name);
    const actual = await sha256File(file, {
      chunkSize: options.hashChunkSize,
      onProgress(fileBytesHashed, fileBytesTotal) {
        options.onProgress?.(Object.freeze({
          type: "hash",
          file: name,
          fileBytesHashed,
          fileBytesTotal,
          bundleBytesHashed: hashBytesBeforeFile + fileBytesHashed,
          bundleBytesTotal: hashBytesTotal,
        }));
      },
    });
    if (actual !== checksums[name]) {
      fail(`SHA-256 mismatch for ${name}: got ${actual}, expected ${checksums[name]}`);
    }
    if (trustedChecksums && actual !== trustedChecksums[name]) {
      fail(`bundle is not in the trusted digest set: ${name}`);
    }
    hashBytesBeforeFile += file.size;
  }

  const artifacts = Object.freeze(Object.fromEntries(
    RCM_ARTIFACTS.map(({ name }) => [name, files.get(name)]),
  ));
  const totalBytes = RCM_ARTIFACTS.reduce(
    (total, { name }) => total + artifacts[name].size,
    0,
  );
  return Object.freeze({
    [VALIDATED_BUNDLE]: true,
    profile: definition,
    metadata,
    checksums,
    artifacts,
    totalBytes,
  });
}
