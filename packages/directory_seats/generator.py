"""Stable seat identities, random secrets, and Admin Console bulk CSV export."""
from __future__ import annotations

import csv
import hashlib
import os
import random
import re
import secrets
import string

DEFAULT_SEAT_COUNT = 50
# Backward-compatible name for the local default, not a Google quota.
CLOUD_IDENTITY_FREE_CAP = DEFAULT_SEAT_COUNT

ADMIN_CSV_HEADERS = [
    "First Name [Required]",
    "Last Name [Required]",
    "Email Address [Required]",
    "Password [Required]",
    "Org Unit Path [Required]",
    "Change Password at Next Sign-In",
]

FIRST_NAMES = [
    "Alex", "Blake", "Casey", "Dana", "Ellis", "Finley", "Gray", "Harper",
    "Indigo", "Jordan", "Kerry", "Logan", "Morgan", "Noel", "Owen", "Parker",
    "Quinn", "Reese", "Sage", "Taylor", "Urban", "Val", "Winter", "Xen",
]

LAST_NAMES = [
    "Abbott", "Barker", "Carson", "Dalton", "Ellis", "Foster", "Garner",
    "Hayes", "Ingram", "Jensen", "Keller", "Lawson", "Mercer", "Norris",
    "Palmer", "Sutton",
]

SYMBOLS = "!@#$%&*"


def _rng(seed: str) -> random.Random:
    return random.Random(hashlib.sha256(seed.encode()).digest())


def seat_password(fqdn: str, index: int) -> str:
    """Return a fresh secret; identity arguments remain for API compatibility.

    Persist the result: calling again is not a password recovery mechanism.
    """
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in SYMBOLS for c in password)
        ):
            return password


def _validated_domain(fqdn: str) -> str:
    if not isinstance(fqdn, str):
        raise ValueError("Seat domain must be a valid fully qualified domain name")
    fqdn = fqdn.strip().lower()
    labels = fqdn.split(".")
    if len(fqdn) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("Seat domain must be a valid fully qualified domain name")
    return fqdn


def _validate_prefix(prefix: str, last_index: int) -> None:
    if (
        not isinstance(prefix, str)
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", prefix)
        or ".." in prefix
        or len(prefix) + len(f"{last_index:02d}") > 64
    ):
        raise ValueError("Seat prefix must produce a valid email local part of at most 64 characters")


def make_seat(fqdn: str, index: int, prefix: str = "unit", org_unit: str = "/") -> dict:
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        raise ValueError("Seat index must be a positive integer")
    fqdn = _validated_domain(fqdn)
    _validate_prefix(prefix, index)
    rng = _rng(f"seat:{fqdn}:{index}")
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    handle = f"{prefix}{index:02d}"
    return {
        "fqdn": fqdn,
        "seat_index": index,
        "email": f"{handle}@{fqdn}",
        "password": seat_password(fqdn, index),
        "first_name": first,
        "last_name": last,
        "org_unit": org_unit,
        "change_password_at_next_login": False,
    }


def generate_domain_seats(
    fqdn: str,
    count: int = DEFAULT_SEAT_COUNT,
    prefix: str = "unit",
    org_unit: str = "/",
) -> list[dict]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Seat count must be a positive integer")
    fqdn = _validated_domain(fqdn)
    _validate_prefix(prefix, count)
    return [make_seat(fqdn, idx, prefix=prefix, org_unit=org_unit) for idx in range(1, count + 1)]


def redact_seat(seat: dict) -> dict:
    return {k: v for k, v in seat.items() if k != "password"}


def export_admin_csv(seats: list[dict], output_path: str) -> str:
    """Create a private credential export, refusing any existing destination.

    Exclusive creation rejects both regular files and symlinks atomically.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(
        output_path,
        "x",
        newline="",
        encoding="utf-8",
        opener=lambda path, flags: os.open(path, flags, 0o600),
    ) as f:
        writer = csv.writer(f)
        writer.writerow(ADMIN_CSV_HEADERS)
        for s in seats:
            writer.writerow([
                s["first_name"],
                s["last_name"],
                s["email"],
                s["password"],
                s.get("org_unit", "/"),
                "True" if s.get("change_password_at_next_login") else "False",
            ])
    return output_path
