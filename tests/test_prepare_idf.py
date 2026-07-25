"""Setpoint-extraction logic from Schedule:Compact.

This is the part of ``prepare_idf`` that can be wrong *quietly*: pick the setback value
instead of the occupied one and every run is subtly mis-conditioned with nothing in the error
log. The eppy manipulation around it needs a real IDD and is exercised by running the script.
"""

from __future__ import annotations

import pytest

from simulation.prepare_idf import numeric_fields, occupied_setpoint

# A realistic DOE-prototype cooling setpoint schedule: 26.7 occupied, 29.4 setback.
COOLING_COMPACT = [
    "Through: 12/31",
    "For: Weekdays SummerDesignDay",
    "Until: 06:00",
    "29.4",
    "Until: 22:00",
    "26.7",
    "Until: 24:00",
    "29.4",
    "For: AllOtherDays",
    "Until: 24:00",
    "29.4",
]

HEATING_COMPACT = [
    "Through: 12/31",
    "For: Weekdays WinterDesignDay",
    "Until: 06:00",
    "15.6",
    "Until: 22:00",
    "21.1",
    "Until: 24:00",
    "15.6",
    "For: AllOtherDays",
    "Until: 24:00",
    "15.6",
]


def test_directives_are_not_mistaken_for_values() -> None:
    """``Until: 24:00`` contains digits but is not a temperature."""
    assert 24.0 not in numeric_fields(["Until: 24:00", "26.7"])
    assert numeric_fields(["Until: 24:00", "26.7"]) == [26.7]


def test_sentinels_outside_the_setpoint_band_are_dropped() -> None:
    # Prototypes use 99 / -99 to mean "no control"; picking one would be catastrophic.
    assert numeric_fields(["99.0", "26.7", "-99.0"]) == [26.7]


def test_cooling_picks_the_lowest_occupied_value() -> None:
    # Unoccupied cooling setback is HIGHER, so occupied is the minimum.
    assert occupied_setpoint(COOLING_COMPACT, "cooling") == pytest.approx(26.7)


def test_heating_picks_the_highest_occupied_value() -> None:
    # Unoccupied heating setback is LOWER, so occupied is the maximum.
    assert occupied_setpoint(HEATING_COMPACT, "heating") == pytest.approx(21.1)


def test_no_usable_values_returns_none() -> None:
    assert occupied_setpoint(["Through: 12/31", "For: AllDays", "Until: 24:00"], "cooling") is None


def test_empty_schedule_returns_none() -> None:
    assert occupied_setpoint([], "heating") is None


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="cooling"):
        occupied_setpoint(COOLING_COMPACT, "sideways")


def test_blank_and_whitespace_fields_are_ignored() -> None:
    assert numeric_fields(["", "   ", "26.7", "\t"]) == [26.7]


def test_heuristic_errs_toward_tighter_comfort() -> None:
    """Whatever it picks must never be looser than every value in the schedule."""
    cooling = occupied_setpoint(COOLING_COMPACT, "cooling")
    heating = occupied_setpoint(HEATING_COMPACT, "heating")
    assert cooling <= max(numeric_fields(COOLING_COMPACT))
    assert heating >= min(numeric_fields(HEATING_COMPACT))
