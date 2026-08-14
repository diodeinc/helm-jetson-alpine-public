#!/usr/bin/env node

import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";


const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 3190;
const DEFAULT_ROOT = path.resolve("build/helm-web/site");
const SECURITY_HEADERS = Object.freeze({
  "Content-Security-Policy": [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "font-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "worker-src 'self'",
  ].join("; "),
  "Permissions-Policy": [
    "usb=(self)",
    "serial=(self)",
    "camera=()",
    "geolocation=()",
    "microphone=()",
    "payment=()",
  ].join(", "),
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
});

const MIME_TYPES = Object.freeze({
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
});

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "RequestError";
    this.status = status;
  }
}

function commonHeaders(extra = {}) {
  return {
    ...SECURITY_HEADERS,
    "Cache-Control": "no-store",
    ...extra,
  };
}

function sendText(response, status, message, method = "GET", extra = {}) {
  const body = `${message}\n`;
  response.writeHead(
    status,
    commonHeaders({
      "Content-Type": "text/plain; charset=utf-8",
      "Content-Length": Buffer.byteLength(body),
      ...extra,
    }),
  );
  response.end(method === "HEAD" ? undefined : body);
}

function isWithinRoot(root, candidate) {
  return candidate === root || candidate.startsWith(`${root}${path.sep}`);
}

export async function resolveRequestPath(root, requestTarget) {
  let url;
  try {
    url = new URL(requestTarget, "http://helm.invalid");
  } catch {
    throw new RequestError(400, "invalid request URL");
  }
  let decoded;
  try {
    decoded = decodeURIComponent(url.pathname);
  } catch {
    throw new RequestError(400, "invalid URL encoding");
  }
  if (decoded.includes("\0") || decoded.includes("\\")) {
    throw new RequestError(400, "invalid request path");
  }
  const segments = decoded.split("/");
  if (segments.includes("..") || segments.includes(".")) {
    throw new RequestError(400, "path traversal is not permitted");
  }
  if (segments.some((segment) => segment.startsWith("."))) {
    throw new RequestError(404, "not found");
  }
  if (decoded === "/" || decoded.endsWith("/")) {
    decoded += "index.html";
  }
  const lexical = path.resolve(root, `.${decoded}`);
  if (!isWithinRoot(root, lexical)) {
    throw new RequestError(403, "requested path is outside the site root");
  }
  let resolved;
  try {
    resolved = await realpath(lexical);
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
      throw new RequestError(404, "not found");
    }
    throw error;
  }
  if (!isWithinRoot(root, resolved)) {
    throw new RequestError(403, "symlink target is outside the site root");
  }
  return resolved;
}

function parseDecimal(value) {
  if (!/^[0-9]+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

export function parseRange(header, size) {
  if (header === undefined) {
    return null;
  }
  if (typeof header !== "string" || !Number.isSafeInteger(size) || size < 0) {
    return false;
  }
  const match = /^bytes=([^,]+)$/.exec(header.trim());
  if (!match) {
    return false;
  }
  const separator = match[1].indexOf("-");
  if (separator === -1) {
    return false;
  }
  const startText = match[1].slice(0, separator).trim();
  const endText = match[1].slice(separator + 1).trim();
  if (startText === "") {
    const suffix = parseDecimal(endText);
    if (suffix === null || suffix === 0 || size === 0) {
      return false;
    }
    return { start: Math.max(0, size - suffix), end: size - 1 };
  }
  const start = parseDecimal(startText);
  if (start === null || start >= size) {
    return false;
  }
  if (endText === "") {
    return { start, end: size - 1 };
  }
  const requestedEnd = parseDecimal(endText);
  if (requestedEnd === null || requestedEnd < start) {
    return false;
  }
  return { start, end: Math.min(requestedEnd, size - 1) };
}

function contentType(filename) {
  const basename = path.basename(filename);
  if (basename === "PROFILE" || basename === "SHA256SUMS") {
    return "text/plain; charset=utf-8";
  }
  return MIME_TYPES[path.extname(filename).toLowerCase()] ?? "application/octet-stream";
}

async function serveFile(request, response, root) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    sendText(response, 405, "method not allowed", request.method, { Allow: "GET, HEAD" });
    return;
  }
  let filename;
  try {
    filename = await resolveRequestPath(root, request.url ?? "/");
  } catch (error) {
    if (error instanceof RequestError) {
      sendText(response, error.status, error.message, request.method);
      return;
    }
    throw error;
  }
  const metadata = await stat(filename);
  if (!metadata.isFile()) {
    sendText(response, 404, "not found", request.method);
    return;
  }

  const range = parseRange(request.headers.range, metadata.size);
  if (range === false) {
    sendText(response, 416, "range not satisfiable", request.method, {
      "Accept-Ranges": "bytes",
      "Content-Range": `bytes */${metadata.size}`,
    });
    return;
  }
  const status = range === null ? 200 : 206;
  const start = range?.start ?? 0;
  const end = range?.end ?? metadata.size - 1;
  const length = range === null ? metadata.size : end - start + 1;
  const headers = commonHeaders({
    "Accept-Ranges": "bytes",
    "Content-Type": contentType(filename),
    "Content-Length": length,
  });
  if (range !== null) {
    headers["Content-Range"] = `bytes ${start}-${end}/${metadata.size}`;
  }
  response.writeHead(status, headers);
  if (request.method === "HEAD") {
    response.end();
    return;
  }
  try {
    await pipeline(createReadStream(filename, range === null ? {} : { start, end }), response);
  } catch (error) {
    if (!response.destroyed) {
      response.destroy(error);
    }
  }
}

