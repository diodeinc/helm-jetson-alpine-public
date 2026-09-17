#!/usr/bin/env python3
"""Import one storage device from NVIDIA flash.idx or resolved XML.

The index path verifies NVIDIA's recorded sizes and SHA-1 values.  The XML path
derives the QSPI offsets itself and hashes the referenced payloads directly,
removing ``tegraparser_v2 --generateflashindex`` from catalog creation.  The
runtime writer consumes only the normalized manifest and never invokes NVIDIA
host tooling.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET


SCHEMA = "jetson-orin-firmware-catalog-v1"
SPI_DEVICE_TYPE = 3
GPT_ENTRY_SIZE = 128
GPT_ENTRY_COUNT = 128
GPT_HEADER_SECTORS = 1


def digest(path: Path, algorithm: str) -> str:
    result = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def parse_int(value: str, field: str, line_number: int) -> int:
    try:
        result = int(value.strip(), 0)
    except ValueError as error:
        raise ValueError(f"line {line_number}: invalid {field}: {value!r}") from error
    if result < 0:
        raise ValueError(f"line {line_number}: {field} must not be negative")
    return result


def parse_index(path: Path, device_selector: str) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for line_number, row in enumerate(csv.reader(stream, skipinitialspace=True), 1):
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) != 8:
                raise ValueError(
                    f"line {line_number}: expected 8 fields, found {len(row)}"
                )
            entry, locator, offset, partition_size, filename, file_size, attributes, sha1 = (
                field.strip() for field in row
            )
            try:
                device_type, instance, partition_name = locator.split(":", 2)
            except ValueError as error:
                raise ValueError(
                    f"line {line_number}: invalid device locator {locator!r}"
                ) from error
            if f"{device_type}:{instance}" != device_selector:
                continue
            if not partition_name:
                raise ValueError(f"line {line_number}: empty partition name")

            selected.append(
                {
                    "entry": parse_int(entry, "entry", line_number),
                    "name": partition_name,
                    "offset": parse_int(offset, "offset", line_number),
                    "size": parse_int(partition_size, "partition size", line_number),
                    "attributes": attributes,
                    "oracle_sha1": sha1 or None,
                    "oracle_file_size": (
                        parse_int(file_size, "file size", line_number) if file_size else None
                    ),
                    "oracle_filename": filename or None,
                }
            )
    if not selected:
        raise ValueError(f"no entries found for device {device_selector}")
    return selected


def element_text(element: ET.Element, name: str, *, required: bool = True) -> str | None:
    child = element.find(name)
    value = "" if child is None or child.text is None else child.text.strip()
    if required and not value:
        raise ValueError(
            f"partition {element.get('name', '<unknown>')}: missing {name}"
        )
    return value or None


def align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError(f"invalid alignment: {alignment}")
    return (value + alignment - 1) // alignment * alignment


def parse_spi_xml(path: Path, device_selector: str) -> list[dict[str, object]]:
    """Derive the normalized SPI-NOR layout from resolved NVIDIA flash XML.

    This intentionally supports only the narrow QSPI layout needed by the
    current Orin profile.  It does not attempt to serialize NVIDIA's private
    in-memory ``flash.xml.bin`` ABI.
    """

    try:
        device_type_text, instance_text = device_selector.split(":", 1)
        device_type = int(device_type_text, 0)
        instance = int(instance_text, 0)
    except ValueError as error:
        raise ValueError(f"invalid device selector: {device_selector!r}") from error
    if device_type != SPI_DEVICE_TYPE:
        raise ValueError("native XML layout derivation currently supports only SPI type 3")

    root = ET.parse(path).getroot()
    matching = [
        candidate
        for candidate in root.findall("device")
        if candidate.get("type") == "spi"
        and int(candidate.get("instance", "-1"), 0) == instance
    ]
    if len(matching) != 1:
        raise ValueError(
            f"expected one SPI instance {instance} in {path}, found {len(matching)}"
        )
    device = matching[0]
    sector_size = int(device.get("sector_size", "0"), 0)
    sector_count = int(device.get("num_sectors", "0"), 0)
    if sector_size <= 0 or sector_count <= 0:
        raise ValueError("SPI device has an invalid sector size or sector count")
    storage_size = sector_size * sector_count
    generated_gpt_size = sector_size * GPT_HEADER_SECTORS + GPT_ENTRY_SIZE * GPT_ENTRY_COUNT

    selected: list[dict[str, object]] = []
    current_offset = 0
    partition_id = 1
    for entry, partition in enumerate(device.findall("partition")):
        name = partition.get("name")
        partition_type = partition.get("type", "").strip().lower()
        if not name:
            raise ValueError(f"SPI partition {entry} has no name")
        allocation_policy = element_text(partition, "allocation_policy")
        if allocation_policy != "sequential":
            raise ValueError(
                f"partition {name}: unsupported allocation policy {allocation_policy!r}"
            )
        allocation_attribute = int(
            str(element_text(partition, "allocation_attribute")), 0
        )
        if allocation_attribute == 0x8:
            index_policy = "fixed"
        elif allocation_attribute == 0x808:
            index_policy = "expand"
        else:
            raise ValueError(
                f"partition {name}: unsupported allocation attribute "
                f"{allocation_attribute:#x}"
            )

        declared_size = int(str(element_text(partition, "size")), 0)
        start_text = element_text(partition, "start_location", required=False)
        if start_text is not None:
            offset = int(start_text, 0)
            if offset < current_offset:
                raise ValueError(
                    f"partition {name}: explicit start {offset} overlaps prior allocation"
                )
        else:
            offset = current_offset

        alignment_text = element_text(partition, "align_boundary", required=False)
        if alignment_text is not None:
            offset = align_up(offset, int(alignment_text, 0))

        if partition_type == "secondary_gpt":
            if declared_size != 0xFFFFFFFFFFFFFFFF:
                raise ValueError(
                    f"partition {name}: expected secondary-GPT size sentinel"
                )
            size = generated_gpt_size
            offset = storage_size - size
            allocation_size = size
            attribute_id = 0
        elif partition_type == "backup_secondary_gpt":
            if declared_size < generated_gpt_size:
                raise ValueError(
                    f"partition {name}: allocation is too small for generated GPT"
                )
            size = generated_gpt_size
            allocation_size = declared_size
            attribute_id = partition_id
            partition_id += 1
        else:
            if declared_size <= 0 or declared_size == 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"partition {name}: invalid size {declared_size}")
            size = declared_size
            allocation_size = declared_size
            attribute_id = partition_id
            partition_id += 1

        filename = element_text(partition, "filename", required=False)
        selected.append(
            {
                "entry": entry,
                "name": name,
                "offset": offset,
                "size": size,
                "attributes": f"{index_policy}-<reserved>-{attribute_id}",
                "oracle_sha1": None,
                "oracle_file_size": None,
                "oracle_filename": filename,
            }
        )
        current_offset = offset + allocation_size

    if not selected:
        raise ValueError(f"no SPI partitions found for device {device_selector}")
    if current_offset != storage_size:
        raise ValueError(
            f"derived SPI layout ends at {current_offset}, device ends at {storage_size}"
        )
    return selected


def validate_layout(partitions: list[dict[str, object]], storage_size: int) -> None:
    names: set[str] = set()
    previous_end = 0
    for partition in sorted(partitions, key=lambda item: int(item["offset"])):
        name = str(partition["name"])
        offset = int(partition["offset"])
        size = int(partition["size"])
        if name in names:
            raise ValueError(f"duplicate partition name: {name}")
        names.add(name)
        if size <= 0:
            raise ValueError(f"partition {name}: size must be positive")
        if offset < previous_end:
            raise ValueError(f"partition {name}: overlaps the preceding partition")
        if offset + size > storage_size:
            raise ValueError(
                f"partition {name}: end {offset + size} exceeds storage size {storage_size}"
            )
        previous_end = offset + size


def safe_payload(image_directories: list[Path], filename: str) -> Path:
    relative = Path(filename)
    if relative.is_absolute() or relative.name != filename or ".." in relative.parts:
        raise ValueError(f"unsafe payload filename in flash index: {filename!r}")
    for directory in image_directories:
        source = directory / relative
        if source.is_file():
            return source
    searched = ", ".join(str(directory) for directory in image_directories)
    raise FileNotFoundError(f"payload {filename!r} does not exist under: {searched}")


def copy_oracle(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"oracle file does not exist: {source}")
    target = destination / source.name
    if target.exists():
        if digest(target, "sha256") != digest(source, "sha256"):
            raise ValueError(f"oracle basename collision: {source.name}")
        return
    shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "layout", type=Path, help="NVIDIA-generated flash.idx or resolved flash XML"
    )
    parser.add_argument("images", type=Path, help="directory containing indexed images")
    parser.add_argument("output", type=Path, help="new catalog directory")
    parser.add_argument(
        "--fallback-images",
        action="append",
        default=[],
        type=Path,
        help="additional image directory searched after IMAGES; may be repeated",
    )
    parser.add_argument("--profile", required=True, help="stable catalog profile name")
    parser.add_argument("--bsp-release", required=True, help="for example, R35.6.1")
    parser.add_argument("--board", required=True, help="human-readable board/SKU identity")
    parser.add_argument("--device", default="3:0", help="flash.idx device type and instance")
    parser.add_argument(
        "--layout-format",
        choices=("index", "xml"),
        default="index",
        help="input format (default: index; use xml to avoid generated flash.idx)",
    )
    parser.add_argument(
        "--expected-size",
        type=lambda value: int(value, 0),
        help="require this exact storage size; defaults to the largest partition end",
    )
    parser.add_argument(
        "--erase-block-size",
        type=lambda value: int(value, 0),
        default=65536,
        help="expected target erase block size (default: 65536)",
    )
    parser.add_argument(
        "--oracle",
        action="append",
        default=[],
        type=Path,
        help="additional oracle input to retain; may be repeated",
    )
    arguments = parser.parse_args()

    layout = arguments.layout.resolve()
    images = arguments.images.resolve()
    image_directories = [images, *(path.resolve() for path in arguments.fallback_images)]
    output = arguments.output.resolve()
    if output.exists():
        parser.error(f"output already exists: {output}")
    if arguments.erase_block_size <= 0:
        parser.error("--erase-block-size must be positive")

    if arguments.layout_format == "xml":
        partitions = parse_spi_xml(layout, arguments.device)
    else:
        partitions = parse_index(layout, arguments.device)
    inferred_size = max(int(item["offset"]) + int(item["size"]) for item in partitions)
    storage_size = arguments.expected_size or inferred_size
    if arguments.expected_size is not None and inferred_size != arguments.expected_size:
        raise ValueError(
            f"indexed device ends at {inferred_size}, expected {arguments.expected_size}"
        )
    validate_layout(partitions, storage_size)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payload_directory = temporary / "payloads"
        oracle_directory = temporary / "oracle"
        payload_directory.mkdir()
        oracle_directory.mkdir()

        copied: dict[str, dict[str, object]] = {}
        normalized: list[dict[str, object]] = []
        for partition in partitions:
            filename = partition.pop("oracle_filename")
            expected_file_size = partition.pop("oracle_file_size")
            expected_sha1 = partition.pop("oracle_sha1")
            image = None
            if filename is not None:
                source = safe_payload(image_directories, str(filename))
                actual_size = source.stat().st_size
                if expected_file_size is not None and expected_file_size != actual_size:
                    raise ValueError(
                        f"payload {filename}: index size {expected_file_size}, actual {actual_size}"
                    )
                if actual_size > int(partition["size"]):
                    raise ValueError(
                        f"payload {filename}: {actual_size} bytes exceed partition "
                        f"{partition['name']} ({partition['size']} bytes)"
                    )
                actual_sha1 = digest(source, "sha1")
                if expected_sha1 is not None and actual_sha1 != expected_sha1:
                    raise ValueError(
                        f"payload {filename}: SHA-1 does not match NVIDIA flash index"
                    )
                if filename not in copied:
                    destination = payload_directory / str(filename)
                    shutil.copy2(source, destination)
                    copied[str(filename)] = {
                        "path": f"payloads/{filename}",
                        "size": actual_size,
                        "sha1": actual_sha1,
                        "sha256": digest(destination, "sha256"),
                    }
                image = copied[str(filename)]

            normalized.append({**partition, "image": image})

        copy_oracle(layout, oracle_directory)
        for oracle in arguments.oracle:
            copy_oracle(oracle.resolve(), oracle_directory)

        device_type, instance = (int(value, 0) for value in arguments.device.split(":", 1))
        manifest = {
            "schema": SCHEMA,
            "profile": arguments.profile,
            "bsp_release": arguments.bsp_release,
            "board": arguments.board,
            "storage": {
                "kind": "spi-nor" if device_type == 3 else "block",
                "oracle_device_type": device_type,
                "oracle_instance": instance,
                "size": storage_size,
                "erase_block_size": arguments.erase_block_size,
            },
            "oracle": (
                {
                    "format": "nvidia-flash-xml-v1",
                    "flash_xml_sha256": digest(layout, "sha256"),
                }
                if arguments.layout_format == "xml"
                else {
                    "format": "nvidia-flash-index-v1",
                    "flash_index_sha256": digest(layout, "sha256"),
                }
            ),
            "partitions": normalized,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        hashed_files = sorted(
            path for path in temporary.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
        )
        with (temporary / "SHA256SUMS").open("w", encoding="utf-8") as sums:
            for path in hashed_files:
                sums.write(f"{digest(path, 'sha256')}  {path.relative_to(temporary)}\n")

        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    payload_count = sum(1 for item in normalized if item["image"] is not None)
    print(
        f"Imported {len(normalized)} partitions ({payload_count} populated) "
        f"for {arguments.profile} into {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
