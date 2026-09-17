#!/usr/bin/env python3
"""Generate the unfused P3767-0001 R39.2 firmware seed from the public NVIDIA BSP.

Requires Python 3 and a running Docker daemon with linux/amd64 support.
The container has no device mounts and only runs NVIDIA's offline signing path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import uuid


BSP_URL = (
    "https://developer.download.nvidia.com/embedded/L4T/r39_Release_v2.0/"
    "release/Jetson_Linux_R39.2.0_aarch64.tbz2"
)
BSP_SHA256 = "1626626cd827de0e350b8802033b9da653c69b2290accedb9e5d01f49607e099"
BSP_BYTES = 1286875027
DOCKER_IMAGE = "ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517"
PACKAGES = (
    "python3 python3-cryptography python3-pycryptodome python3-yaml openssl "
    "device-tree-compiler libxml2-utils libusb-1.0-0 xxd cpio cpp binutils "
    "bzip2 zstd lz4 rsync bc dosfstools e2fsprogs uuid-runtime file"
)


def family_builder():
    path = Path(__file__).with_name("build-r39-family-inputs.py")
    spec = importlib.util.spec_from_file_location("helm_r39_family", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load family verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ensure_archive(path: Path, verifier) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(path.parent).free < BSP_BYTES + 1024**3:
            raise SystemExit("not enough free space to download the 1.29 GB NVIDIA BSP")
        temporary = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
        print(f"Downloading NVIDIA R39.2 BSP to {path}", flush=True)
        try:
            with urllib.request.urlopen(BSP_URL, timeout=60) as response:
                with temporary.open("xb") as output:
                    shutil.copyfileobj(response, output, 1024 * 1024)
            if temporary.stat().st_size != BSP_BYTES or verifier.sha256_file(temporary) != BSP_SHA256:
                raise SystemExit("downloaded NVIDIA BSP failed size/SHA-256 verification")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print("Verifying NVIDIA BSP archive", flush=True)
    if path.stat().st_size != BSP_BYTES or verifier.sha256_file(path) != BSP_SHA256:
        raise SystemExit(f"NVIDIA BSP size/SHA-256 mismatch: {path}")


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bsp-archive", type=Path,
        default=repo / "build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2",
        help="verified BSP archive; downloaded from NVIDIA if missing",
    )
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    verifier = family_builder()
    if not shutil.which("docker"):
        raise SystemExit("Docker is required for the Linux NVIDIA tools")
    subprocess.run(["docker", "info", "--format", "{{.OSType}}"], check=True, stdout=subprocess.DEVNULL)
    archive = args.bsp_archive.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists; choose a new directory: {output}")
    ensure_archive(archive, verifier)
    output.mkdir(parents=True)
    if shutil.disk_usage(output).free < 4 * 1024**3:
        raise SystemExit("at least 4 GiB of free working space is required for seed generation")

    container = "helm-r39-offline-seed-" + uuid.uuid4().hex[:12]
    provenance = {
        "bsp_url": BSP_URL,
        "bsp_sha256": BSP_SHA256,
        "bsp_bytes": BSP_BYTES,
        "docker_image": DOCKER_IMAGE,
        "docker_platform": "linux/amd64",
        "board": {"BOARDID": "3767", "BOARDSKU": "0001", "FAB": "300", "RAMCODE": "2", "CHIP_SKU": "00:00:00:D4"},
        "hardware_access": False,
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"Generating firmware offline; commands and output: {output / 'generation.log'}", flush=True)
    created = False
    with (output / "generation.log").open("wb") as log:
        def run(*argv: str) -> None:
            log.write((json.dumps(argv) + "\n").encode())
            log.flush()
            subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, check=True)

        try:
            run("docker", "run", "--platform", "linux/amd64", "--name", container, "--detach",
                "--mount", f"type=bind,source={archive},target=/source/bsp.tbz2,readonly",
                "--mount", f"type=bind,source={output},target=/output",
                DOCKER_IMAGE, "sleep", "infinity")
            created = True
            run("docker", "exec", container, "bash", "-lc",
                "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y " + PACKAGES)
            run("docker", "exec", container, "bash", "-lc",
                "mkdir /var/tmp/helm-r39 && tar -xjf /source/bsp.tbz2 --strip-components=1 -C /var/tmp/helm-r39")
            run("docker", "exec", "--env", "USER=root", container, "bash", "-lc",
                "cd /var/tmp/helm-r39 && env BOARDID=3767 BOARDSKU=0001 FAB=300 BOARDREV= "
                "RAMCODE=2 RAMCODE_ID=2 CHIP_SKU=00:00:00:D4 FUSELEVEL=fuselevel_production "
                "./flash.sh --no-flash --no-systemimg --sign jetson-orin-nano-devkit-qspi internal")
            run("docker", "exec", container, "bash", "-lc",
                "cp -a /var/tmp/helm-r39/bootloader/signed /output/generated-signed && "
                "dpkg-query -W > /output/installed-packages.txt")
        finally:
            if created:
                cleanup = subprocess.run(["docker", "rm", "--force", container], stdout=log, stderr=subprocess.STDOUT)
                if cleanup.returncode:
                    print(f"Could not remove generation container {container}; see generation.log", file=sys.stderr)

    generated = output / "generated-signed"
    verifier.verify_seed(generated)
    comparison = {
        name: {"expected": expected, "actual": verifier.sha256_file(generated / name)}
        for name, expected in verifier.SEED_HASHES.items()
    }
    (output / "firmware-hashes.json").write_text(json.dumps(comparison, indent=2) + "\n")
    provenance.update({
        "seed_flash_index_sha256": verifier.sha256_file(generated / "flash.idx"),
        "seed_flash_layout_sha256": verifier.SEED_FLASH_LAYOUT_SHA256,
        "verified_firmware_files": len(comparison),
    })
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    signed = output / "signed"
    signed.mkdir()
    for name in (*verifier.SEED_HASHES, "flash.idx"):
        shutil.copy2(generated / name, signed / name)
    shutil.rmtree(generated)
    verifier.verify_seed(signed)
    print(f"Verified {len(comparison)} firmware files and the fixed QSPI layout: {signed}")
    print(f"Use HELM_R39_SEED_SIGNED_DIR={signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
