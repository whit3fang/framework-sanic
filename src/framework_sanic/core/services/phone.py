"""Contact-matching helpers (E.164, national, leading-zero).

These are utilities only; each connector decides how (and whether) to use them.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")


def digits(number: str) -> str:
    return _DIGITS.sub("", number or "")


def to_e164(number: str, default_country_code: str = "1") -> str | None:
    if not number:
        return None
    raw = number.strip()
    d = digits(raw)
    if not d:
        return None
    if raw.startswith("+"):
        return "+" + d
    if raw.startswith("00"):
        return "+" + d[2:]
    if d.startswith("0"):
        return "+" + default_country_code + d[1:]
    if default_country_code == "1" and len(d) == 10:
        return "+1" + d
    if len(d) > 10:
        return "+" + d
    return "+" + default_country_code + d


def national(number: str, country_code: str = "1") -> str | None:
    e164 = to_e164(number, country_code)
    if not e164:
        return None
    d = e164[1:]
    return d[len(country_code) :] if d.startswith(country_code) else d


def phone_variants(number: str, country_code: str = "1") -> list[str]:
    """E.164, digits-only, national and leading-zero national forms (deduplicated, ordered)."""
    out: list[str] = []
    e164 = to_e164(number, country_code)
    nat = national(number, country_code)
    for v in (
        e164,
        e164[1:] if e164 else None,
        nat,
        ("0" + nat) if nat else None,
        number.strip() if number else None,
    ):
        if v and v not in out:
            out.append(v)
    return out