export async function createStaticServer({ root }) {
  const resolvedRoot = await realpath(path.resolve(root));
  const metadata = await stat(resolvedRoot);
  if (!metadata.isDirectory()) {
    throw new Error(`site root is not a directory: ${resolvedRoot}`);
  }
  return http.createServer((request, response) => {
    serveFile(request, response, resolvedRoot).catch((error) => {
      if (!response.headersSent) {
        sendText(response, 500, "internal server error", request.method);
      } else if (!response.destroyed) {
        response.destroy(error);
      }
    });
  });
}

function parsePort(value) {
  if (!/^[0-9]+$/.test(value)) {
    throw new Error(`invalid port: ${value}`);
  }
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65_535) {
    throw new Error(`invalid port: ${value}`);
  }
  return port;
}

function parseArguments(arguments_) {
  const options = {
    host: process.env.HELM_WEB_HOST ?? DEFAULT_HOST,
    port: parsePort(process.env.HELM_WEB_PORT ?? String(DEFAULT_PORT)),
    root: process.env.HELM_WEB_ROOT ?? DEFAULT_ROOT,
  };
  const seen = new Set();
  for (let index = 0; index < arguments_.length; index += 1) {
    const option = arguments_[index];
    if (option === "--help" || option === "-h") {
      return { help: true, ...options };
    }
    if (!["--host", "--port", "--root"].includes(option)) {
      throw new Error(`unknown option: ${option}`);
    }
    if (seen.has(option)) {
      throw new Error(`option may be specified only once: ${option}`);
    }
    seen.add(option);
    index += 1;
    if (index >= arguments_.length || arguments_[index] === "") {
      throw new Error(`${option} requires a value`);
    }
    const value = arguments_[index];
    if (option === "--host") {
      options.host = value;
    } else if (option === "--port") {
      options.port = parsePort(value);
    } else {
      options.root = path.resolve(value);
    }
  }
  return options;
}

async function run() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(
      "usage: server.mjs [--host 127.0.0.1] [--port 3190] " +
      "[--root build/helm-web/site]\n",
    );
    return;
  }
  const server = await createStaticServer({ root: options.root });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address !== null ? address.port : options.port;
  process.stdout.write(`helm-web: serving ${path.resolve(options.root)} on http://${options.host}:${port}/\n`);

  const stop = () => {
    server.close((error) => {
      if (error) {
        process.stderr.write(`helm-web: shutdown failed: ${error.message}\n`);
        process.exitCode = 1;
      }
    });
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
}

async function isMainModule() {
  if (!process.argv[1]) {
    return false;
  }
  try {
    const invokedPath = await realpath(path.resolve(process.argv[1]));
    const modulePath = await realpath(fileURLToPath(import.meta.url));
    return invokedPath === modulePath;
  } catch {
    return false;
  }
}

if (await isMainModule()) {
  run().catch((error) => {
    process.stderr.write(`helm-web: ${error.message}\n`);
    process.exitCode = 1;
  });
}
