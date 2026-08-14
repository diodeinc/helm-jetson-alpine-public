#!/usr/bin/env python3
"""Derive fixed Helm P3767 R39.2 inputs from NVIDIA's multi-spec BUP.

The official R39.2 bootloader package contains a UEFI capsule with one
multi-spec BUP covering every Orin NX/Nano P3767 SKU.  This tool extracts the
selected QSPI payloads and rebuilds the three SKU-sensitive RCM components
that are not shipped separately in the BUP.  A qualified P3767-0001 zerosbk
catalog supplies only fixed layout/policy templates and common RCM payloads.

The capsule, BUP, seed files, helper scripts, profile identities, and output
layout are all pinned or allowlisted.  This is intentionally not a generic
NVIDIA signing implementation and never handles fused secure-boot keys.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import NoReturn


CAPSULE_SHA256 = "6b105ccfa982f76c3b235d4b348fbb52a0a59936136db722c14a47ca6a2d0185"
BUP_SHA256 = "ee84d87dc76b5e8f6d007cffee7b1c48d5b86476397e7ee6c4a2b63519816b01"
BUP_MAGIC = b"NVIDIA__BLOB__V3"
BUP_VERSION = 0x01030622
BUP_HEADER = struct.Struct("<16sIIIIII")
BUP_ENTRY = struct.Struct("<40sIIII128s")
BUP_ENTRY_COUNT = 89
BUP_COMPONENT_VERSION = 14624

BCH_HEADER_SIZE = 8192
BCH_MAGIC = b"NVDA"
BCH_OUTER_SHA_OFFSET = 4
BCH_OUTER_SHA_INPUT_OFFSET = 68
BCH_INNER_SHA_OFFSET = 80
BCH_INNER_SHA_INPUT_OFFSET = 4032
BCH_STAGE2_OFFSET = 0x1400
BCH_STAGE2_SIZE_OFFSET = BCH_STAGE2_OFFSET + 4
BCH_STAGE2_SHA_OFFSET = BCH_STAGE2_OFFSET + 0x60
BCH_STAGE1_OFFSET = 0x1EE0
BCH_STAGE1_SIZE_OFFSET = BCH_STAGE1_OFFSET + 4
BCH_STAGE1_SHA_OFFSET = BCH_STAGE1_OFFSET + 0x50
SHA512_SIZE = 64

MB2_RAW_MAGIC = b"MB2B0234"
R39_MB2_CVM_EEPROM_READ_SIZE_OFFSET = 0x9D40
R39_MB2_CVB_EEPROM_READ_SIZE_OFFSET = 0x9D44
R39_MB2_EEPROM_ADDRESS_OFFSET = 0x9D48
R39_MB2_CVM_EEPROM_ADDRESS = 0xA0
R39_MB2_CVB_EEPROM_ADDRESS = 0xAE

MEM_DESCRIPTOR_OFFSET = 0x1400
MEM_DESCRIPTOR_STRIDE = 0xA0
MEM_DESCRIPTOR_SIZE_OFFSET = 4
MEM_DESCRIPTOR_BLOCK_OFFSET = 0x18
MEM_DESCRIPTOR_SHA_OFFSET = 0x60
MEM_BLOCK_SIZE = 512
MEM_SLOT_COUNT = 4

COMPRESSED_CHUNK_SIZE = 0x40000
COMPRESSED_DESCRIPTOR_SIZE = 0x5C
COMPRESSED_DESCRIPTOR_SHA_OFFSET = 0x1C
COMPRESSED_METADATA_ALIGNMENT = 0x1000
COMPRESSED_UNCOMPRESSED_SIZE_OFFSET = 0x1420
COMPRESSED_FRAME_SIZE_OFFSET = 0x1428
LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"

BUILD_BCH_SHA256 = "28364818de59a99a0ccebf36d8c791313a440308293ec61cc693875355db1909"
APPLY_BCT_DELTA_SHA256 = "20a2930a29a4ce36eaad313c0c61bc31e01ad251c98ebcb9ae2feb0d1bbe8237"

SEED_HASHES = {
    "flash.idx": "d6cbc5ab52375bdc917062c88840a411a0e1eb195b466be119df24209c8b1727",
    "mb1_bct_MB1_sigheader.bct.encrypt": "4533a6604413fa0c8f59746e23e7c3f7d06f2cba1e753dc0a2552a738ae7c04a",
    "mb1_cold_boot_bct_MB1_sigheader.bct.encrypt": "6c56cba06d45b839bf802693d3849f3e88374093834f015691f369abedb0cbe9",
    "mem_rcm_sigheader.bct.encrypt": "d05e00d84be7565bfaf3640e5db4a8848b39e7157f394f29d85da17279ea7fc2",
    "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0001-nv_sigheader.bin.encrypt": "2872ac48b889d7c52ffa922ac3112a634e4e78adf902c3fce419f65a2679f623",
    "uefi_t23x_general_with_dtb_sigheader.bin.encrypt": "c98eaba6f249648aacfd7a78061ed6c5a709a150a360691fef79e50027d9ec13",
    "pscfw_t234_prod_sigheader.bin.encrypt": "230279394bc7fa9127e8675afb7829601a23c0d8a2d23fee9be5b10de577be5a",
    "mce_flash_o10_cr_prod_sigheader.bin.encrypt": "ea39655b16045d1267944848a442a57e23ba3ca851079444c80f7b13718807ac",
    "tsec_t234_sigheader.bin.encrypt": "61c5fe2f13eb64e348807e354b99f339ac736a230084fbd7a5306a4ced4c4778",
    "applet_t234_sigheader.bin.encrypt": "b4c4f89d64fae8e03ab206118300b8aa3dd1cf29ac4a5f53894fc87767c0e48f",
    "mb2_t234_with_mb2_bct_MB2_sigheader.bin.encrypt": "7a7e8d19529013c3c6799e7493404ae0d152dd544e01e778d7023fb8b3102bef",
    "xusb_t234_prod_sigheader.bin.encrypt": "0f5e39ef3ff75320849554c3ab0494aadc98799bff9bf45760c78a77f2d103cb",
    "nvpva_020_sigheader.fw.encrypt": "6d43aa621b1f778fd7a42f1b8b94995644a185bbc830d28efa019d19f62e940e",
    "nvdec_t234_prod_sigheader.fw.encrypt": "b1e08069ed8008cf8891d3fb826aa98945833609415c7e77a4e06e6c0f262630",
    "camera-rtcpu-t234-rce_sigheader.img.encrypt": "d899e020df0c0ea854ce60031c52971acaf1b458a465c79ef59240fee3521c11",
    "adsp-fw_sigheader.bin.encrypt": "7c2131fccd990fe522784a09b4c9d3b82573ee7e7c317e609b86702c33de4905",
    "spe_t234_sigheader.bin.encrypt": "378da71de3bc58dc370a4a75c5f46e5007fffce04f0362480d51020bc0039ad0",
    "tos-optee_t234_sigheader.img.encrypt": "d42e42560b6aeb7d811053a329a155a508d109cab16596184054f821ced9a6ed",
    "eks_t234_sigheader.img.encrypt": "ad480b0044c823557151fa468115ca4657211dc53337df19fc0c74e64cbd2403",
}

COMMON_RCM_FILES = (
    "uefi_t23x_general_with_dtb_sigheader.bin.encrypt",
    "pscfw_t234_prod_sigheader.bin.encrypt",
    "mce_flash_o10_cr_prod_sigheader.bin.encrypt",
    "tsec_t234_sigheader.bin.encrypt",
    "applet_t234_sigheader.bin.encrypt",
    "mb2_t234_with_mb2_bct_MB2_sigheader.bin.encrypt",
    "xusb_t234_prod_sigheader.bin.encrypt",
    "nvpva_020_sigheader.fw.encrypt",
    "nvdec_t234_prod_sigheader.fw.encrypt",
    "camera-rtcpu-t234-rce_sigheader.img.encrypt",
    "adsp-fw_sigheader.bin.encrypt",
    "spe_t234_sigheader.bin.encrypt",
    "tos-optee_t234_sigheader.img.encrypt",
    "eks_t234_sigheader.img.encrypt",
)

QSPI_PART_TO_BUP = {
    "BCT": "BCT",
    "mb1": "mb1",
    "psc_bl1": "psc_bl1",
    "MB1_BCT": "MB1_BCT",
    "MEM_BCT": "MEM_BCT",
    "tsec-fw": "tsec-fw",
    "nvdec": "nvdec",
    "mb2": "mb2",
    "xusb-fw": "xusb-fw",
    "bpmp-fw": "bpmp-fw",
    "bpmp-fw-dtb": "bpmp-fw-dtb",
    "psc-fw": "psc-fw",
    "mts-mce": "mts-mce",
    "sc7": "sc7",
    "pscrf": "pscrf",
    "mb2rf": "mb2rf",
    "cpu-bootloader": "cpu-bootloader",
    "secure-os": "secure-os",
    "dce-fw": "dce-fw",
    "spe-fw": "spe-fw",
    "rce-fw": "rce-fw",
    "adsp-fw": "adsp-fw",
    "pva-fw": "pva-fw",
    "BCT-boot-chain_backup": "BCT-boot-chain_backup",
    "secondary_gpt_backup": "secondary_gpt_backup",
    "VER": "VER",
    "secondary_gpt": "secondary_gpt",
}


@dataclass(frozen=True)
class Profile:
    sku: str
    bpmp_fw: str
    bpmp_dtb: str
    dce_rcm: str
    dce_qspi: str

    @property
    def tnspec(self) -> str:
        return f"3767-000-{self.sku}--1-0-jetson-orin-nano-devkit-"


PROFILES = {
    "helm-orin-nx-16gb-r39.2": Profile(
        "0000",
        "bpmp_t234-TE980M-A1_prod_sigheader.bin.encrypt",
        "tegra234-bpmp-3767-0000-a02-3509-a02_with_odm_sigheader.dtb.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0000-nv_sigheader.bin.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0000-nv_aligned_blob_w_bin_sigheader.bin.encrypt",
    ),
    "helm-orin-nx-8gb-r39.2": Profile(
        "0001",
        "bpmp_t234-TE980M-A1_prod_sigheader.bin.encrypt",
        "tegra234-bpmp-3767-0001-3509-a02_with_odm_sigheader.dtb.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0001-nv_sigheader.bin.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0001-nv_aligned_blob_w_bin_sigheader.bin.encrypt",
    ),
    "helm-orin-nano-8gb-r39.2": Profile(
        "0003",
        "bpmp_t234-TE950M-A1_prod_sigheader.bin.encrypt",
        "tegra234-bpmp-3767-0003-3509-a02_with_odm_sigheader.dtb.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0003-nv_sigheader.bin.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0003-nv_aligned_blob_w_bin_sigheader.bin.encrypt",
    ),
    "helm-orin-nano-4gb-r39.2": Profile(
        "0004",
        "bpmp_t234-TE950M-A1_prod_sigheader.bin.encrypt",
        "tegra234-bpmp-3767-0004-3509-a02_with_odm_sigheader.dtb.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0004-nv_sigheader.bin.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0004-nv_aligned_blob_w_bin_sigheader.bin.encrypt",
    ),
    "helm-orin-nano-8gb-sd-r39.2": Profile(
        "0005",
        "bpmp_t234-TE950M-A1_prod_sigheader.bin.encrypt",
        "tegra234-bpmp-3767-0003-3509-a02_with_odm_sigheader.dtb.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0005-nv_sigheader.bin.encrypt",
        "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0005-nv_aligned_blob_w_bin_sigheader.bin.encrypt",
    ),
}


@dataclass(frozen=True)
class BupEntry:
    name: str
    offset: int
    size: int
    version: int
    op_mode: int
    tnspec: str
    payload: bytes


def fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        fail(f"cannot read {path}: {error}")
    return digest.hexdigest()


def decode_fixed(value: bytes, label: str) -> str:
    content, separator, tail = value.partition(b"\0")
    if separator and any(tail):
        fail(f"{label} has nonzero bytes after its NUL terminator")
    try:
        return content.decode("ascii")
    except UnicodeDecodeError:
        fail(f"{label} is not ASCII")


def parse_bup(
    blob: bytes,
    *,
    expected_version: int,
    expected_count: int,
    expected_sha256: str | None,
) -> list[BupEntry]:
    if expected_sha256 is not None and sha256_bytes(blob) != expected_sha256:
        fail("NVIDIA BUP SHA-256 does not match the pinned R39.2 artifact")
    if len(blob) < BUP_HEADER.size:
        fail("BUP is truncated before its header")
    magic, version, blob_size, header_size, count, blob_type, uncompressed_size = (
        BUP_HEADER.unpack_from(blob)
    )
    if magic != BUP_MAGIC:
        fail("BUP magic is invalid")
    if version != expected_version:
        fail(f"BUP version is {version:#x}, expected {expected_version:#x}")
    if blob_size != len(blob) or uncompressed_size != len(blob):
        fail("BUP size fields do not match the capsule payload")
    if header_size != BUP_HEADER.size or blob_type != 0:
        fail("BUP header layout/type is unsupported")
    if count != expected_count:
        fail(f"BUP has {count} entries, expected {expected_count}")

    table_end = header_size + count * BUP_ENTRY.size
    if table_end > len(blob):
        fail("BUP entry table extends beyond the blob")
    entries: list[BupEntry] = []
    cursor = table_end
    keys: set[tuple[str, str]] = set()
    for index in range(count):
        raw_name, offset, size, component_version, op_mode, raw_spec = (
            BUP_ENTRY.unpack_from(blob, header_size + index * BUP_ENTRY.size)
        )
        name = decode_fixed(raw_name, f"BUP entry {index} name")
        tnspec = decode_fixed(raw_spec, f"BUP entry {index} tnspec")
        if not name or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
            fail(f"BUP entry {index} has an unsafe component name: {name!r}")
        if component_version != BUP_COMPONENT_VERSION:
            fail(
                f"BUP entry {index} component version is {component_version}, "
                f"expected {BUP_COMPONENT_VERSION}"
            )
        if op_mode not in (0, 2):
            fail(f"BUP entry {index} has unsupported operation mode {op_mode}")
        if offset != cursor:
            fail(
                f"BUP entry {index} starts at {offset:#x}, expected contiguous "
                f"offset {cursor:#x}"
            )
        if size <= 0 or offset + size > len(blob):
            fail(f"BUP entry {index} has invalid payload bounds")
        key = (name, tnspec)
        if key in keys:
            fail(f"BUP has duplicate component/spec entry: {name!r}, {tnspec!r}")
        keys.add(key)
        payload = blob[offset : offset + size]
        entries.append(
            BupEntry(name, offset, size, component_version, op_mode, tnspec, payload)
        )
        cursor = offset + size
    if cursor != len(blob):
        fail(f"BUP entries end at {cursor:#x}, blob ends at {len(blob):#x}")
    return entries


def parse_capsule(path: Path) -> list[BupEntry]:
    try:
        capsule = path.read_bytes()
    except OSError as error:
        fail(f"cannot read capsule {path}: {error}")
    if sha256_bytes(capsule) != CAPSULE_SHA256:
        fail("NVIDIA capsule SHA-256 does not match the pinned R39.2 artifact")
    if capsule.count(BUP_MAGIC) != 1:
        fail("capsule does not contain exactly one NVIDIA BUP")
    offset = capsule.index(BUP_MAGIC)
    if offset + BUP_HEADER.size > len(capsule):
        fail("capsule BUP header is truncated")
    blob_size = struct.unpack_from("<I", capsule, offset + 20)[0]
    if offset + blob_size != len(capsule):
        fail("capsule BUP does not consume the remainder of the signed capsule")
    entries = parse_bup(
        capsule[offset:],
        expected_version=BUP_VERSION,
        expected_count=BUP_ENTRY_COUNT,
        expected_sha256=BUP_SHA256,
    )
    supported_specs = {profile.tnspec for profile in PROFILES.values()}
    actual_specs = {entry.tnspec for entry in entries if entry.tnspec}
    if actual_specs != supported_specs:
        fail("capsule module-spec set is not the five supported P3767 SKUs")
    return entries


def select_entry(entries: list[BupEntry], name: str, tnspec: str) -> BupEntry:
    exact = [entry for entry in entries if entry.name == name and entry.tnspec == tnspec]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        fail(f"BUP component {name!r} is ambiguous for {tnspec}")
    shared = [entry for entry in entries if entry.name == name and not entry.tnspec]
    if len(shared) != 1:
        fail(f"BUP has no unique shared/profile component {name!r} for {tnspec}")
    return shared[0]


def verify_seed(seed: Path) -> None:
    if not seed.is_dir():
        fail(f"qualified P3767-0001 seed directory is missing: {seed}")
    for filename, expected in SEED_HASHES.items():
        path = seed / filename
        if not path.is_file():
            fail(f"qualified seed file is missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            fail(f"qualified seed SHA-256 mismatch for {filename}: {actual}")


def verify_helper(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        fail(f"{label} helper is missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        fail(f"{label} helper SHA-256 mismatch: {actual}")


def validate_bch_hashes(component: bytes, label: str) -> bytes:
    if len(component) < BCH_HEADER_SIZE or component[:4] != BCH_MAGIC:
        fail(f"{label} is not a T234 NVDA boot component")
    header = component[:BCH_HEADER_SIZE]
    if header[BCH_OUTER_SHA_OFFSET : BCH_OUTER_SHA_OFFSET + SHA512_SIZE] != (
        hashlib.sha512(header[BCH_OUTER_SHA_INPUT_OFFSET:]).digest()
    ):
        fail(f"{label} outer BCH digest is invalid")
    if header[BCH_INNER_SHA_OFFSET : BCH_INNER_SHA_OFFSET + SHA512_SIZE] != (
        hashlib.sha512(header[BCH_INNER_SHA_INPUT_OFFSET:]).digest()
    ):
        fail(f"{label} inner BCH digest is invalid")
    return header


def single_bch_payload(component: bytes, magic: bytes, label: str) -> bytes:
    header = validate_bch_hashes(component, label)
    payload = component[BCH_HEADER_SIZE:]
    for descriptor, sha_offset in (
        (BCH_STAGE2_OFFSET, BCH_STAGE2_SHA_OFFSET),
        (BCH_STAGE1_OFFSET, BCH_STAGE1_SHA_OFFSET),
    ):
        if header[descriptor : descriptor + 4] != magic:
            fail(f"{label} does not carry the expected {magic!r} component ID")
        if struct.unpack_from("<I", header, descriptor + 4)[0] != len(payload):
            fail(f"{label} BCH size does not match its payload")
        if header[sha_offset : sha_offset + SHA512_SIZE] != hashlib.sha512(
            payload
        ).digest():
            fail(f"{label} BCH payload digest is invalid")
    return payload


def rebuild_single_bch_payload(
    component: bytes, magic: bytes, payload: bytes, label: str
) -> bytes:
    """Replace one zerosbk component payload and refresh all BCH digests."""

    original = single_bch_payload(component, magic, label)
    if len(payload) != len(original):
        fail(f"{label} replacement payload changes the component size")

    header = bytearray(component[:BCH_HEADER_SIZE])
    digest = hashlib.sha512(payload).digest()
    for descriptor, sha_offset in (
        (BCH_STAGE2_OFFSET, BCH_STAGE2_SHA_OFFSET),
        (BCH_STAGE1_OFFSET, BCH_STAGE1_SHA_OFFSET),
    ):
        struct.pack_into("<I", header, descriptor + 4, len(payload))
        header[sha_offset : sha_offset + SHA512_SIZE] = digest
    header[BCH_INNER_SHA_OFFSET : BCH_INNER_SHA_OFFSET + SHA512_SIZE] = (
        hashlib.sha512(header[BCH_INNER_SHA_INPUT_OFFSET:]).digest()
    )
    header[BCH_OUTER_SHA_OFFSET : BCH_OUTER_SHA_OFFSET + SHA512_SIZE] = (
        hashlib.sha512(header[BCH_OUTER_SHA_INPUT_OFFSET:]).digest()
    )
    rebuilt = bytes(header) + payload
    if single_bch_payload(rebuilt, magic, f"patched {label}") != payload:
        fail(f"{label} failed BCH reconstruction validation")
    return rebuilt


def disable_absent_cvb_eeprom(component: bytes, label: str) -> bytes:
    """Retain the module EEPROM and disable Helm's absent carrier EEPROM."""

    payload = bytearray(single_bch_payload(component, b"MB2B", label))
    if payload.count(MB2_RAW_MAGIC) != 1:
        fail(f"{label} does not contain exactly one R39 MB2 raw-BCT marker")
    raw_offset = payload.index(MB2_RAW_MAGIC)
    cvm_offset = raw_offset + R39_MB2_CVM_EEPROM_READ_SIZE_OFFSET
    cvb_offset = raw_offset + R39_MB2_CVB_EEPROM_READ_SIZE_OFFSET
    address_offset = raw_offset + R39_MB2_EEPROM_ADDRESS_OFFSET
    if address_offset + 2 > len(payload):
        fail(f"{label} MB2 raw BCT is truncated before its EEPROM fields")

    cvm_size = struct.unpack_from("<I", payload, cvm_offset)[0]
    cvb_size = struct.unpack_from("<I", payload, cvb_offset)[0]
    addresses = bytes(payload[address_offset : address_offset + 2])
    expected_addresses = bytes(
        (R39_MB2_CVM_EEPROM_ADDRESS, R39_MB2_CVB_EEPROM_ADDRESS)
    )
    if cvm_size != 0x100:
        fail(f"{label} has unexpected CVM EEPROM read size {cvm_size:#x}")
    if cvb_size not in (0, 0x100):
        fail(f"{label} has unexpected CVB EEPROM read size {cvb_size:#x}")
    if addresses != expected_addresses:
        fail(
            f"{label} has unexpected CVM/CVB EEPROM addresses "
            f"{addresses.hex()}, expected {expected_addresses.hex()}"
        )
    if cvb_size == 0:
        return component

    struct.pack_into("<I", payload, cvb_offset, 0)
    return rebuild_single_bch_payload(component, b"MB2B", bytes(payload), label)


