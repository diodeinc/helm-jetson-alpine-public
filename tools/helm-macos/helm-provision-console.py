#!/usr/bin/env python3
"""Drive Helm's RAM-recovery CDC console using only the Python standard library."""

from __future__ import annotations

import argparse
import ast
import errno
import glob
import io
import math
import os
import pty
import re
import secrets
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


DEVICE_RE = re.compile(r"/dev/nvme[0-9]n[0-9]\Z")
PROFILE_RE = re.compile(r"helm-orin-(?:nx-(?:8gb|16gb)|nano-(?:4gb|8gb|8gb-sd))-r39\.2\Z")
TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
IOREG_NODE_RE = re.compile(r"^(?P<prefix>.*?)\+-o .* <class (?P<class>[^,>]+)")
IOREG_PROPERTY_RE = re.compile(r'^[ |]*"(?P<key>[^"]+)" = (?P<value>.*)$')
RECOVERY_VENDOR_ID = 0x0955
RECOVERY_PRODUCT_ID = 0x7020
RECOVERY_PRODUCT = "Helm Alpine Recovery Console"
RECOVERY_SERIAL = "helm-recovery"
SUCCESS_RE = re.compile(
    r"HELM_PROVISION_SUCCESS session=([0-9a-f]{32}) "
    r"profile=([^ ]+) device=([^ ]+)\Z"
)
FAILURE_RE = re.compile(
    r"HELM_PROVISION_FAILURE session=([0-9a-f]{32}) rc=([0-9]+)\Z"
)


class ProvisionError(RuntimeError):
    """A safe, user-facing provisioning failure."""


@dataclass(frozen=True)
class Marker:
    kind: str
    session: str
    profile: str | None = None
    device: str | None = None
    returncode: int | None = None


@dataclass(frozen=True)
class UsbSerialIdentity:
    callout: str
    vendor_id: int | None
    product_id: int | None
    product: str | None
    serial: str | None

    def is_helm_recovery(self) -> bool:
        return (
            self.vendor_id == RECOVERY_VENDOR_ID
            and self.product_id == RECOVERY_PRODUCT_ID
            and self.product == RECOVERY_PRODUCT
            and self.serial == RECOVERY_SERIAL
        )

    def summary(self) -> str:
        vendor = "????" if self.vendor_id is None else f"{self.vendor_id:04x}"
        product_id = "????" if self.product_id is None else f"{self.product_id:04x}"
        return (
            f"{vendor}:{product_id} product={self.product!r} serial={self.serial!r}"
        )


def parse_marker(line: str) -> Marker | None:
    """Parse only complete marker lines; echoed shell commands never qualify."""
    normalized = line.rstrip("\r\n")
    match = SUCCESS_RE.fullmatch(normalized)
    if match:
        return Marker("success", match.group(1), match.group(2), match.group(3))
    match = FAILURE_RE.fullmatch(normalized)
    if match:
        return Marker("failure", match.group(1), returncode=int(match.group(2)))
    return None


def parse_ioreg_value(raw: str) -> object:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return raw[1:-1]
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return raw


def parse_ioreg_serial_identities(output: str) -> dict[str, UsbSerialIdentity]:
    """Map BSD callout nodes to their nearest IOUSBHostDevice parent."""
    stack: list[tuple[int, str, dict[str, object]]] = []
    result: dict[str, UsbSerialIdentity] = {}
    for line in output.splitlines():
        node_match = IOREG_NODE_RE.match(line)
        if node_match:
            column = line.index("+-o")
            while stack and stack[-1][0] >= column:
                stack.pop()
            stack.append((column, node_match.group("class"), {}))
            continue
        property_match = IOREG_PROPERTY_RE.match(line)
        if property_match is None or not stack:
            continue
        key = property_match.group("key")
        value = parse_ioreg_value(property_match.group("value"))
        stack[-1][2][key] = value
        if key != "IOCalloutDevice" or not isinstance(value, str):
            continue
        usb_parent: dict[str, object] | None = None
        for _column, object_class, properties in reversed(stack[:-1]):
            if object_class == "IOUSBHostDevice":
                usb_parent = properties
                break
        if usb_parent is None:
            continue
        result[value] = UsbSerialIdentity(
            callout=value,
            vendor_id=usb_parent.get("idVendor")
            if isinstance(usb_parent.get("idVendor"), int)
            else None,
            product_id=usb_parent.get("idProduct")
            if isinstance(usb_parent.get("idProduct"), int)
            else None,
            product=usb_parent.get("USB Product Name")
            if isinstance(usb_parent.get("USB Product Name"), str)
            else None,
            serial=usb_parent.get("USB Serial Number")
            if isinstance(usb_parent.get("USB Serial Number"), str)
            else None,
        )
    return result


