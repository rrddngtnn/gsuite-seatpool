"""Deterministic seat generation and Admin Console bulk CSV export under the 50-user Cloud Identity cap."""
from __future__ import annotations

import csv
import hashlib
import os
import random

CLOUD_IDENTITY_FREE_CAP = 50

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

WORDS = ["Apex", "Core", "Edge", "Flux", "Grid", "Link", "Node", "Span"]
SYMBOLS = "!@#$%&*"


def _rng(seed: str) -> random.Random:
    return random.Random(hashlib.sha256(seed.encode()).digest())


def seat_password(fqdn: str, index: int) -> str:
    rng = _rng(f"seat-pw:{fqdn}:{index}")
    return f"{rng.choice(WORDS)}{rng.randint(1000, 9999)}{rng.choice(SYMBOLS)}{rng.choice(WORDS).lower()}"


def make_seat(fqdn: str, index: int, prefix: str = "unit", org_unit: str = "/") -> dict:
    if index < 1 or index > CLOUD_IDENTITY_FREE_CAP:
        raise ValueError(
            f"Seat index {index} outside Cloud Identity Free limit (1..{CLOUD_IDENTITY_FREE_CAP})"
        )
    fqdn = fqdn.strip().lower()
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
    count: int = CLOUD_IDENTITY_FREE_CAP,
    prefix: str = "unit",
    org_unit: str = "/",
) -> list[dict]:
    if count < 1 or count > CLOUD_IDENTITY_FREE_CAP:
        raise ValueError(
            f"Requested {count} seats; Cloud Identity Free ceiling is {CLOUD_IDENTITY_FREE_CAP} per domain"
        )
    return [make_seat(fqdn, idx, prefix=prefix, org_unit=org_unit) for idx in range(1, count + 1)]


def redact_seat(seat: dict) -> dict:
    return {k: v for k, v in seat.items() if k != "password"}


def export_admin_csv(seats: list[dict], output_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
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
