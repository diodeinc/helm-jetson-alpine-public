#!/usr/bin/env python3
"""Build a Tegra234 recovery blob with native, dependency-free Python."""

from __future__ import annotations

import argparse
import hashlib
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


BLOB_MAGIC = b"blob"
TABLE_MAGIC = 0x415A1F8A
DIGEST_OFFSET = 8
BODY_OFFSET = 72
TABLE_OFFSET = 80
ENTRY_SIZE = 16
PAYLOAD_OFFSET = 1104
BCH_MAGIC_OFFSET = 0x1400

# Values recovered from the unstripped R39.2 tegrahost_v2
# NvTegraMapPartitionTypeT23x function. The older values also match the
# independent, boot-tested t234-bootkit implementation.
TYPE_IDS = {
    "bootloader": 43,
    "psc_fw": 17,
    "mts_mce": 8,
    "tsec_fw": 19,
    # R39.2 does not special-case this string in its mapper. tegrahost_v2
    # emits its fallback value, which is the R39 T23x wire value for MB2A.
    "mb2_applet": 62,
    "mb2_bootloader": 6,
    "xusb_fw": 36,
    "pva_fw": 59,
    "dce_fw": 31,
    "nvdec": 7,
    "bpmp_fw": 15,
    "bpmp_fw_dtb": 16,
    "sce_fw": 32,
    "rce_fw": 35,
    "ape_fw": 33,
    "spe_fw": 37,
    "tos": 39,
    "eks": 41,
    "kernel": 45,
    "kernel_dtb": 46,
}

COMPONENT_MAGICS = {
    "bootloader": b"CPBL",
    "psc_fw": b"PFWP",
    "mts_mce": b"MTSM",
    "tsec_fw": b"TSEC",
    "mb2_applet": b"MB2A",
    "mb2_bootloader": b"MB2B",
    "xusb_fw": b"XUSB",
    "pva_fw": b"PVAF",
    "dce_fw": b"DCEF",
    "nvdec": b"NDEC",
    "bpmp_fw": b"BPMF",
    "bpmp_fw_dtb": b"BPMD",
    "sce_fw": b"SCEF",
    "rce_fw": b"RCEF",
    "ape_fw": b"APEF",
    "spe_fw": b"SPEF",
    "tos": b"TOSB",
    "eks": b"EKSB",
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


@dataclass(frozen=True)
class Entry:
    kind: str
    name: str
    data: bytes


def validate_payload(entry: Entry, source: Path) -> None:
    expected = COMPONENT_MAGICS.get(entry.kind)
    if expected is not None:
        if len(entry.data) < BCH_MAGIC_OFFSET + 4 or entry.data[:4] != b"NVDA":
            fail(f"{source} is not a T234 BCH component")
        actual = entry.data[BCH_MAGIC_OFFSET : BCH_MAGIC_OFFSET + 4]
        if actual != expected:
            fail(f"{source} has BCH magic {actual!r}, expected {expected!r}")
    elif entry.kind == "kernel" and not entry.data.startswith(b"ANDROID!"):
        fail(f"{source} is not an Android boot image")
    elif entry.kind == "kernel_dtb" and not entry.data.startswith(b"\xd0\x0d\xfe\xed"):
        fail(f"{source} is not a flattened device tree")


def read_manifest(path: Path, input_dir: Path) -> list[Entry]:
    root = ET.parse(path).getroot()
    if root.tag != "file_list" or root.get("mode") != "blob":
        fail(f"{path} is not a recovery blob manifest")
    entries: list[Entry] = []
    for node in root.findall("file"):
        name = node.get("name")
        kind = node.get("type")
        if not name or not kind:
            fail(f"{path} has an entry without name/type")
        relative = Path(name)
        if relative.is_absolute() or relative.name != name or ".." in relative.parts:
            fail(f"unsafe payload name: {name!r}")
        if kind not in TYPE_IDS:
            fail(f"unsupported T234 payload type: {kind}")
        source = input_dir / relative
        if not source.is_file():
            fail(f"missing payload: {source}")
        entry = Entry(kind, name, source.read_bytes())
        if not entry.data:
            fail(f"empty payload: {source}")
        validate_payload(entry, source)
        entries.append(entry)
    if not entries:
        fail(f"{path} contains no payloads")
    if TABLE_OFFSET + len(entries) * ENTRY_SIZE > PAYLOAD_OFFSET:
        fail("recovery blob entry table exceeds the fixed header reservation")
    return entries


def build_blob(entries: list[Entry]) -> bytes:
    cursor = PAYLOAD_OFFSET
    records = bytearray()
    for entry in entries:
        records.extend(struct.pack("<IQI", TYPE_IDS[entry.kind], cursor, len(entry.data)))
        cursor += len(entry.data)
    body = struct.pack("<II", TABLE_MAGIC, len(entries)) + records
    body += bytes(PAYLOAD_OFFSET - BODY_OFFSET - len(body))
    digest = hashlib.sha512(body).digest()
    header = BLOB_MAGIC + bytes(4) + digest + body
    if len(header) != PAYLOAD_OFFSET:
        raise AssertionError("internal recovery blob header size error")
    return header + b"".join(entry.data for entry in entries)


def verify_blob(blob: bytes, entries: list[Entry]) -> None:
    if blob[:4] != BLOB_MAGIC or len(blob) < PAYLOAD_OFFSET:
        fail("generated recovery blob has an invalid header")
    if hashlib.sha512(blob[BODY_OFFSET:PAYLOAD_OFFSET]).digest() != blob[8:72]:
        fail("generated recovery blob header digest does not match")
    count = struct.unpack_from("<I", blob, 76)[0]
    if count != len(entries):
        fail("generated recovery blob entry count does not match")
    cursor = PAYLOAD_OFFSET
    for index, entry in enumerate(entries):
        kind, offset, size = struct.unpack_from("<IQI", blob, TABLE_OFFSET + index * 16)
        if (kind, offset, size) != (TYPE_IDS[entry.kind], cursor, len(entry.data)):
            fail(f"generated recovery blob entry {index} does not match")
        if blob[offset : offset + size] != entry.data:
            fail(f"generated recovery blob payload {index} does not match")
        cursor += size
    if cursor != len(blob):
        fail("generated recovery blob has trailing bytes")


def self_test() -> None:
    entries = [Entry("kernel", "a.bin", b"A"), Entry("kernel_dtb", "b.bin", b"BC")]
    blob = build_blob(entries)
    verify_blob(blob, entries)
    expected = "700bdfa2ff9e7fae4a548cb9bb7ec570ee1adc07f80f939d213e167d5ef88ea7"
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        fail(f"NVIDIA tegrahost fixture mismatch: {actual} != {expected}")
    print("ok: native blob builder matches the NVIDIA two-entry fixture byte-for-byte")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path)
    parser.add_argument("input_dir", nargs="?", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.manifest is None or args.input_dir is None or args.output is None:
        parser.error("manifest, input_dir, and output are required")

    entries = read_manifest(args.manifest, args.input_dir)
    blob = build_blob(entries)
    verify_blob(blob, entries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_bytes(blob)
    temporary.replace(args.output)
    print(f"wrote {args.output} ({len(blob)} bytes, {len(entries)} payloads)")
    for entry in entries:
        print(f"  {entry.kind:16} {len(entry.data):10}  {entry.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
