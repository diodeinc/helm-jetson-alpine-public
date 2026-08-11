#!/usr/bin/env python3
"""Build the legacy Android boot image consumed by Jetson T234 MB2."""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path


MAGIC = b"ANDROID!"
HEADER_FORMAT = "<8s10I16s512s32s1024s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def pad_field(value: str, size: int, label: str) -> bytes:
    encoded = value.encode("ascii")
    if len(encoded) >= size:
        raise SystemExit(f"error: {label} must be shorter than {size} bytes")
    return encoded.ljust(size, b"\0")


def align(data: bytes, page_size: int) -> bytes:
    return data + bytes((-len(data)) % page_size)


def build_image(
    kernel: bytes,
    ramdisk: bytes,
    *,
    page_size: int,
    kernel_addr: int,
    ramdisk_addr: int,
    second_addr: int,
    tags_addr: int,
    name: str,
    cmdline: str,
) -> bytes:
    if page_size < 512 or page_size & (page_size - 1):
        raise SystemExit("error: page size must be a power of two of at least 512")
    if HEADER_SIZE > page_size:
        raise SystemExit("error: page size is smaller than the v0 boot header")
    if not kernel or not ramdisk:
        raise SystemExit("error: kernel and ramdisk must both be non-empty")

    encoded_cmdline = cmdline.encode("ascii")
    if len(encoded_cmdline) >= 512 + 1024:
        raise SystemExit("error: command line must be shorter than 1536 bytes")
    primary_cmdline = encoded_cmdline[:512].ljust(512, b"\0")
    extra_cmdline = encoded_cmdline[512:].ljust(1024, b"\0")

    digest = hashlib.sha1()
    for payload in (kernel, ramdisk, b""):
        digest.update(payload)
        digest.update(struct.pack("<I", len(payload)))
    image_id = digest.digest().ljust(32, b"\0")

    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        len(kernel),
        kernel_addr,
        len(ramdisk),
        ramdisk_addr,
        0,
        second_addr,
        tags_addr,
        page_size,
        0,
        0,
        pad_field(name, 16, "name"),
        primary_cmdline,
        image_id,
        extra_cmdline,
    )
    return align(header, page_size) + align(kernel, page_size) + align(
        ramdisk, page_size
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--ramdisk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--name", default="helm-recovery")
    parser.add_argument("--cmdline", default="rdinit=/init console=ttyTCU0,115200 console=tty0")
    parser.add_argument("--page-size", type=lambda value: int(value, 0), default=2048)
    parser.add_argument("--kernel-addr", type=lambda value: int(value, 0), default=0x10008000)
    parser.add_argument("--ramdisk-addr", type=lambda value: int(value, 0), default=0x11000000)
    parser.add_argument("--second-addr", type=lambda value: int(value, 0), default=0x10F00000)
    parser.add_argument("--tags-addr", type=lambda value: int(value, 0), default=0x10000100)
    args = parser.parse_args()

    image = build_image(
        args.kernel.read_bytes(),
        args.ramdisk.read_bytes(),
        page_size=args.page_size,
        kernel_addr=args.kernel_addr,
        ramdisk_addr=args.ramdisk_addr,
        second_addr=args.second_addr,
        tags_addr=args.tags_addr,
        name=args.name,
        cmdline=args.cmdline,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_bytes(image)
    temporary.replace(args.output)
    print(
        f"wrote {args.output} ({len(image)} bytes; "
        f"kernel={args.kernel.stat().st_size}, ramdisk={args.ramdisk.stat().st_size})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
