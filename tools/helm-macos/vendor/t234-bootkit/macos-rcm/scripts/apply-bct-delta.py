#!/usr/bin/env python3
"""Apply a byte-exact fixed-layout BCT update learned from oracle fixtures.

The baseline and updated reference files define the patch mask. The input must
have the same structure and every patched byte must still contain either the
baseline value or the already-updated value. This deliberately fails closed if
a new BSP changes a field that the retained layout update also owns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NoReturn


def fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="raw reference before update")
    parser.add_argument("updated", type=Path, help="same reference after update")
    parser.add_argument("input", type=Path, help="raw BCT to patch")
    parser.add_argument("output", type=Path, help="patched BCT")
    parser.add_argument(
        "--expected-prefix",
        help="optional ASCII prefix required in all three BCT inputs",
    )
    args = parser.parse_args()

    baseline = args.baseline.read_bytes()
    updated = args.updated.read_bytes()
    source = args.input.read_bytes()
    if not baseline or len(baseline) != len(updated) or not source:
        fail("baseline and updated reference must have the same nonzero size")
    if args.expected_prefix is not None:
        try:
            prefix = args.expected_prefix.encode("ascii")
        except UnicodeEncodeError:
            fail("--expected-prefix must be ASCII")
        for label, payload in (
            ("baseline", baseline),
            ("updated reference", updated),
            ("input", source),
        ):
            if not payload.startswith(prefix):
                fail(f"{label} does not begin with {args.expected_prefix!r}")

    changed = [
        offset
        for offset, (before, after) in enumerate(zip(baseline, updated))
        if before != after
    ]
    if not changed:
        fail("reference update changes no bytes")
    if len(source) <= changed[-1]:
        fail(
            f"input is too small for the last reference patch byte {changed[-1]:#x}"
        )

    conflicts = [
        offset
        for offset in changed
        if source[offset] not in (baseline[offset], updated[offset])
    ]
    if conflicts:
        preview = ", ".join(f"{offset:#x}" for offset in conflicts[:8])
        suffix = "..." if len(conflicts) > 8 else ""
        fail(
            f"input conflicts with the reference patch at {len(conflicts)} byte(s): "
            f"{preview}{suffix}"
        )

    result = bytearray(source)
    for offset in changed:
        result[offset] = updated[offset]
    args.output.write_bytes(result)
    print(
        f"wrote {args.output} ({len(result)} bytes, "
        f"applied {len(changed)} fixed-layout byte updates)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
