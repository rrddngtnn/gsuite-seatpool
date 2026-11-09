"""Local generator capacity is not a Google licensing or quota assertion."""
from __future__ import annotations

import pytest

from directory_seats import generator


@pytest.mark.parametrize("count", [75, 100])
def test_explicit_count_above_local_default(count):
    seats = generator.generate_domain_seats("alpha.example.test", count=count)
    assert len(seats) == count
    assert len({seat["email"] for seat in seats}) == count
    assert [seat["seat_index"] for seat in seats] == list(range(1, count + 1))
    assert seats[-1]["email"] == f"unit{count}@alpha.example.test"
    assert generator.redact_seat(seats[0]) == generator.redact_seat(
        generator.make_seat("alpha.example.test", 1)
    )


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "2", None])
@pytest.mark.parametrize("argument", ["count", "index"])
def test_count_and_index_require_positive_integers_before_secrets(monkeypatch, value, argument):
    def unexpected_password(*args):
        pytest.fail("Invalid input must not generate passwords")

    monkeypatch.setattr(generator, "seat_password", unexpected_password)
    operation = generator.generate_domain_seats if argument == "count" else generator.make_seat
    with pytest.raises(ValueError, match="positive integer") as exc:
        operation("alpha.example.test", **{argument: value})
    assert not any(word in str(exc.value).lower() for word in ("google", "quota", "ceiling", "cloud identity"))


@pytest.mark.parametrize("operation", [generator.make_seat, generator.generate_domain_seats])
@pytest.mark.parametrize("fqdn", [None, True, "", "localhost", "a..test", "-a.test", "a-.test", "a_b.test", "user@a.test", "a.test/path", "a. test", "a" * 64 + ".test", ".".join(["a" * 63] * 4)])
def test_invalid_domain_is_rejected_before_passwords(monkeypatch, operation, fqdn):
    monkeypatch.setattr(generator, "seat_password", lambda *args: pytest.fail("Password generated"))
    with pytest.raises(ValueError, match="domain"):
        operation(fqdn, 1)


@pytest.mark.parametrize("operation", [generator.make_seat, generator.generate_domain_seats])
@pytest.mark.parametrize("prefix", [None, True, "", "bad prefix", "bad@", "bad/", "bad\\n", ".bad", "bad..prefix", "x" * 64])
def test_invalid_prefix_is_rejected_before_passwords(monkeypatch, operation, prefix):
    monkeypatch.setattr(generator, "seat_password", lambda *args: pytest.fail("Password generated"))
    with pytest.raises(ValueError, match="prefix"):
        operation("alpha.example.test", 1, prefix=prefix)


def test_entire_generated_handle_range_is_validated_before_passwords(monkeypatch):
    monkeypatch.setattr(generator, "seat_password", lambda *args: pytest.fail("Password generated"))
    with pytest.raises(ValueError, match="prefix"):
        generator.generate_domain_seats("alpha.example.test", 100, prefix="x" * 62)


def test_normalized_domain_retains_identity_and_custom_prefix():
    seat = generator.make_seat(" Alpha.Example.Test ", 100, prefix="team.ops-_")
    assert seat["fqdn"] == "alpha.example.test"
    assert seat["email"] == "team.ops-_100@alpha.example.test"
    assert generator.redact_seat(seat) == generator.redact_seat(
        generator.make_seat("alpha.example.test", 100, prefix="team.ops-_")
    )


def test_default_is_local_and_legacy_constant_is_an_alias():
    assert generator.DEFAULT_SEAT_COUNT == 50
    assert generator.CLOUD_IDENTITY_FREE_CAP == generator.DEFAULT_SEAT_COUNT
    assert len(generator.generate_domain_seats("alpha.example.test")) == 50
