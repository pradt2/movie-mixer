"""Exact time helpers shared by the core."""

from __future__ import annotations

from fractions import Fraction


def parse_fraction(text: str) -> Fraction:
    """Parse an exact rational from user text: '24000/1001', '23.976', '0.5'.

    Raises ValueError with a useful message on garbage input.
    """
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty number")
    try:
        return Fraction(cleaned)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"not an exact number: {text!r}") from exc


def overlaps(a_start: Fraction, a_end: Fraction, b_start: Fraction, b_end: Fraction) -> bool:
    """Half-open interval intersection test."""
    return a_start < b_end and b_start < a_end


def clamp(value: Fraction, low: Fraction, high: Fraction) -> Fraction:
    return max(low, min(high, value))


ZERO = Fraction(0)
ONE = Fraction(1)