def memory_slot_zero(component: bytes, label: str) -> bytes:
    header = validate_bch_hashes(component, label)
    ranges: list[tuple[int, int]] = []
    slots: list[bytes] = []
    for index in range(MEM_SLOT_COUNT):
        descriptor = MEM_DESCRIPTOR_OFFSET + index * MEM_DESCRIPTOR_STRIDE
        expected_magic = f"MEM{index}".encode("ascii")
        if header[descriptor : descriptor + 4] != expected_magic:
            fail(f"{label} memory slot {index} has the wrong component ID")
        size = struct.unpack_from(
            "<I", header, descriptor + MEM_DESCRIPTOR_SIZE_OFFSET
        )[0]
        block = struct.unpack_from(
            "<I", header, descriptor + MEM_DESCRIPTOR_BLOCK_OFFSET
        )[0]
        start = block * MEM_BLOCK_SIZE
        end = start + size
        padded_end = (end + MEM_BLOCK_SIZE - 1) // MEM_BLOCK_SIZE * MEM_BLOCK_SIZE
        if size <= 0 or start < BCH_HEADER_SIZE or padded_end > len(component):
            fail(f"{label} memory slot {index} has invalid bounds")
        if ranges and start < ranges[-1][1]:
            fail(f"{label} memory slot {index} overlaps its predecessor")
        payload = component[start:end]
        recorded = header[
            descriptor
            + MEM_DESCRIPTOR_SHA_OFFSET : descriptor
            + MEM_DESCRIPTOR_SHA_OFFSET
            + SHA512_SIZE
        ]
        if recorded != hashlib.sha512(payload).digest():
            fail(f"{label} memory slot {index} digest is invalid")
        if any(component[end:padded_end]):
            fail(f"{label} memory slot {index} has nonzero block padding")
        ranges.append((start, padded_end))
        slots.append(payload)
    if ranges[-1][1] != len(component):
        fail(f"{label} has trailing data after its memory slots")
    return slots[0]


