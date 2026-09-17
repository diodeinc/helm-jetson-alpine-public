#!/usr/bin/env python3
"""Validate or install an owned Jetson Orin QSPI firmware catalog.

The default is a non-destructive catalog inspection.  --build-image assembles
a complete raw SPI image on any host.  --execute performs a full recovery-mode
reprovision: erase all SPI NOR, write catalog payloads, and read every payload
back.  It does not invoke tegradevflash, tegraparser, or another NVIDIA host
executable.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys


SCHEMA = "jetson-orin-firmware-catalog-v1"
MEMERASE = 0x40084D02  # _IOW('M', 2, struct erase_info_user)
MEMSYNC = 0x00004D03   # _IO('M', 3)


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_digest(descriptor: int, offset: int, size: int) -> str:
    result = hashlib.sha256()
    remaining = size
    while remaining:
        block = os.pread(descriptor, min(1024 * 1024, remaining), offset)
        if not block:
            raise OSError(f"short read at offset {offset}")
        result.update(block)
        offset += len(block)
        remaining -= len(block)
    return result.hexdigest()


def write_all(descriptor: int, source: Path, offset: int) -> None:
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            written = 0
            while written < len(block):
                count = os.pwrite(descriptor, block[written:], offset + written)
                if count <= 0:
                    raise OSError(f"short write at offset {offset + written}")
                written += count
            offset += len(block)


def load_catalog(manifest_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != SCHEMA:
        raise ValueError(f"unsupported manifest schema: {manifest.get('schema')!r}")
    storage = manifest.get("storage")
    partitions = manifest.get("partitions")
    if not isinstance(storage, dict) or storage.get("kind") != "spi-nor":
        raise ValueError("manifest is not for SPI NOR")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("manifest has no partitions")

    storage_size = int(storage["size"])
    names: set[str] = set()
    previous_end = 0
    populated = 0
    catalog_root = manifest_path.parent.resolve()
    for partition in sorted(partitions, key=lambda item: int(item["offset"])):
        name = str(partition["name"])
        offset = int(partition["offset"])
        size = int(partition["size"])
        if name in names:
            raise ValueError(f"duplicate partition: {name}")
        names.add(name)
        if offset < previous_end or size <= 0 or offset + size > storage_size:
            raise ValueError(f"invalid bounds for partition {name}")
        previous_end = offset + size
        image = partition.get("image")
        if image is None:
            continue
        if not isinstance(image, dict):
            raise ValueError(f"partition {name}: malformed image entry")
        relative = Path(str(image["path"]))
        source = (catalog_root / relative).resolve()
        if catalog_root not in source.parents:
            raise ValueError(f"partition {name}: image escapes catalog")
        if not source.is_file():
            raise FileNotFoundError(f"partition {name}: missing {source}")
        if source.stat().st_size != int(image["size"]):
            raise ValueError(f"partition {name}: image size mismatch")
        if int(image["size"]) > size:
            raise ValueError(f"partition {name}: image exceeds partition")
        if file_digest(source) != image["sha256"]:
            raise ValueError(f"partition {name}: SHA-256 mismatch")
        image["_source"] = source
        populated += 1
    if populated == 0:
        raise ValueError("manifest has no populated partitions")
    return manifest, partitions


def sysfs_number(device: Path, field: str) -> int:
    name = device.name
    value = Path("/sys/class/mtd") / name / field
    if not value.is_file():
        raise FileNotFoundError(f"cannot determine {field} for {device}: {value} is absent")
    return int(value.read_text().strip(), 0)


def copy_backup(descriptor: int, target: Path, size: int) -> None:
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        offset = 0
        while offset < size:
            block = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not block:
                raise OSError(f"short backup read at offset {offset}")
            stream.write(block)
            offset += len(block)
        stream.flush()
        os.fsync(stream.fileno())
    print(f"Backup: {target} ({file_digest(target)})")


def erase_all(descriptor: int, size: int, erase_size: int) -> None:
    for offset in range(0, size, erase_size):
        fcntl.ioctl(descriptor, MEMERASE, struct.pack("II", offset, erase_size))
        if offset % (8 * 1024 * 1024) == 0:
            print(f"Erasing: {offset // (1024 * 1024)} / {size // (1024 * 1024)} MiB")


def write_offsets(partition: dict[str, object], erase_size: int) -> list[int]:
    offset = int(partition["offset"])
    if partition["name"] != "BCT":
        return [offset]
    image_size = int(partition["image"]["size"])
    size = int(partition["size"])
    if offset != 0:
        raise ValueError("BCT redundancy is only supported for a BCT at offset zero")
    if image_size > erase_size or size % erase_size:
        raise ValueError("BCT image/partition is incompatible with the erase block size")
    return list(range(offset, offset + size, erase_size))


def build_image(
    target: Path,
    size: int,
    partitions: list[dict[str, object]],
    erase_size: int,
) -> None:
    checksum = target.with_name(target.name + ".sha256")
    if target.exists() or checksum.exists():
        raise FileExistsError(f"image or checksum output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            erased = b"\xff" * (1024 * 1024)
            remaining = size
            while remaining:
                block = erased[: min(len(erased), remaining)]
                stream.write(block)
                remaining -= len(block)
            for partition in partitions:
                if partition.get("image") is None:
                    continue
                source = partition["image"]["_source"]
                for offset in write_offsets(partition, erase_size):
                    stream.seek(offset)
                    with source.open("rb") as payload:
                        while block := payload.read(1024 * 1024):
                            stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())

        descriptor = os.open(target, os.O_RDONLY)
        try:
            for partition in partitions:
                if partition.get("image") is None:
                    continue
                expected = str(partition["image"]["sha256"])
                image_size = int(partition["image"]["size"])
                for offset in write_offsets(partition, erase_size):
                    if read_digest(descriptor, offset, image_size) != expected:
                        raise OSError(
                            f"assembled image mismatch for {partition['name']} at {offset}"
                        )
        finally:
            os.close(descriptor)

        image_sha256 = file_digest(target)
        checksum.write_text(f"{image_sha256}  {target.name}\n")
        print(f"Raw QSPI image: {target} ({size} bytes)")
        print(f"SHA-256: {image_sha256}")
    except BaseException:
        target.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--device", type=Path, default=Path("/dev/mtd0"))
    parser.add_argument("--backup", type=Path, help="save the complete pre-flash QSPI image")
    parser.add_argument(
        "--build-image",
        type=Path,
        help="assemble a complete raw QSPI image instead of accessing hardware",
    )
    parser.add_argument("--execute", action="store_true", help="perform the destructive write")
    parser.add_argument(
        "--confirm",
        metavar="PROFILE",
        help="must exactly match the catalog profile when --execute is used",
    )
    arguments = parser.parse_args()

    manifest_path = arguments.manifest.resolve()
    manifest, partitions = load_catalog(manifest_path)
    profile = str(manifest["profile"])
    storage = manifest["storage"]
    size = int(storage["size"])
    expected_erase_size = int(storage["erase_block_size"])
    populated = [partition for partition in partitions if partition.get("image") is not None]

    unique_images = {str(partition["image"]["path"]) for partition in populated}
    print(f"Profile: {profile}")
    print(f"Board: {manifest['board']}")
    print(f"BSP: {manifest['bsp_release']}")
    print(f"QSPI: {size} bytes, expected erase block {expected_erase_size} bytes")
    print(
        f"Plan: full erase, {len(populated)} partition writes from "
        f"{len(unique_images)} unique payloads, readback verification"
    )

    if arguments.build_image and arguments.execute:
        parser.error("--build-image and --execute are mutually exclusive")
    if arguments.build_image:
        build_image(arguments.build_image.resolve(), size, partitions, expected_erase_size)
        return 0
    if not arguments.execute:
        print("Inspection only; pass --execute --confirm " + profile + " to write QSPI.")
        return 0
    if arguments.confirm != profile:
        parser.error(f"--confirm must exactly equal {profile!r}")
    if sys.platform != "linux":
        parser.error("QSPI writes are supported only from Linux running on the Jetson")
    if os.geteuid() != 0:
        parser.error("QSPI writes require root")

    device = arguments.device.resolve()
    device_stat = device.stat()
    if not stat.S_ISCHR(device_stat.st_mode):
        parser.error(f"refusing non-character device: {device}")
    actual_size = sysfs_number(device, "size")
    erase_size = sysfs_number(device, "erasesize")
    if actual_size != size:
        parser.error(f"target size {actual_size} does not match catalog size {size}")
    if erase_size != expected_erase_size:
        parser.error(
            f"target erase block {erase_size} does not match catalog {expected_erase_size}"
        )
    if size % erase_size:
        parser.error("target size is not a multiple of its erase block size")

    descriptor = os.open(device, os.O_RDWR | os.O_SYNC)
    try:
        if arguments.backup:
            copy_backup(descriptor, arguments.backup.resolve(), size)
        erase_all(descriptor, size, erase_size)
        for partition in populated:
            source = partition["image"]["_source"]
            for offset in write_offsets(partition, erase_size):
                print(f"Writing {partition['name']} at {offset}: {source.name}")
                write_all(descriptor, source, offset)
        fcntl.ioctl(descriptor, MEMSYNC)
        os.fsync(descriptor)

        for partition in populated:
            expected = str(partition["image"]["sha256"])
            image_size = int(partition["image"]["size"])
            for offset in write_offsets(partition, erase_size):
                if read_digest(descriptor, offset, image_size) != expected:
                    raise OSError(f"readback mismatch for {partition['name']} at {offset}")
        print("QSPI installation and readback verification succeeded.")
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
