#!/usr/bin/env python3
"""Validate and package Helm's five browser RCM bundles deterministically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CATALOG_VERSION = 1
GENERATED_MARKER = ".helm-web-generated"
PROFILE_KEYS = (
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
)
HASHED_FILES = (
    "br_bct_BR.bct",
    "mb1_t234_prod_aligned_sigheader.bin.encrypt",
    "psc_bl1_t234_prod_aligned_sigheader.bin.encrypt",
    "mb1_bct_MB1_sigheader.bct.encrypt",
    "mem_rcm_sigheader.bct.encrypt",
    "blob.bin",
    "PROFILE",
)
BUNDLE_FILES = (*HASHED_FILES, "SHA256SUMS")
MODULE_FILES = (
    "bundle.js",
    "sha256.js",
    "webusb-rcm.js",
    "index.js",
    "recovery-protocol.mjs",
    "web-serial-transport.mjs",
)


class PackageError(RuntimeError):
    """A deterministic, user-facing packaging failure."""


@dataclass(frozen=True)
class ProfileDefinition:
    id: str
    label: str
    board: str
    sku: str
    product_id: int

    @property
    def directory(self) -> str:
        return f"rcm-r39.2-p3767-{self.sku}"

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "PROFILE": f"{self.id}-ram-recovery",
            "BSP_RELEASE": "39.2",
            "BOARD": self.board,
            "CARRIER": "Diode_Helm",
            "SIGNING": "zerosbk",
            "HOST_BUILD": "macos-native",
            "HOST_BOOT_PERSISTENT_WRITE": "none",
            "TARGET_INSTALLERS": "guarded-nvme-and-qspi",
            "USB_PID": f"0x{self.product_id:04x}",
            "QSPI_PROFILE": self.id,
            "KERNEL_DTB": f"helm-p3767-{self.sku}.dtb",
        }


PROFILES = (
    ProfileDefinition(
        "helm-orin-nx-16gb-r39.2",
        "Jetson Orin NX 16GB",
        "P3767-0000",
        "0000",
        0x7323,
    ),
    ProfileDefinition(
        "helm-orin-nx-8gb-r39.2",
        "Jetson Orin NX 8GB",
        "P3767-0001",
        "0001",
        0x7423,
    ),
    ProfileDefinition(
        "helm-orin-nano-8gb-r39.2",
        "Jetson Orin Nano 8GB",
        "P3767-0003",
        "0003",
        0x7523,
    ),
    ProfileDefinition(
        "helm-orin-nano-4gb-r39.2",
        "Jetson Orin Nano 4GB",
        "P3767-0004",
        "0004",
        0x7623,
    ),
    ProfileDefinition(
        "helm-orin-nano-8gb-sd-r39.2",
        "Jetson Orin Nano 8GB dev-kit/SD",
        "P3767-0005",
        "0005",
        0x7523,
    ),
)


def fail(message: str) -> None:
    raise PackageError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        fail(f"required file is missing: {path}")
    if not stat.S_ISREG(mode):
        fail(f"required path is not a regular file: {path}")
    if path.stat().st_size <= 0:
        fail(f"required file is empty: {path}")


def read_strict_text(path: Path, maximum_bytes: int) -> str:
    require_regular_file(path)
    size = path.stat().st_size
    if size > maximum_bytes:
        fail(f"metadata exceeds {maximum_bytes} bytes: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"metadata is not valid UTF-8: {path}")
    if "\r" in text or not text.endswith("\n"):
        fail(f"metadata must use LF and end with a newline: {path}")
    if "\x00" in text:
        fail(f"metadata contains NUL: {path}")
    return text


def parse_profile(path: Path, definition: ProfileDefinition) -> None:
    values: dict[str, str] = {}
    lines = read_strict_text(path, 4096).removesuffix("\n").split("\n")
    if len(lines) != len(PROFILE_KEYS):
        fail(f"PROFILE must contain exactly {len(PROFILE_KEYS)} lines: {path}")
    for line in lines:
        if "=" not in line:
            fail(f"invalid PROFILE line in {path}: {line!r}")
        key, value = line.split("=", 1)
        if key not in PROFILE_KEYS or not value or key in values:
            fail(f"invalid or duplicate PROFILE key in {path}: {key!r}")
        values[key] = value
    if tuple(values) != PROFILE_KEYS:
        fail(f"PROFILE keys or order do not match the fixed schema: {path}")
    expected = definition.metadata
    for key in PROFILE_KEYS:
        if values[key] != expected[key]:
            fail(
                f"PROFILE {key} is {values[key]!r}; "
                f"expected {expected[key]!r}: {path}"
            )


def parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    lines = read_strict_text(path, 4096).removesuffix("\n").split("\n")
    if len(lines) != len(HASHED_FILES):
        fail(f"SHA256SUMS must contain exactly {len(HASHED_FILES)} lines: {path}")
    for expected_name, line in zip(HASHED_FILES, lines, strict=True):
        parts = line.split("  ")
        if len(parts) != 2:
            fail(f"invalid SHA256SUMS line in {path}: {line!r}")
        digest, name = parts
        if name != expected_name:
            fail(
                f"SHA256SUMS entry is {name!r}; expected {expected_name!r}: {path}"
            )
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            fail(f"invalid SHA-256 digest for {name}: {path}")
        if name in checksums:
            fail(f"duplicate SHA256SUMS entry for {name}: {path}")
        checksums[name] = digest
    return checksums


def validate_bundle(source_root: Path, definition: ProfileDefinition) -> dict[str, object]:
    directory = source_root / definition.directory
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        fail(f"required RCM bundle is missing: {directory}")
    if not stat.S_ISDIR(mode):
        fail(f"RCM bundle path is not a directory: {directory}")
    observed = tuple(sorted(entry.name for entry in directory.iterdir()))
    expected_names = tuple(sorted(BUNDLE_FILES))
    if observed != expected_names:
        missing = sorted(set(expected_names) - set(observed))
        extra = sorted(set(observed) - set(expected_names))
        fail(f"bundle contents differ for {directory}; missing={missing}, extra={extra}")

    for name in BUNDLE_FILES:
        require_regular_file(directory / name)
    parse_profile(directory / "PROFILE", definition)
    declared = parse_checksums(directory / "SHA256SUMS")

    files: list[dict[str, object]] = []
    public_path = f"bundles/{definition.directory}"
    for name in BUNDLE_FILES:
        path = directory / name
        digest = sha256_file(path)
        if name in declared and digest != declared[name]:
            fail(
                f"SHA-256 mismatch for {path}: declared {declared[name]}, observed {digest}"
            )
        files.append(
            {
                "name": name,
                "size": path.stat().st_size,
                "sha256": digest,
                "url": f"{public_path}/{name}",
            }
        )
    return {
        "id": definition.id,
        "label": definition.label,
        "board": definition.board,
        "sku": definition.sku,
        "productId": definition.product_id,
        "path": public_path,
        "files": files,
    }


def validate_bundles(source_root: Path) -> list[dict[str, object]]:
    if not source_root.is_dir():
        fail(f"RCM bundle source directory is missing: {source_root}")
    expected_directories = {definition.directory for definition in PROFILES}
    observed_directories = {
        entry.name for entry in source_root.glob("rcm-r39.2-p3767-*")
    }
    if observed_directories != expected_directories:
        missing = sorted(expected_directories - observed_directories)
        extra = sorted(observed_directories - expected_directories)
        fail(f"expected exactly five RCM bundle directories; missing={missing}, extra={extra}")
    return [validate_bundle(source_root, definition) for definition in PROFILES]


def copy_regular(source: Path, destination: Path, mode: str = "copy") -> None:
    require_regular_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hardlink":
        try:
            os.link(source, destination)
            return
        except OSError:
            pass
    shutil.copy2(source, destination, follow_symlinks=False)


def copy_frontend(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        fail(f"frontend source directory is missing or unsafe: {source}")
    entries = sorted(source.rglob("*"), key=lambda entry: entry.as_posix())
    hidden = [
        entry
        for entry in entries
        if any(part.startswith(".") for part in entry.relative_to(source).parts)
    ]
    if hidden:
        fail(f"frontend contains a hidden path: {hidden[0]}")
    files = [entry for entry in entries if entry.is_file() and not entry.is_symlink()]
    unsafe = [
        entry
        for entry in entries
        if entry.is_symlink() or (not entry.is_dir() and not entry.is_file())
    ]
    if unsafe:
        fail(f"frontend contains a symlink or special file: {unsafe[0]}")
    required = {"index.html", "styles.css", "app.mjs"}
    top_level = {entry.relative_to(source).as_posix() for entry in files}
    missing = sorted(required - top_level)
    if missing:
        fail(f"frontend is missing required files: {missing}")
    for entry in entries:
        relative = entry.relative_to(source)
        target = destination / relative
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            copy_regular(entry, target)


def require_safe_output(output: Path) -> None:
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            fail(f"refusing to replace non-directory output: {output}")
        marker = output / GENERATED_MARKER
        try:
            marker_mode = marker.lstat().st_mode
        except FileNotFoundError:
            marker_mode = 0
        if (
            not stat.S_ISREG(marker_mode)
            or marker.read_text(encoding="ascii") != "helm-web-site-v1\n"
        ):
            fail(f"refusing to replace output without {GENERATED_MARKER}: {output}")


def path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def publish_stage(stage: Path, output: Path) -> None:
    require_safe_output(output)
    previous: Path | None = None
    if output.exists():
        previous = output.parent / f".{output.name}.previous-{os.getpid()}"
        if previous.exists() or previous.is_symlink():
            fail(f"temporary previous-output path already exists: {previous}")
        os.replace(output, previous)
    try:
        os.replace(stage, output)
    except Exception:
        if previous is not None and not output.exists():
            os.replace(previous, output)
        raise
    if previous is not None:
        shutil.rmtree(previous)


def prepare_site(
    source_root: Path,
    web_root: Path,
    frontend_root: Path,
    output: Path,
    bundle_mode: str,
) -> dict[str, object]:
    source_root = source_root.resolve()
    web_root = web_root.resolve()
    frontend_root = frontend_root.resolve()
    # Preserve the final path component so an existing output symlink can be
    # rejected instead of silently resolving and replacing its target.
    output = Path(os.path.abspath(output))
    profiles = validate_bundles(source_root)

    for name in (*MODULE_FILES, "server.mjs"):
        require_regular_file(web_root / name)
    resolved_output = output.resolve(strict=False)
    if any(
        path_contains(resolved_output, source)
        or path_contains(source, resolved_output)
        for source in (source_root, web_root, frontend_root)
    ):
        fail("output and source directories must not contain one another")
    output.parent.mkdir(parents=True, exist_ok=True)
    require_safe_output(output)

    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        copy_frontend(frontend_root, stage)
        for name in MODULE_FILES:
            copy_regular(web_root / name, stage / "modules" / name)
        copy_regular(web_root / "server.mjs", stage / "server.mjs")

        for definition in PROFILES:
            source_directory = source_root / definition.directory
            destination_directory = stage / "bundles" / definition.directory
            for name in BUNDLE_FILES:
                copy_regular(
                    source_directory / name,
                    destination_directory / name,
                    mode=bundle_mode,
                )

        catalog: dict[str, object] = {
            "version": CATALOG_VERSION,
            "profiles": profiles,
        }
        (stage / "catalog.json").write_text(
            json.dumps(catalog, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (stage / GENERATED_MARKER).write_text("helm-web-site-v1\n", encoding="ascii")
        publish_stage(stage, output)
        return catalog
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def profile_text(definition: ProfileDefinition) -> str:
    return "".join(f"{key}={definition.metadata[key]}\n" for key in PROFILE_KEYS)


def write_fixture_bundle(root: Path, definition: ProfileDefinition) -> None:
    directory = root / definition.directory
    directory.mkdir(parents=True)
    for index, name in enumerate(HASHED_FILES[:-1], start=1):
        (directory / name).write_bytes(f"fixture:{definition.sku}:{index}:{name}\n".encode())
    (directory / "PROFILE").write_text(profile_text(definition), encoding="utf-8")
    checksums = "".join(
        f"{sha256_file(directory / name)}  {name}\n" for name in HASHED_FILES
    )
    (directory / "SHA256SUMS").write_text(checksums, encoding="ascii")


def expect_failure(action, message: str) -> None:
    try:
        action()
    except PackageError:
        return
    raise AssertionError(message)


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="helm-web-package-test-") as temporary:
        root = Path(temporary)
        bundles = root / "bundles"
        web = root / "web"
        frontend = web / "site"
        output = root / "output" / "site"
        bundles.mkdir()
        frontend.mkdir(parents=True)
        for definition in PROFILES:
            write_fixture_bundle(bundles, definition)
        for name in MODULE_FILES:
            (web / name).write_text(f"// fixture {name}\n", encoding="utf-8")
        (web / "server.mjs").write_text("// fixture server\n", encoding="utf-8")
        (frontend / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (frontend / "styles.css").write_text("body {}\n", encoding="utf-8")
        (frontend / "app.mjs").write_text("export {};\n", encoding="utf-8")

        catalog = prepare_site(bundles, web, frontend, output, "copy")
        assert catalog["version"] == 1
        profiles = catalog["profiles"]
        assert isinstance(profiles, list) and len(profiles) == 5
        assert [profile["sku"] for profile in profiles] == [
            "0000",
            "0001",
            "0003",
            "0004",
            "0005",
        ]
        assert profiles[1]["productId"] == 0x7423
        assert len(profiles[0]["files"]) == len(BUNDLE_FILES)
        assert (output / "modules" / "webusb-rcm.js").is_file()
        assert (output / "bundles" / PROFILES[0].directory / "blob.bin").is_file()
        serialized = (output / "catalog.json").read_text(encoding="utf-8")
        assert serialized == json.dumps(catalog, indent=2, sort_keys=True) + "\n"

        tampered = bundles / PROFILES[2].directory / "blob.bin"
        with tampered.open("ab") as stream:
            stream.write(b"tampered")
        expect_failure(
            lambda: validate_bundles(bundles),
            "a tampered bundle payload was accepted",
        )

        unsafe_output = root / "unsafe-output"
        unsafe_output.mkdir()
        expect_failure(
            lambda: require_safe_output(unsafe_output),
            "an unmarked output directory was accepted",
        )
    print("prepare-site: self-test passed")


def parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    repository = script_dir.parent.parent
    argument_parser = argparse.ArgumentParser(
        description="Validate and package Helm browser flashing assets",
    )
    argument_parser.add_argument(
        "command",
        nargs="?",
        choices=("prepare", "self-test"),
        default="prepare",
    )
    argument_parser.add_argument(
        "--source",
        type=Path,
        default=repository / "build" / "helm-macos",
        help="directory containing the five rcm-r39.2-p3767-* bundles",
    )
    argument_parser.add_argument(
        "--frontend",
        type=Path,
        default=script_dir / "site",
        help="static frontend source tree",
    )
    argument_parser.add_argument(
        "--output",
        type=Path,
        default=repository / "build" / "helm-web" / "site",
        help="generated static-site directory",
    )
    argument_parser.add_argument(
        "--bundle-mode",
        choices=("copy", "hardlink"),
        default="hardlink",
        help="hardlink avoids duplicating large local blobs and falls back to copy",
    )
    return argument_parser


def main(arguments: Iterable[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    if options.command == "self-test":
        self_test()
        return 0
    script_dir = Path(__file__).resolve().parent
    try:
        catalog = prepare_site(
            options.source,
            script_dir,
            options.frontend,
            options.output,
            options.bundle_mode,
        )
    except PackageError as error:
        print(f"prepare-site: {error}", file=sys.stderr)
        return 1
    print(
        f"prepare-site: packaged {len(catalog['profiles'])} profiles at "
        f"{options.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