def compressed_dce_source(
    component: bytes, lz4: Path, work: Path, label: str
) -> bytes:
    header = validate_bch_hashes(component, label)
    body = component[BCH_HEADER_SIZE:]
    if len(body) < COMPRESSED_METADATA_ALIGNMENT:
        fail(f"{label} compressed body is truncated")
    chunk_count = struct.unpack_from("<I", body)[0]
    metadata_size = (
        ((chunk_count + 1) * COMPRESSED_DESCRIPTOR_SIZE + COMPRESSED_METADATA_ALIGNMENT - 1)
        // COMPRESSED_METADATA_ALIGNMENT
        * COMPRESSED_METADATA_ALIGNMENT
    )
    if chunk_count <= 0 or metadata_size >= len(body):
        fail(f"{label} compressed metadata is invalid")
    metadata = body[:metadata_size]
    compressed = body[metadata_size:]
    expected_chunks = (
        len(compressed) + COMPRESSED_CHUNK_SIZE - 1
    ) // COMPRESSED_CHUNK_SIZE
    if chunk_count != expected_chunks:
        fail(f"{label} compressed chunk count is invalid")
    for index in range(chunk_count):
        chunk = compressed[
            index * COMPRESSED_CHUNK_SIZE : (index + 1) * COMPRESSED_CHUNK_SIZE
        ]
        offset = (
            (index + 1) * COMPRESSED_DESCRIPTOR_SIZE
            + COMPRESSED_DESCRIPTOR_SHA_OFFSET
        )
        if metadata[offset : offset + SHA512_SIZE] != hashlib.sha512(chunk).digest():
            fail(f"{label} compressed chunk {index} digest is invalid")
    if header[BCH_STAGE2_SHA_OFFSET : BCH_STAGE2_SHA_OFFSET + SHA512_SIZE] != (
        hashlib.sha512(metadata).digest()
    ):
        fail(f"{label} compressed metadata BCH digest is invalid")
    if struct.unpack_from("<I", header, BCH_STAGE1_SIZE_OFFSET)[0] != len(body):
        fail(f"{label} compressed body size is invalid")
    if header[BCH_STAGE1_SHA_OFFSET : BCH_STAGE1_SHA_OFFSET + SHA512_SIZE] != (
        hashlib.sha512(body).digest()
    ):
        fail(f"{label} compressed body BCH digest is invalid")

    frame_size = struct.unpack_from("<I", header, COMPRESSED_FRAME_SIZE_OFFSET)[0]
    uncompressed_size = struct.unpack_from(
        "<I", header, COMPRESSED_UNCOMPRESSED_SIZE_OFFSET
    )[0]
    if frame_size <= 0 or frame_size > len(compressed):
        fail(f"{label} LZ4 frame size is invalid")
    frame = compressed[:frame_size]
    if not frame.startswith(LZ4_FRAME_MAGIC):
        fail(f"{label} does not contain an LZ4 frame")
    padding = compressed[frame_size:]
    if len(padding) >= 16 or (padding and padding != b"\x80" + bytes(len(padding) - 1)):
        fail(f"{label} has invalid LZ4 alignment padding")

    frame_path = work / "dce-frame.lz4"
    output_path = work / "dce-uncompressed.bin"
    frame_path.write_bytes(frame)
    run_tool([str(lz4), "-q", "-d", "-f", str(frame_path), str(output_path)], "lz4")
    decompressed = output_path.read_bytes()
    if len(decompressed) != uncompressed_size or len(decompressed) <= SHA512_SIZE:
        fail(f"{label} LZ4 output size is invalid")
    source = decompressed[:-SHA512_SIZE]
    if decompressed[-SHA512_SIZE:] != hashlib.sha512(source).digest():
        fail(f"{label} decompressed source digest is invalid")
    if len(source) % 16:
        fail(f"{label} decompressed source is not 16-byte aligned")
    return source


