"""Regression tests for UI formatting of exact times."""

from fractions import Fraction

from video_editor.ui.format import format_rate, format_time


def test_format_time_exact_rounding():
    assert format_time(Fraction(0)) == "0:00:00.000"
    assert format_time(Fraction(3661, 1)) == "1:01:01.000"
    assert format_time(Fraction(1, 3)) == "0:00:00.333"
    assert format_time(Fraction(2, 3)) == "0:00:00.667"  # rounds half up
    assert format_time(Fraction(1, 1000) * Fraction(1, 2)) == "0:00:00.001"  # 0.5ms -> 1
    assert format_time(None) == "unknown"


def test_format_time_long_durations():
    assert format_time(Fraction(10 * 3600 + 5)) == "10:00:05.000"


def test_format_rate():
    assert format_rate(Fraction(25)) == "25"
    assert format_rate(Fraction(24000, 1001)) == "23.976"
    assert format_rate(None) == "rate unknown"
