import pytest

from nemothrones.engine import clock


def test_round_trip_and_labels():
    t = clock.to_t(298, 2, 10, 16, 30)
    d = clock.from_t(t)
    assert (d.year, d.moon, d.day, d.hour, d.minute) == (298, 2, 10, 16, 30)
    assert clock.year_label(298) == "298 AC"
    assert clock.year_label(0) == "1 BC"
    assert clock.year_label(-1) == "2 BC"
    bc = clock.from_t(clock.to_t(-1, 12, 31, 23))
    assert (bc.year, bc.moon, bc.day) == (-1, 12, 31)


def test_every_day_of_a_year_round_trips():
    for moon, days in enumerate(clock.MOON_DAYS, 1):
        for day in range(1, days + 1):
            d = clock.from_t(clock.to_t(299, moon, day))
            assert (d.moon, d.day) == (moon, day)


def test_format_and_age():
    t = clock.to_t(298, 2, 10, 16)
    assert clock.format_date(t) == "10th day of the 2nd moon, 298 AC, afternoon"
    assert clock.age_years(clock.to_t(263), t) == 35
    assert clock.age_years(None, t) is None
    assert clock.ordinal(11) == "11th" and clock.ordinal(22) == "22nd" and clock.ordinal(3) == "3rd"


@pytest.mark.parametrize("text,minutes", [
    ("30m", 30), ("2 hours", 120), ("an hour", 60), ("1 day", 1440), ("3d", 4320),
    ("a fortnight", 14 * 1440), ("1 day 6 hours", 1440 + 360), ("45", 45), ("2 weeks", 14 * 1440),
])
def test_parse_duration(text, minutes):
    assert clock.parse_duration(text) == minutes


def test_parse_duration_rejects_nonsense():
    with pytest.raises(ValueError):
        clock.parse_duration("forever, or until the snows melt")


def test_next_morning():
    t = clock.to_t(298, 2, 10, 16)
    m = clock.from_t(clock.next_morning(t))
    assert (m.day, m.hour) == (11, 7)