def run_tool(command: list[str], label: str) -> None:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        fail(f"{label} failed ({result.returncode}): {detail}")


def put_file(directory: Path, filename: str, payload: bytes) -> None:
    if Path(filename).name != filename or not filename:
        fail(f"unsafe output filename: {filename!r}")
    destination = directory / filename
    if destination.exists():
        if destination.read_bytes() != payload:
            fail(f"different payloads map to the same output filename: {filename}")
        return
    destination.write_bytes(payload)


def canonical_partition(name: str) -> str:
    if name.startswith(("A_", "B_")):
        return name[2:]
    return name


def qspi_filename(original: str, canonical: str, profile: Profile) -> str:
    if canonical == "bpmp-fw":
        return profile.bpmp_fw
    if canonical == "bpmp-fw-dtb":
        return profile.bpmp_dtb
    if canonical == "dce-fw":
        return profile.dce_qspi
    return original


def build_flash_index(
    seed: Path,
    output: Path,
    entries: list[BupEntry],
    profile: Profile,
) -> tuple[int, int]:
    rows: list[list[str]] = []
    with (seed / "flash.idx").open(newline="", encoding="utf-8") as stream:
        rows = [[field.strip() for field in row] for row in csv.reader(stream)]
    if any(len(row) != 8 for row in rows):
        fail("qualified seed flash.idx does not have eight fields per row")

    qspi_count = 0
    populated = 0
    for row in rows:
        locator = row[1]
        if not locator.startswith("3:0:"):
            continue
        qspi_count += 1
        partition = locator.split(":", 2)[2]
        canonical = canonical_partition(partition)
        original_filename = row[4]
        if not original_filename:
            continue
        populated += 1
        if canonical == "eks":
            payload = (seed / "eks_t234_sigheader.img.encrypt").read_bytes()
            filename = "eks_t234_sigheader.img.encrypt"
        else:
            bup_name = QSPI_PART_TO_BUP.get(canonical)
            if bup_name is None:
                fail(f"no allowlisted BUP mapping for populated QSPI partition {partition}")
            payload = select_entry(entries, bup_name, profile.tnspec).payload
            filename = qspi_filename(original_filename, canonical, profile)
            if canonical == "mb2":
                payload = disable_absent_cvb_eeprom(
                    payload, f"P3767-{profile.sku} {partition}"
                )
        partition_size = int(row[3], 0)
        if len(payload) > partition_size:
            fail(
                f"payload for {partition} is {len(payload)} bytes, larger than "
                f"its {partition_size}-byte partition"
            )
        put_file(output, filename, payload)
        row[4] = filename
        row[5] = str(len(payload))
        row[7] = hashlib.sha1(payload).hexdigest()

    if qspi_count != 61 or populated != 52:
        fail(
            f"qualified seed QSPI layout has {qspi_count} partitions/{populated} "
            "payloads, expected 61/52"
        )
    with (output / "flash.idx").open("w", encoding="utf-8", newline="") as stream:
        for row in rows:
            stream.write(", ".join(row) + "\n")
    return qspi_count, populated


