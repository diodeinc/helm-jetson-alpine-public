#!/usr/bin/env python3
"""Build an unfused T234 Boot Component Header.

This implements the deterministic R35.6.1 ``zerosbk`` header used by the
Helm/Orin NX recovery bundle.  It deliberately starts from a known-good header
template so that policy fields, ratchets, and reserved bytes remain tied to a
validated NVIDIA BSP artifact. It supports the observed single-payload format,
the four-payload cold memory format, the pre-headered NVDEC finalization form,
and the compressed-component header around an already prepared LZ4
metadata/blob payload. It does not implement fused secure boot or encryption.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path
from typing import NoReturn


HEADER_SIZE = 8192
ALIGNMENT = 16
MAGIC = b"NVDA"
MULTI_DESCRIPTOR_OFFSET = 0x1400
MULTI_DESCRIPTOR_STRIDE = 0xA0
MULTI_SIZE_OFFSET = 0x04
MULTI_BLOCK_OFFSET = 0x18
MULTI_SHA_OFFSET = 0x60
MULTI_PAYLOAD_COUNT = 4
COMPRESSED_CHUNK_SIZE = 0x40000
COMPRESSED_DESCRIPTOR_SIZE = 0x5C
COMPRESSED_DESCRIPTOR_SHA_OFFSET = 0x1C
COMPRESSED_METADATA_ALIGNMENT = 0x1000
LZ4_FRAME_MAGIC = 0x184D2204

OUTER_SHA_OFFSET = 4
OUTER_SHA_INPUT_OFFSET = 68
BCH_SHA_OFFSET = 80
BCH_SHA_INPUT_OFFSET = 4032

STAGE2_MAGIC_OFFSET = 5120
STAGE2_SIZE_OFFSET = 5124
STAGE2_SHA_OFFSET = 5216
EXISTING_STAGE2_COPY_MAGIC_OFFSET = 5280
EXISTING_STAGE2_COPY_SIZE_OFFSET = 5284
STAGE1_MAGIC_OFFSET = 7904
STAGE1_SIZE_OFFSET = 7908
STAGE1_SHA_OFFSET = 7984
SHA512_SIZE = 64
COMPRESSED_FLAG_OFFSET = STAGE2_MAGIC_OFFSET + 0x1F
COMPRESSED_UNCOMPRESSED_SIZE_OFFSET = STAGE2_MAGIC_OFFSET + 0x20
COMPRESSED_FRAME_SIZE_OFFSET = STAGE2_MAGIC_OFFSET + 0x28
COMPRESSED_SOURCE_PADDING_OFFSET = STAGE2_MAGIC_OFFSET + 0x2C


def fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


def read_header_template(path: Path) -> bytearray:
    data = path.read_bytes()
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        fail(f"{path} does not start with a T234 NVDA header")
    return bytearray(data[:HEADER_SIZE])


def build_header(template: bytearray, magic_id: bytes, payload: bytes) -> bytes:
    if len(magic_id) != 4:
        fail("magic ID must contain exactly four ASCII bytes")
    if len(payload) > 0xFFFFFFFF:
        fail("payload is too large for the 32-bit BCH size field")

    digest = hashlib.sha512(payload).digest()
    for offset in (STAGE2_MAGIC_OFFSET, STAGE1_MAGIC_OFFSET):
        template[offset : offset + 4] = magic_id
    for offset in (STAGE2_SIZE_OFFSET, STAGE1_SIZE_OFFSET):
        struct.pack_into("<I", template, offset, len(payload))
    for offset in (STAGE2_SHA_OFFSET, STAGE1_SHA_OFFSET):
        template[offset : offset + SHA512_SIZE] = digest

    template[BCH_SHA_OFFSET : BCH_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[BCH_SHA_INPUT_OFFSET:]
    ).digest()
    template[OUTER_SHA_OFFSET : OUTER_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[OUTER_SHA_INPUT_OFFSET:]
    ).digest()
    return bytes(template)


def build_existing_header(
    policy_template: bytearray, magic_id: bytes, component: bytes
) -> tuple[bytes, bytes]:
    """Finalize a vendor component that already contains an NVDA header.

    R35 ships NVDEC with a distinct pre-headered form.  Its payload digest is
    installed only in the stage-2 descriptor, a duplicate magic/size record at
    0x14a0 is populated, and the stage-1 digest remains zero.  All other policy
    bytes must match the retained component template before hashes are updated.
    """

    if len(component) <= HEADER_SIZE or component[:4] != MAGIC:
        fail("existing-header input is not an NVDA component with a payload")
    header = bytearray(component[:HEADER_SIZE])
    payload = component[HEADER_SIZE:]
    if len(magic_id) != 4:
        fail("magic ID must contain exactly four ASCII bytes")
    if len(payload) > 0xFFFFFFFF:
        fail("payload is too large for the 32-bit BCH size field")

    dynamic_ranges = (
        (OUTER_SHA_OFFSET, OUTER_SHA_OFFSET + SHA512_SIZE),
        (BCH_SHA_OFFSET, BCH_SHA_OFFSET + SHA512_SIZE),
        (STAGE2_SHA_OFFSET, STAGE2_SHA_OFFSET + SHA512_SIZE),
        (EXISTING_STAGE2_COPY_MAGIC_OFFSET, EXISTING_STAGE2_COPY_SIZE_OFFSET + 4),
    )
    for offset, (actual, expected) in enumerate(zip(header, policy_template)):
        if any(start <= offset < end for start, end in dynamic_ranges):
            continue
        if actual != expected:
            fail(
                f"existing header conflicts with the policy template at {offset:#x}"
            )

    digest = hashlib.sha512(payload).digest()
    header[STAGE2_MAGIC_OFFSET : STAGE2_MAGIC_OFFSET + 4] = magic_id
    struct.pack_into("<I", header, STAGE2_SIZE_OFFSET, len(payload))
    header[STAGE2_SHA_OFFSET : STAGE2_SHA_OFFSET + SHA512_SIZE] = digest
    header[
        EXISTING_STAGE2_COPY_MAGIC_OFFSET : EXISTING_STAGE2_COPY_MAGIC_OFFSET + 4
    ] = magic_id
    struct.pack_into("<I", header, EXISTING_STAGE2_COPY_SIZE_OFFSET, len(payload))

    header[BCH_SHA_OFFSET : BCH_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        header[BCH_SHA_INPUT_OFFSET:]
    ).digest()
    header[OUTER_SHA_OFFSET : OUTER_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        header[OUTER_SHA_INPUT_OFFSET:]
    ).digest()
    return bytes(header), payload


def align(payload: bytes, alignment: int) -> bytes:
    """Zero-pad multi-slot payloads to their storage-block boundary."""

    remainder = len(payload) % alignment
    if remainder:
        payload += bytes(alignment - remainder)
    return payload


def align_tegrahost(payload: bytes, alignment: int) -> bytes:
    """Apply tegrahost's 0x80-then-zero component alignment padding."""

    remainder = len(payload) % alignment
    if remainder:
        padding_size = alignment - remainder
        payload += b"\x80" + bytes(padding_size - 1)
    return payload


