#!/usr/bin/env python3
"""Reject private machine details and sensitive files in tracked current source.

This complements Gitleaks; it does not inspect history or prove that a release
is safe. Diagnostics include the file, line and category, never matched content.
Use symbolic placeholders such as $HOME or <user> in public documentation.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


TAILNET_HOST = re.compile(rb"\b(?:[a-z0-9-]+\.)+ts\.net\b", re.IGNORECASE)
SHARED_IPV4 = re.compile(rb"(?<![\d.])100(?:\.\d{1,3}){3}(?![\d.])")
LOCAL_HOME = re.compile(
    rb"(?:/(?:Users|home)/|[A-Za-z]:\\Users\\)([A-Za-z0-9_.-]+)(?=[/\\\s\"'`]|$)"
)
HOME_PLACEHOLDERS = {
    b"user", b"username", b"your_user", b"your_username", b"example",
    b"shared", b"runner",
}
PRIVATE_EXTENSIONS = {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
PRIVATE_FILENAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
}
PRIVATE_DIRECTORIES = {".ssh", ".aws", ".gnupg"}
ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}


def sensitive_path(name: str) -> bool:
    path = PurePosixPath(name)
    basename = path.name.lower()
    return (
        path.suffix.lower() in PRIVATE_EXTENSIONS
        or basename in PRIVATE_FILENAMES
        or any(part.lower() in PRIVATE_DIRECTORIES for part in path.parts)
        or (
            (basename == ".env" or basename.startswith(".env."))
            and basename not in ENV_TEMPLATES
        )
    )


def is_shared_ipv4(value: bytes) -> bool:
    try:
        address = ipaddress.IPv4Address(value.decode("ascii"))
    except ipaddress.AddressValueError:
        return False
    return address.packed[0] == 100 and 64 <= address.packed[1] <= 127


def content_findings(data: bytes) -> list[tuple[int, str]]:
    if b"\0" in data:
        return []
    findings = []
    for number, line in enumerate(data.splitlines(), 1):
        if TAILNET_HOST.search(line):
            findings.append((number, "private tailnet hostname"))
        if any(
            is_shared_ipv4(match.group())
            for match in SHARED_IPV4.finditer(line)
        ):
            findings.append((number, "shared-address-space host (possible tailnet IP)"))
        if any(
            match.group(1).lower() not in HOME_PLACEHOLDERS
            for match in LOCAL_HOME.finditer(line)
        ):
            findings.append((number, "personal home directory"))
    return findings


def scan(root: Path) -> list[tuple[str, int, str]]:
    tracked = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--cached", "--full-name", "-z"]
    ).split(b"\0")
    findings = []
    for raw_name in sorted(set(tracked) - {b""}):
        name = os.fsdecode(raw_name)
        path = root / name
        if sensitive_path(name):
            findings.append((name, 1, "sensitive credential or key file"))
        # Inspect a link target as text, without following it outside the repo.
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
        elif not path.exists():
            continue  # An unstaged deletion is absent from current source.
        else:
            findings.append((name, 1, "tracked directory needs separate review"))
            continue
        findings.extend((name, line, reason) for line, reason in content_findings(data))
    return findings


def self_test() -> None:
    # Assemble synthetic positive cases so these tests do not embed private
    # identifiers in the source they are testing.
    fake_host = b"fixture-host." + b"tail000000." + b"ts.net"
    fake_home = b"/Users/" + b"fixture-person/project"
    assert content_findings(b"first line\nhttps://" + fake_host) == [
        (2, "private tailnet hostname")
    ]
    assert content_findings(fake_home) == [(1, "personal home directory")]
    assert content_findings(b"http://100." + b"64.0.1/") == [
        (1, "shared-address-space host (possible tailnet IP)")
    ]
    assert content_findings(b"http://100." + b"127.255.255/")
    for suffix in (b"63.255.255", b"128.0.1", b"64.0.256", b"064.0.1"):
        assert not content_findings(b"http://100." + suffix)
    assert not content_findings(b"http://192.0.2.1")
    assert content_findings(b"/home/" + b"fixture-person/project")
    assert content_findings(b"C:\\Users\\" + b"fixture-person\\project")
    assert not content_findings(
        b"$HOME/project ~/project /Users/<user>/project /home/user/project\n"
        b"https://<device>.<tailnet>.ts.net https://example.com"
    )
    assert not content_findings(b"\0" + fake_host)
    for name in (".env", "dir/.env.prod", "device.key", "backup.p12", ".ssh/config"):
        assert sensitive_path(name), name
    for name in (".env.example", "docs/keys.md", "device.pub", "src/config.py"):
        assert not sensitive_path(name), name

    with tempfile.TemporaryDirectory(prefix="helm-source-check-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "tracked.md").write_bytes(b"public documentation\n")
        (root / "untracked.md").write_bytes(fake_host)
        (root / ".env").write_text("fixture=value\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "tracked.md"], check=True)
        assert scan(root) == []  # Only tracked paths are publication inputs.
        (root / "tracked.md").write_bytes(b"changed worktree\n" + fake_host)
        assert scan(root) == [("tracked.md", 2, "private tailnet hostname")]
        (root / "tracked.md").unlink()
        (root / "tracked.md").symlink_to(os.fsdecode(fake_home))
        assert scan(root) == [("tracked.md", 1, "personal home directory")]
        (root / "tracked.md").unlink()
        subprocess.run(["git", "-C", str(root), "add", ".env"], check=True)
        assert scan(root) == [(".env", 1, "sensitive credential or key file")]
    print("Public source check self-tests passed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    root = Path(__file__).resolve().parents[1]
    findings = scan(root)
    for name, line, reason in findings:
        print(f"{name}:{line}: {reason} (content redacted)")
    if findings:
        print(f"Public source check failed: {len(findings)} finding(s).")
        return 1
    print("Public source check passed for tracked current files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