def build_rcm_components(
    seed: Path,
    output: Path,
    entries: list[BupEntry],
    profile: Profile,
    build_bch: Path,
    apply_delta: Path,
    lz4: Path,
    work: Path,
) -> None:
    cold_name = "mb1_cold_boot_bct_MB1_sigheader.bct.encrypt"
    recovery_name = "mb1_bct_MB1_sigheader.bct.encrypt"
    cold_seed_component = (seed / cold_name).read_bytes()
    recovery_seed_component = (seed / recovery_name).read_bytes()
    cold_seed_payload = single_bch_payload(cold_seed_component, b"MBCT", cold_name)
    recovery_seed_payload = single_bch_payload(
        recovery_seed_component, b"MBCT", recovery_name
    )
    selected_cold = single_bch_payload(
        select_entry(entries, "MB1_BCT", profile.tnspec).payload,
        b"MBCT",
        f"P3767-{profile.sku} MB1_BCT",
    )
    for label, payload in (
        ("cold seed MB1 BCT", cold_seed_payload),
        ("recovery seed MB1 BCT", recovery_seed_payload),
        ("selected cold MB1 BCT", selected_cold),
    ):
        if len(payload) <= SHA512_SIZE or payload[-SHA512_SIZE:] != hashlib.sha512(
            payload[:-SHA512_SIZE]
        ).digest():
            fail(f"{label} does not end with its raw-payload SHA-512")

    baseline_path = work / "mb1-cold.raw"
    updated_path = work / "mb1-recovery.raw"
    selected_path = work / "mb1-selected.raw"
    patched_path = work / "mb1-patched.raw"
    baseline_path.write_bytes(cold_seed_payload[:-SHA512_SIZE])
    updated_path.write_bytes(recovery_seed_payload[:-SHA512_SIZE])
    selected_path.write_bytes(selected_cold[:-SHA512_SIZE])
    run_tool(
        [
            sys.executable,
            str(apply_delta),
            str(baseline_path),
            str(updated_path),
            str(selected_path),
            str(patched_path),
        ],
        "fixed MB1-BCT recovery update",
    )
    mb1_output = output / recovery_name
    run_tool(
        [
            sys.executable,
            str(build_bch),
            str(seed / recovery_name),
            "MBCT",
            str(patched_path),
            str(mb1_output),
            "--append-payload-sha512",
        ],
        "MB1-BCT BCH builder",
    )
    single_bch_payload(mb1_output.read_bytes(), b"MBCT", recovery_name)

    memory_source = memory_slot_zero(
        select_entry(entries, "MEM_BCT", profile.tnspec).payload,
        f"P3767-{profile.sku} MEM_BCT",
    )
    memory_path = work / "memory-slot-zero.raw"
    memory_path.write_bytes(memory_source)
    memory_output = output / "mem_rcm_sigheader.bct.encrypt"
    run_tool(
        [
            sys.executable,
            str(build_bch),
            str(seed / "mem_rcm_sigheader.bct.encrypt"),
            "MEM0",
            str(memory_path),
            str(memory_output),
            "--no-pad",
        ],
        "memory RCM BCH builder",
    )
    if single_bch_payload(
        memory_output.read_bytes(), b"MEM0", "generated memory RCM"
    ) != memory_source:
        fail("generated memory RCM payload changed during BCH construction")

    dce_source = compressed_dce_source(
        select_entry(entries, "dce-fw", profile.tnspec).payload,
        lz4,
        work,
        f"P3767-{profile.sku} compressed DCE",
    )
    dce_source_path = work / "dce-source.raw"
    dce_source_path.write_bytes(dce_source)
    dce_output = output / profile.dce_rcm
    seed_dce = (
        seed
        / "display-t234-dce_with_kernel_tegra234-p3768-0000+p3767-0001-nv_sigheader.bin.encrypt"
    )
    run_tool(
        [
            sys.executable,
            str(build_bch),
            str(seed_dce),
            "DCEF",
            str(dce_source_path),
            str(dce_output),
            "--no-pad",
        ],
        "DCE RCM BCH builder",
    )
    if single_bch_payload(
        dce_output.read_bytes(), b"DCEF", "generated DCE RCM"
    ) != dce_source:
        fail("generated DCE RCM payload changed during BCH construction")

    if profile.sku == "0001":
        exact = {
            recovery_name: seed / recovery_name,
            "mem_rcm_sigheader.bct.encrypt": seed / "mem_rcm_sigheader.bct.encrypt",
            profile.dce_rcm: seed_dce,
        }
        for filename, reference in exact.items():
            if (output / filename).read_bytes() != reference.read_bytes():
                fail(f"P3767-0001 reconstruction is not byte-exact: {filename}")