def align_size(size: int, alignment: int) -> int:
    return (size + alignment - 1) // alignment * alignment


def lz4_frame_size(frame: bytes) -> int:
    """Return the first LZ4 frame's encoded length, excluding outer padding."""

    if len(frame) < 7 or struct.unpack_from("<I", frame)[0] != LZ4_FRAME_MAGIC:
        fail("compressed payload does not begin with an LZ4 frame")
    flags = frame[4]
    if flags >> 6 != 1:
        fail(f"unsupported LZ4 frame version in flags {flags:#x}")

    cursor = 6
    if flags & 0x08:  # content size
        cursor += 8
    if flags & 0x01:  # dictionary ID
        cursor += 4
    cursor += 1  # descriptor checksum
    if cursor > len(frame):
        fail("truncated LZ4 frame descriptor")

    while True:
        if cursor + 4 > len(frame):
            fail("truncated LZ4 block header")
        block = struct.unpack_from("<I", frame, cursor)[0]
        cursor += 4
        if block == 0:
            break
        cursor += block & 0x7FFFFFFF
        if flags & 0x10:  # per-block checksum
            cursor += 4
        if cursor > len(frame):
            fail("truncated LZ4 block data")
    if flags & 0x04:  # content checksum
        cursor += 4
    if cursor > len(frame):
        fail("truncated LZ4 frame checksum")
    return cursor


