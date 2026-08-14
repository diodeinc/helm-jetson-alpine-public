import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  createStaticServer,
  parseRange,
} from "../server.mjs";


function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

function close(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

function rawRequest(base, requestPath, options = {}) {
  const url = new URL(base);
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        hostname: url.hostname,
        port: url.port,
        method: options.method ?? "GET",
        path: requestPath,
        headers: options.headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks),
          });
        });
      },
    );
    request.once("error", reject);
    request.end();
  });
}

test("range parser accepts one bounded byte range", () => {
  assert.deepEqual(parseRange(undefined, 10), null);
  assert.deepEqual(parseRange("bytes=2-5", 10), { start: 2, end: 5 });
  assert.deepEqual(parseRange("bytes=7-", 10), { start: 7, end: 9 });
  assert.deepEqual(parseRange("bytes=-3", 10), { start: 7, end: 9 });
  assert.deepEqual(parseRange("bytes=8-99", 10), { start: 8, end: 9 });
  assert.equal(parseRange("bytes=10-", 10), false);
  assert.equal(parseRange("bytes=1-2,4-5", 10), false);
  assert.equal(parseRange("items=1-2", 10), false);
});

test("server starts when its entry point is reached through a release symlink", async () => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "helm-web-entry-test-"));
  const entry = path.join(temporary, "server.mjs");
  const serverPath = fileURLToPath(new URL("../server.mjs", import.meta.url));
  try {
    await symlink(serverPath, entry);
    const result = spawnSync(process.execPath, [entry, "--help"], {
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /^usage: server\.mjs /);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("static server provides safe GET, HEAD, and range responses", async () => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "helm-web-server-test-"));
  const root = path.join(temporary, "site");
  const outside = path.join(temporary, "outside.txt");
  await mkdir(path.join(root, "bundles"), { recursive: true });
  await writeFile(path.join(root, "index.html"), "<!doctype html>\n", "utf8");
  await writeFile(path.join(root, "catalog.json"), '{"version":1}\n', "utf8");
  await writeFile(path.join(root, "og.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  await writeFile(path.join(root, "bundles", "blob.bin"), Buffer.from("0123456789"));
  await writeFile(outside, "secret\n", "utf8");
  await symlink(outside, path.join(root, "outside-link"));

  const server = await createStaticServer({ root });
  const base = await listen(server);
  try {
    const index = await rawRequest(base, "/");
    assert.equal(index.status, 200);
    assert.equal(index.body.toString(), "<!doctype html>\n");
    assert.equal(index.headers["content-type"], "text/html; charset=utf-8");
    assert.match(index.headers["content-security-policy"], /default-src 'none'/);
    assert.match(index.headers["permissions-policy"], /usb=\(self\)/);
    assert.match(index.headers["permissions-policy"], /serial=\(self\)/);
    assert.equal(index.headers["cache-control"], "no-store");

    const socialImage = await rawRequest(base, "/og.png", { method: "HEAD" });
    assert.equal(socialImage.status, 200);
    assert.equal(socialImage.headers["content-type"], "image/png");

    const head = await rawRequest(base, "/bundles/blob.bin", { method: "HEAD" });
    assert.equal(head.status, 200);
    assert.equal(head.headers["content-length"], "10");
    assert.equal(head.body.length, 0);

    const range = await rawRequest(base, "/bundles/blob.bin", {
      headers: { Range: "bytes=2-5" },
    });
    assert.equal(range.status, 206);
    assert.equal(range.headers["content-range"], "bytes 2-5/10");
    assert.equal(range.headers["content-length"], "4");
    assert.equal(range.body.toString(), "2345");

    const suffix = await rawRequest(base, "/bundles/blob.bin", {
      headers: { Range: "bytes=-3" },
    });
    assert.equal(suffix.status, 206);
    assert.equal(suffix.body.toString(), "789");

    const invalidRange = await rawRequest(base, "/bundles/blob.bin", {
      headers: { Range: "bytes=20-" },
    });
    assert.equal(invalidRange.status, 416);
    assert.equal(invalidRange.headers["content-range"], "bytes */10");

    const traversal = await rawRequest(base, "/%2e%2e%2foutside.txt");
    assert.equal(traversal.status, 400);
    assert.equal(traversal.body.includes(Buffer.from("secret")), false);

    const externalLink = await rawRequest(base, "/outside-link");
    assert.equal(externalLink.status, 403);
    assert.equal(externalLink.body.includes(Buffer.from("secret")), false);

    const hidden = await rawRequest(base, "/.helm-web-generated");
    assert.equal(hidden.status, 404);

    const post = await rawRequest(base, "/", { method: "POST" });
    assert.equal(post.status, 405);
    assert.equal(post.headers.allow, "GET, HEAD");
  } finally {
    await close(server);
    await rm(temporary, { recursive: true, force: true });
  }
});