def write_marker_and_sums(
    output: Path, profile_name: str, profile: Profile
) -> None:
    marker = output / "HELM_R39_FAMILY_INPUT_V1"
    marker.write_text(
        "\n".join(
            (
                "FORMAT=HELM_R39_FAMILY_INPUT_V1",
                f"PROFILE={profile_name}",
                "BOARD_ID=3767",
                f"BOARD_SKU={profile.sku}",
                "BSP_RELEASE=39.2",
                "SIGNING=zerosbk",
                "SOURCE=NVIDIA_R39.2_MULTI_SPEC_BUP",
                f"NVIDIA_CAPSULE_SHA256={CAPSULE_SHA256}",
                f"NVIDIA_BUP_SHA256={BUP_SHA256}",
                f"SEED_FLASH_INDEX_SHA256={SEED_HASHES['flash.idx']}",
                "",
            )
        ),
        encoding="utf-8",
    )
    files = sorted(
        path for path in output.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    )
    with (output / "SHA256SUMS").open("w", encoding="utf-8") as stream:
        for path in files:
            stream.write(f"{sha256_file(path)}  {path.name}\n")


def build(arguments: argparse.Namespace) -> None:
    assert arguments.profile is not None
    assert arguments.capsule is not None
    assert arguments.seed is not None
    assert arguments.output is not None
    assert arguments.build_bch is not None
    assert arguments.apply_bct_delta is not None
    assert arguments.lz4 is not None
    profile = PROFILES[arguments.profile]
    seed = arguments.seed.resolve()
    output = arguments.output.resolve()
    if output.exists():
        fail(f"output already exists: {output}")
    if output == seed or seed in output.parents:
        fail("output must not be the qualified seed or one of its descendants")
    verify_seed(seed)
    verify_helper(arguments.build_bch, BUILD_BCH_SHA256, "build-bch")
    verify_helper(
        arguments.apply_bct_delta, APPLY_BCT_DELTA_SHA256, "apply-bct-delta"
    )
    if not arguments.lz4.is_file() or not os.access(arguments.lz4, os.X_OK):
        fail(f"lz4 executable is missing: {arguments.lz4}")
    entries = parse_capsule(arguments.capsule)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    work = temporary / ".work"
    work.mkdir()
    try:
        qspi_count, populated = build_flash_index(seed, temporary, entries, profile)
        for filename in COMMON_RCM_FILES:
            payload = (seed / filename).read_bytes()
            if filename == "mb2_t234_with_mb2_bct_MB2_sigheader.bin.encrypt":
                payload = disable_absent_cvb_eeprom(payload, "R39 recovery MB2")
            put_file(temporary, filename, payload)
        build_rcm_components(
            seed,
            temporary,
            entries,
            profile,
            arguments.build_bch.resolve(),
            arguments.apply_bct_delta.resolve(),
            arguments.lz4.resolve(),
            work,
        )
        shutil.rmtree(work)
        write_marker_and_sums(temporary, arguments.profile, profile)
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(
        f"Prepared {arguments.profile} from NVIDIA's pinned R39.2 multi-spec BUP "
        f"({qspi_count} QSPI partitions, {populated} populated) in {output}"
    )