def parse_compressed_blob(blob: bytes) -> tuple[bytes, bytes, int]:
    if len(blob) < COMPRESSED_METADATA_ALIGNMENT:
        fail("compressed blob is too small for its metadata table")
    chunk_count = struct.unpack_from("<I", blob)[0]
    if chunk_count == 0:
        fail("compressed blob metadata has no chunks")
    metadata_size = align_size(
        (chunk_count + 1) * COMPRESSED_DESCRIPTOR_SIZE,
        COMPRESSED_METADATA_ALIGNMENT,
    )
    if metadata_size >= len(blob):
        fail("compressed blob metadata extends beyond the payload")
    metadata = blob[:metadata_size]
    compressed = blob[metadata_size:]
    if len(compressed) % ALIGNMENT:
        fail("compressed LZ4 region is not 16-byte aligned")
    expected_chunks = align_size(len(compressed), COMPRESSED_CHUNK_SIZE) // COMPRESSED_CHUNK_SIZE
    if chunk_count != expected_chunks:
        fail(
            f"compressed metadata has {chunk_count} chunks, expected {expected_chunks}"
        )
    for index in range(chunk_count):
        start = index * COMPRESSED_CHUNK_SIZE
        chunk = compressed[start : start + COMPRESSED_CHUNK_SIZE]
        digest_offset = (
            (index + 1) * COMPRESSED_DESCRIPTOR_SIZE
            + COMPRESSED_DESCRIPTOR_SHA_OFFSET
        )
        recorded = metadata[digest_offset : digest_offset + SHA512_SIZE]
        if len(recorded) != SHA512_SIZE or recorded != hashlib.sha512(chunk).digest():
            fail(f"compressed metadata hash mismatch for chunk {index}")

    frame_size = lz4_frame_size(compressed)
    padding = compressed[frame_size:]
    if len(padding) >= ALIGNMENT:
        fail("compressed LZ4 frame has excessive outer padding")
    if padding and padding != b"\x80" + bytes(len(padding) - 1):
        fail("compressed LZ4 frame has invalid 0x80/zero padding")
    return metadata, compressed, frame_size


def build_compressed_header(
    template: bytearray,
    magic_id: bytes,
    blob: bytes,
    source: bytes,
) -> bytes:
    if len(magic_id) != 4:
        fail("magic ID must contain exactly four ASCII bytes")
    metadata, compressed, frame_size = parse_compressed_blob(blob)
    aligned_source_size = align_size(len(source), ALIGNMENT)

    template[STAGE2_MAGIC_OFFSET : STAGE2_MAGIC_OFFSET + 4] = magic_id
    struct.pack_into("<I", template, STAGE2_SIZE_OFFSET, len(compressed))
    template[COMPRESSED_FLAG_OFFSET] |= 0x08
    struct.pack_into(
        "<I",
        template,
        COMPRESSED_UNCOMPRESSED_SIZE_OFFSET,
        aligned_source_size + SHA512_SIZE,
    )
    struct.pack_into("<I", template, COMPRESSED_FRAME_SIZE_OFFSET, frame_size)
    template[COMPRESSED_SOURCE_PADDING_OFFSET] = (
        len(source) - aligned_source_size
    ) & 0xFF
    template[STAGE2_SHA_OFFSET : STAGE2_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        metadata
    ).digest()

    template[STAGE1_MAGIC_OFFSET : STAGE1_MAGIC_OFFSET + 4] = magic_id
    struct.pack_into("<I", template, STAGE1_SIZE_OFFSET, len(blob))
    template[STAGE1_SHA_OFFSET : STAGE1_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        blob
    ).digest()

    template[BCH_SHA_OFFSET : BCH_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[BCH_SHA_INPUT_OFFSET:]
    ).digest()
    template[OUTER_SHA_OFFSET : OUTER_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[OUTER_SHA_INPUT_OFFSET:]
    ).digest()
    return bytes(template)