def read_macos_serial_identities() -> dict[str, UsbSerialIdentity]:
    command = [
        "/usr/sbin/ioreg",
        "-r",
        "-c",
        "IOSerialBSDClient",
        "-t",
        "-l",
        "-w0",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError(f"cannot inspect macOS USB serial identity: {exc}") from exc
    return parse_ioreg_serial_identities(completed.stdout)


def modem_paths(
    excluded: Iterable[str],
    finder: Callable[[str], Sequence[str]] = glob.glob,
) -> list[str]:
    excluded_real = {os.path.realpath(path) for path in excluded}
    candidates: list[str] = []
    for path in sorted(finder("/dev/cu.usbmodem*")):
        if os.path.realpath(path) not in excluded_real:
            candidates.append(path)
    return candidates


def classify_modems(
    paths: Sequence[str],
    identities: dict[str, UsbSerialIdentity],
) -> tuple[list[str], dict[str, UsbSerialIdentity], list[str]]:
    qualified: list[str] = []
    rejected: dict[str, UsbSerialIdentity] = {}
    unresolved: list[str] = []
    for path in paths:
        identity = identities.get(path)
        if identity is None:
            unresolved.append(path)
        elif identity.is_helm_recovery():
            qualified.append(path)
        else:
            rejected[path] = identity
    return qualified, rejected, unresolved


def new_modems(
    excluded: Iterable[str],
    finder: Callable[[str], Sequence[str]] = glob.glob,
    identity_reader: Callable[[], dict[str, UsbSerialIdentity]] = read_macos_serial_identities,
) -> list[str]:
    paths = modem_paths(excluded, finder)
    if not paths:
        return []
    qualified, _rejected, _unresolved = classify_modems(paths, identity_reader())
    return qualified


def wait_for_modem(
    excluded: Iterable[str],
    timeout: float,
    finder: Callable[[str], Sequence[str]] = glob.glob,
    identity_reader: Callable[[], dict[str, UsbSerialIdentity]] = read_macos_serial_identities,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    deadline = monotonic() + timeout
    last_candidates: list[str] = []
    last_paths: list[str] | None = None
    rejected: dict[str, UsbSerialIdentity] = {}
    unresolved: list[str] = []
    refresh_at = 0.0
    stable_since: float | None = None
    while monotonic() < deadline:
        now = monotonic()
        paths = modem_paths(excluded, finder)
        if paths != last_paths or (unresolved and now >= refresh_at):
            identities = identity_reader() if paths else {}
            candidates, rejected, unresolved = classify_modems(paths, identities)
            last_paths = paths
            refresh_at = now + 0.5
        if len(candidates) > 1:
            raise ProvisionError(
                "multiple exact Helm recovery CDC devices appeared: "
                + ", ".join(candidates)
            )
        if candidates != last_candidates:
            last_candidates = candidates
            stable_since = monotonic() if len(candidates) == 1 else None
        elif len(candidates) == 1 and stable_since is not None:
            if monotonic() - stable_since >= 0.5:
                return candidates[0]
        sleeper(0.1)
    detail = ""
    if rejected:
        observations = ", ".join(
            f"{path} [{identity.summary()}]"
            for path, identity in sorted(rejected.items())
        )
        detail += f"; rejected non-recovery serial devices: {observations}"
    if unresolved:
        detail += "; USB parent identity unavailable for: " + ", ".join(unresolved)
    raise ProvisionError(
        "timed out waiting for exact Helm recovery CDC identity "
        "0955:7020/product/serial" + detail
    )


def configure_serial(fd: int) -> None:
    tty.setraw(fd, termios.TCSANOW)
    attrs = termios.tcgetattr(fd)
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[2] |= termios.CLOCAL | termios.CREAD
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def write_all(fd: int, payload: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(payload):
        if time.monotonic() >= deadline:
            raise ProvisionError("timed out writing to the recovery console")
        try:
            written = os.write(fd, payload[offset:])
        except BlockingIOError:
            select.select([], [fd], [], 0.2)
            continue
        except OSError as exc:
            raise ProvisionError(f"recovery console write failed: {exc}") from exc
        if written <= 0:
            raise ProvisionError("recovery console closed while writing")
        offset += written


def command_for(profile: str, device: str, token: str) -> bytes:
    if not PROFILE_RE.fullmatch(profile):
        raise ProvisionError(f"invalid profile passed to serial helper: {profile}")
    if not DEVICE_RE.fullmatch(device):
        raise ProvisionError(f"invalid NVMe device passed to serial helper: {device}")
    if not TOKEN_RE.fullmatch(token):
        raise ProvisionError("invalid provisioning session token")
    return (
        "helm-provision install"
        f" --device {device}"
        f" --confirm-device {device}"
        f" --confirm-profile {profile}"
        f" --session {token}\n"
    ).encode("ascii")


def stream_until(
    fd: int,
    deadline: float,
    on_line: Callable[[str], bool],
    output: object = sys.stdout.buffer,
    line_buffer: bytearray | None = None,
) -> None:
    if line_buffer is None:
        line_buffer = bytearray()
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            readable, _, _ = select.select([fd], [], [], min(0.25, remaining))
        except OSError as exc:
            raise ProvisionError(f"recovery console polling failed: {exc}") from exc
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENXIO):
                raise ProvisionError("recovery console disconnected") from exc
            raise ProvisionError(f"recovery console read failed: {exc}") from exc
        if not chunk:
            raise ProvisionError("recovery console closed")
        output.write(chunk)  # type: ignore[attr-defined]
        output.flush()  # type: ignore[attr-defined]
        line_buffer.extend(chunk)
        while b"\n" in line_buffer:
            raw_line, _, remainder = line_buffer.partition(b"\n")
            line_buffer = bytearray(remainder)
            line = raw_line.decode("utf-8", "replace").rstrip("\r")
            if on_line(line):
                return
    raise ProvisionError("timed out waiting for the target recovery console")


def run_console(args: argparse.Namespace) -> int:
    token = secrets.token_hex(16)
    modem = wait_for_modem(args.exclude, args.connect_timeout)
    print(f"helm-macos: recovery console: {modem}", flush=True)
    try:
        fd = os.open(modem, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        raise ProvisionError(f"cannot open recovery console {modem}: {exc}") from exc

    try:
        try:
            configure_serial(fd)
        except (OSError, termios.error) as exc:
            raise ProvisionError(f"cannot configure recovery console {modem}: {exc}") from exc
        ready_line = f"HELM_PROVISION_READY session={token}"
        ready_command = f"printf '\\n{ready_line}\\n'\n".encode("ascii")
        ready_deadline = time.monotonic() + args.ready_timeout
        # The device node can precede the recovery shell by a fraction of a
        # second. Repeating an inert token probe is safe and avoids a race.
        next_probe = 0.0
        ready_buffer = bytearray()

        def ready(line: str) -> bool:
            return line == ready_line

        while True:
            now = time.monotonic()
            if now >= ready_deadline:
                raise ProvisionError("recovery CDC appeared, but its shell did not become ready")
            if now >= next_probe:
                write_all(fd, ready_command, ready_deadline)
                next_probe = now + 2.0
            probe_deadline = min(ready_deadline, next_probe)
            try:
                stream_until(fd, probe_deadline, ready, line_buffer=ready_buffer)
                break
            except ProvisionError as exc:
                if "timed out" not in str(exc):
                    raise

        print(
            "helm-macos: recovery console ready; starting guarded target preflight",
            flush=True,
        )
        operation_deadline = time.monotonic() + args.operation_timeout
        write_all(fd, command_for(args.profile, args.device, token), operation_deadline)
        result: Marker | None = None

        def completed(line: str) -> bool:
            nonlocal result
            marker = parse_marker(line)
            if marker is None or marker.session != token:
                return False
            result = marker
            return True

        try:
            stream_until(fd, operation_deadline, completed)
        except ProvisionError as exc:
            raise ProvisionError(
                f"{exc}; target state is unknown, so leave Helm powered and inspect UART"
            ) from exc
        assert result is not None
        if result.kind == "failure":
            raise ProvisionError(
                f"target provisioning failed (target rc={result.returncode})"
            )
        if result.profile != args.profile or result.device != args.device:
            raise ProvisionError("target success marker identity does not match this request")
        print(
            f"helm-macos: provisioning verified successful for {args.profile} on {args.device}",
            flush=True,
        )
        return 0
    finally:
        os.close(fd)


def self_test() -> None:
    token = "0123456789abcdef0123456789abcdef"
    success = parse_marker(
        "HELM_PROVISION_SUCCESS session="
        f"{token} profile=helm-orin-nx-8gb-r39.2 device=/dev/nvme0n1\r"
    )
    assert success == Marker(
        "success", token, "helm-orin-nx-8gb-r39.2", "/dev/nvme0n1"
    )
    assert parse_marker(f"HELM_PROVISION_FAILURE session={token} rc=23") == Marker(
        "failure", token, returncode=23
    )
    assert parse_marker(
        f"printf 'HELM_PROVISION_SUCCESS session={token} profile=x device=y'"
    ) is None
    assert parse_marker(f"HELM_PROVISION_SUCCESS session={token} profile=x device=y extra") is None
    assert command_for("helm-orin-nano-8gb-sd-r39.2", "/dev/nvme9n8", token).endswith(
        f"--session {token}\n".encode()
    )
    for profile in ("bad", "helm-orin-nx-8gb-r39.1"):
        try:
            command_for(profile, "/dev/nvme0n1", token)
        except ProvisionError:
            pass
        else:
            raise AssertionError("invalid profile was accepted")
    for device in ("/dev/sda", "/dev/nvme0n1p1", "/dev/nvme00n1"):
        try:
            command_for("helm-orin-nx-8gb-r39.2", device, token)
        except ProvisionError:
            pass
        else:
            raise AssertionError("invalid device was accepted")

    ioreg_fixture = r'''
+-o Root  <class IORegistryEntry, id 0x1>
  +-o MCP2221  <class IOUSBHostDevice, id 0x2>
    | "idVendor" = 1240
    | "idProduct" = 221
    | "USB Product Name" = "MCP2221 USB_I2C_UART Combo"
    +-o IOUSBHostInterface  <class IOUSBHostInterface, id 0x3>
      +-o IOSerialBSDClient  <class IOSerialBSDClient, id 0x4>
          "IOCalloutDevice" = "/dev/cu.usbmodem-uart"
+-o Root  <class IORegistryEntry, id 0x5>
  +-o Helm Alpine Recovery Console  <class IOUSBHostDevice, id 0x6>
    | "idVendor" = 2389
    | "idProduct" = 28704
    | "USB Product Name" = "Helm Alpine Recovery Console"
    | "USB Serial Number" = "helm-recovery"
    +-o CDC ACM Data  <class IOUSBHostInterface, id 0x7>
      | "idVendor" = 1
      | "idProduct" = 2
      +-o IOSerialBSDClient  <class IOSerialBSDClient, id 0x8>
          "IOCalloutDevice" = "/dev/cu.usbmodem-recovery"
'''
    identities = parse_ioreg_serial_identities(ioreg_fixture)
    assert identities["/dev/cu.usbmodem-uart"] == UsbSerialIdentity(
        "/dev/cu.usbmodem-uart",
        0x04D8,
        0x00DD,
        "MCP2221 USB_I2C_UART Combo",
        None,
    )
    assert identities["/dev/cu.usbmodem-recovery"].is_helm_recovery()
    fake_paths = ["/dev/cu.usbmodem-uart", "/dev/cu.usbmodem-recovery"]
    finder = lambda _pattern: fake_paths  # noqa: E731
    identity_reader = lambda: identities  # noqa: E731
    assert new_modems([], finder, identity_reader) == [
        "/dev/cu.usbmodem-recovery"
    ]
    wrong_serial = UsbSerialIdentity(
        "/dev/cu.usbmodem-wrong",
        RECOVERY_VENDOR_ID,
        RECOVERY_PRODUCT_ID,
        RECOVERY_PRODUCT,
        "not-helm-recovery",
    )
    assert classify_modems([wrong_serial.callout], {wrong_serial.callout: wrong_serial}) == (
        [],
        {wrong_serial.callout: wrong_serial},
        [],
    )
    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    def sleep(duration: float) -> None:
        clock[0] += duration

    assert wait_for_modem(
        ["/dev/cu.usbmodem-uart"],
        2.0,
        finder=finder,
        identity_reader=identity_reader,
        monotonic=monotonic,
        sleeper=sleep,
    ) == "/dev/cu.usbmodem-recovery"

    second_recovery = UsbSerialIdentity(
        "/dev/cu.usbmodem-recovery-2",
        RECOVERY_VENDOR_ID,
        RECOVERY_PRODUCT_ID,
        RECOVERY_PRODUCT,
        RECOVERY_SERIAL,
    )
    ambiguous_paths = ["/dev/cu.usbmodem-recovery", second_recovery.callout]
    ambiguous_identities = dict(identities)
    ambiguous_identities[second_recovery.callout] = second_recovery
    try:
        wait_for_modem(
            [],
            0.1,
            finder=lambda _pattern: ambiguous_paths,
            identity_reader=lambda: ambiguous_identities,
        )
    except ProvisionError as exc:
        assert "multiple exact Helm recovery" in str(exc)
    else:
        raise AssertionError("ambiguous modem selection was accepted")

    mcp_clock = [0.0]
    try:
        wait_for_modem(
            [],
            0.3,
            finder=lambda _pattern: ["/dev/cu.usbmodem-uart"],
            identity_reader=identity_reader,
            monotonic=lambda: mcp_clock[0],
            sleeper=lambda duration: mcp_clock.__setitem__(0, mcp_clock[0] + duration),
        )
    except ProvisionError as exc:
        assert "04d8:00dd" in str(exc)
        assert "rejected non-recovery" in str(exc)
    else:
        raise AssertionError("MCP2221 UART was accepted as Helm recovery")

    master_fd, slave_fd = pty.openpty()
    try:
        configure_serial(slave_fd)
    finally:
        os.close(master_fd)
        os.close(slave_fd)

    read_fd, write_fd = os.pipe()
    captured = io.BytesIO()
    observed: list[Marker] = []

    def write_fixture() -> None:
        fixture = (
            b"ordinary target output\r\nHELM_PROVISION_SUCCESS session="
            + token.encode()
            + b" profile=helm-orin-nx-8gb-r39.2 device=/dev/nvme0n1\r\n"
        )
        os.write(write_fd, fixture[:37])
        os.write(write_fd, fixture[37:])
        os.close(write_fd)

    writer = threading.Thread(target=write_fixture)
    writer.start()

    def capture_marker(line: str) -> bool:
        marker = parse_marker(line)
        if marker is None:
            return False
        observed.append(marker)
        return True

    try:
        stream_until(read_fd, time.monotonic() + 2.0, capture_marker, captured)
    finally:
        os.close(read_fd)
        writer.join()
    assert observed == [success]
    assert captured.getvalue().startswith(b"ordinary target output")
    try:
        main(
            [
                "--profile",
                "helm-orin-nx-8gb-r39.2",
                "--device",
                "/dev/nvme0n1",
                "--operation-timeout",
                "nan",
            ]
        )
    except ProvisionError as exc:
        assert "finite and positive" in str(exc)
    else:
        raise AssertionError("non-finite timeout was accepted")
    print("helm-provision-console: self-test passed")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="wait for Helm recovery CDC and run guarded provisioning"
    )
    result.add_argument("--profile", required=True)
    result.add_argument("--device", required=True)
    result.add_argument("--exclude", action="append", default=[])
    result.add_argument("--connect-timeout", type=float, default=180.0)
    result.add_argument("--ready-timeout", type=float, default=30.0)
    result.add_argument("--operation-timeout", type=float, default=1800.0)
    result.add_argument("--self-test", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["--self-test"]:
        self_test()
        return 0
    args = parser().parse_args(argv)
    for name in ("connect_timeout", "ready_timeout", "operation_timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ProvisionError(f"--{name.replace('_', '-')} must be finite and positive")
    return run_console(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProvisionError, KeyboardInterrupt) as exc:
        message = str(exc) if str(exc) else "interrupted"
        if isinstance(exc, KeyboardInterrupt):
            message += "; if target installation began, leave Helm powered and inspect UART"
        print(f"helm-macos: {message}", file=sys.stderr)
        raise SystemExit(1)