def self_test() -> None:
    spec = "3767-000-0001--1-0-jetson-orin-nano-devkit-"
    fixtures = (("shared", "", b"one"), ("specific", spec, b"two"))
    count = len(fixtures)
    table_end = BUP_HEADER.size + count * BUP_ENTRY.size
    cursor = table_end
    table = bytearray()
    payloads = bytearray()
    for name, tnspec, payload in fixtures:
        table.extend(
            BUP_ENTRY.pack(
                name.encode("ascii").ljust(40, b"\0"),
                cursor,
                len(payload),
                BUP_COMPONENT_VERSION,
                0,
                tnspec.encode("ascii").ljust(128, b"\0"),
            )
        )
        payloads.extend(payload)
        cursor += len(payload)
    blob = (
        BUP_HEADER.pack(BUP_MAGIC, 7, cursor, BUP_HEADER.size, count, 0, cursor)
        + table
        + payloads
    )
    entries = parse_bup(
        blob, expected_version=7, expected_count=count, expected_sha256=None
    )
    if select_entry(entries, "shared", spec).payload != b"one":
        fail("self-test shared BUP selection failed")
    if select_entry(entries, "specific", spec).payload != b"two":
        fail("self-test exact BUP selection failed")
    if canonical_partition("A_MEM_BCT") != "MEM_BCT":
        fail("self-test QSPI canonicalization failed")

    mb2_payload = bytearray(R39_MB2_EEPROM_ADDRESS_OFFSET + 0x100)
    mb2_payload[: len(MB2_RAW_MAGIC)] = MB2_RAW_MAGIC
    struct.pack_into(
        "<I", mb2_payload, R39_MB2_CVM_EEPROM_READ_SIZE_OFFSET, 0x100
    )
    struct.pack_into(
        "<I", mb2_payload, R39_MB2_CVB_EEPROM_READ_SIZE_OFFSET, 0x100
    )
    mb2_payload[
        R39_MB2_EEPROM_ADDRESS_OFFSET : R39_MB2_EEPROM_ADDRESS_OFFSET + 2
    ] = bytes((R39_MB2_CVM_EEPROM_ADDRESS, R39_MB2_CVB_EEPROM_ADDRESS))
    mb2_header = bytearray(BCH_HEADER_SIZE)
    mb2_header[:4] = BCH_MAGIC
    mb2_digest = hashlib.sha512(mb2_payload).digest()
    for descriptor, sha_offset in (
        (BCH_STAGE2_OFFSET, BCH_STAGE2_SHA_OFFSET),
        (BCH_STAGE1_OFFSET, BCH_STAGE1_SHA_OFFSET),
    ):
        mb2_header[descriptor : descriptor + 4] = b"MB2B"
        struct.pack_into("<I", mb2_header, descriptor + 4, len(mb2_payload))
        mb2_header[sha_offset : sha_offset + SHA512_SIZE] = mb2_digest
    mb2_header[BCH_INNER_SHA_OFFSET : BCH_INNER_SHA_OFFSET + SHA512_SIZE] = (
        hashlib.sha512(mb2_header[BCH_INNER_SHA_INPUT_OFFSET:]).digest()
    )
    mb2_header[BCH_OUTER_SHA_OFFSET : BCH_OUTER_SHA_OFFSET + SHA512_SIZE] = (
        hashlib.sha512(mb2_header[BCH_OUTER_SHA_INPUT_OFFSET:]).digest()
    )
    mb2_component = bytes(mb2_header) + bytes(mb2_payload)
    patched_mb2 = disable_absent_cvb_eeprom(mb2_component, "self-test MB2")
    patched_payload = single_bch_payload(patched_mb2, b"MB2B", "patched fixture")
    if struct.unpack_from(
        "<I", patched_payload, R39_MB2_CVM_EEPROM_READ_SIZE_OFFSET
    )[0] != 0x100:
        fail("self-test MB2 mutation changed the module EEPROM read size")
    if struct.unpack_from(
        "<I", patched_payload, R39_MB2_CVB_EEPROM_READ_SIZE_OFFSET
    )[0] != 0:
        fail("self-test MB2 mutation did not disable the carrier EEPROM")
    if disable_absent_cvb_eeprom(patched_mb2, "patched fixture") != patched_mb2:
        fail("self-test MB2 mutation is not idempotent")

    invalid_payload = bytearray(mb2_payload)
    invalid_payload[R39_MB2_EEPROM_ADDRESS_OFFSET + 1] = 0xAC
    invalid_mb2 = rebuild_single_bch_payload(
        mb2_component, b"MB2B", bytes(invalid_payload), "invalid-address fixture"
    )
    try:
        disable_absent_cvb_eeprom(invalid_mb2, "invalid-address fixture")
    except SystemExit:
        pass
    else:
        fail("self-test MB2 mutation accepted an unexpected EEPROM address")
    print("build-r39-family-inputs.py self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--capsule", type=Path)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--build-bch", type=Path)
    parser.add_argument("--apply-bct-delta", type=Path)
    parser.add_argument("--lz4", type=Path)
    arguments = parser.parse_args()
    if arguments.self_test:
        if any(
            value is not None
            for value in (
                arguments.capsule,
                arguments.seed,
                arguments.output,
                arguments.profile,
                arguments.build_bch,
                arguments.apply_bct_delta,
                arguments.lz4,
            )
        ):
            parser.error("--self-test does not accept build arguments")
        self_test()
        return 0
    missing = [
        flag
        for flag, value in (
            ("--capsule", arguments.capsule),
            ("--seed", arguments.seed),
            ("--output", arguments.output),
            ("--profile", arguments.profile),
            ("--build-bch", arguments.build_bch),
            ("--apply-bct-delta", arguments.apply_bct_delta),
            ("--lz4", arguments.lz4),
        )
        if value is None
    ]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))
    build(arguments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