def build_multi_header(
    template: bytearray,
    magic_id: bytes,
    payloads: list[bytes],
    block_size: int,
) -> tuple[bytes, list[bytes]]:
    if magic_id != b"MEMB":
        fail("the observed multi-payload format is supported only for MEMB")
    if len(payloads) != MULTI_PAYLOAD_COUNT:
        fail(f"MEMB requires exactly {MULTI_PAYLOAD_COUNT} payloads")
    if block_size < 16 or block_size & (block_size - 1):
        fail("block size must be a power of two and at least 16 bytes")
    if HEADER_SIZE % block_size:
        fail(f"{HEADER_SIZE}-byte header is not aligned to block size {block_size}")

    padded_payloads: list[bytes] = []
    current_block = HEADER_SIZE // block_size
    for index, payload in enumerate(payloads):
        if len(payload) > 0xFFFFFFFF:
            fail(f"payload {index} is too large for the 32-bit BCH size field")
        descriptor = MULTI_DESCRIPTOR_OFFSET + index * MULTI_DESCRIPTOR_STRIDE
        expected_magic = f"MEM{index}".encode("ascii")
        template[descriptor : descriptor + 4] = expected_magic
        struct.pack_into("<I", template, descriptor + MULTI_SIZE_OFFSET, len(payload))
        struct.pack_into("<I", template, descriptor + MULTI_BLOCK_OFFSET, current_block)
        template[
            descriptor + MULTI_SHA_OFFSET : descriptor + MULTI_SHA_OFFSET + SHA512_SIZE
        ] = hashlib.sha512(payload).digest()

        padded = align(payload, block_size)
        padded_payloads.append(padded)
        current_block += len(padded) // block_size

    template[BCH_SHA_OFFSET : BCH_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[BCH_SHA_INPUT_OFFSET:]
    ).digest()
    template[OUTER_SHA_OFFSET : OUTER_SHA_OFFSET + SHA512_SIZE] = hashlib.sha512(
        template[OUTER_SHA_INPUT_OFFSET:]
    ).digest()
    return bytes(template), padded_payloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "template",
        type=Path,
        help="known-good zerosbk component whose first 8192 bytes are reused",
    )
    parser.add_argument("magic_id", help="four-character T234 component ID, e.g. MEM0")
    parser.add_argument(
        "payload",
        type=Path,
        nargs="+",
        help="one payload, or four payloads with --multi",
    )
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--multi",
        action="store_true",
        help="build the observed four-payload cold memory BCH",
    )
    parser.add_argument(
        "--compressed-source",
        type=Path,
        help=(
            "build the compressed-component BCH; PAYLOAD must be the prepared "
            "metadata/LZ4 blob and this path must be its original source"
        ),
    )
    parser.add_argument(
        "--finalize-existing-header",
        action="store_true",
        help=(
            "finalize the observed pre-headered NVDEC form instead of "
            "prepending a new BCH"
        ),
    )
    parser.add_argument(
        "--block-size",
        type=int,
        default=512,
        help="multi-payload alignment and descriptor block size (default: 512)",
    )
    parser.add_argument(
        "--append-payload-sha512",
        action="store_true",
        help="append SHA-512 of the raw payload before alignment (MB1-BCT convention)",
    )
    parser.add_argument(
        "--no-pad",
        action="store_true",
        help="reject an unaligned payload instead of applying 0x80/zero padding",
    )
    args = parser.parse_args()

    try:
        magic_id = args.magic_id.encode("ascii")
    except UnicodeEncodeError:
        fail("magic ID must be ASCII")

    payloads = [path.read_bytes() for path in args.payload]
    if args.finalize_existing_header:
        if args.multi or args.append_payload_sha512 or args.no_pad or args.compressed_source:
            fail(
                "--finalize-existing-header is incompatible with --multi, "
                "--append-payload-sha512, --no-pad, and --compressed-source"
            )
        if len(payloads) != 1:
            fail("existing-header mode requires exactly one component")
        header, payload = build_existing_header(
            read_header_template(args.template), magic_id, payloads[0]
        )
        args.output.write_bytes(header + payload)
        print(
            f"wrote {args.output} ({len(header) + len(payload)} bytes, "
            f"magic={args.magic_id}, finalized_existing_header=True)"
        )
        return 0
    if args.compressed_source is not None:
        if args.multi or args.append_payload_sha512 or args.no_pad:
            fail(
                "--compressed-source is incompatible with --multi, "
                "--append-payload-sha512, and --no-pad"
            )
        if len(payloads) != 1:
            fail("compressed-component mode requires exactly one prepared blob")
        blob = payloads[0]
        header = build_compressed_header(
            read_header_template(args.template),
            magic_id,
            blob,
            args.compressed_source.read_bytes(),
        )
        args.output.write_bytes(header + blob)
        print(
            f"wrote {args.output} ({len(header) + len(blob)} bytes, "
            f"magic={args.magic_id}, compressed_blob={len(blob)})"
        )
        return 0

    if args.multi:
        if args.append_payload_sha512 or args.no_pad:
            fail("--append-payload-sha512 and --no-pad apply only to single payloads")
        header, padded_payloads = build_multi_header(
            read_header_template(args.template), magic_id, payloads, args.block_size
        )
        args.output.write_bytes(header + b"".join(padded_payloads))
        print(
            f"wrote {args.output} ({args.output.stat().st_size} bytes, "
            f"magic={args.magic_id}, payloads={len(payloads)}, "
            f"block_size={args.block_size})"
        )
        return 0

    if len(payloads) != 1:
        fail("single-payload mode requires exactly one payload")
    payload = payloads[0]
    if args.append_payload_sha512:
        payload += hashlib.sha512(payload).digest()
    if len(payload) % ALIGNMENT:
        if args.no_pad:
            fail(f"payload size {len(payload)} is not {ALIGNMENT}-byte aligned")
        payload = align_tegrahost(payload, ALIGNMENT)

    header = build_header(read_header_template(args.template), magic_id, payload)
    args.output.write_bytes(header + payload)
    print(
        f"wrote {args.output} ({len(header) + len(payload)} bytes, "
        f"magic={args.magic_id}, payload={len(payload)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
