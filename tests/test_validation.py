import pytest

from evidence_mcp.validation import (
    InvalidArgument,
    check_id,
    check_key,
    check_period,
    check_query,
    check_range,
    check_version,
)


@pytest.mark.parametrize("value", ["OECD.STI.STP", "DSD_MSTI@DF_MSTI", "ECB", "EXR-1"])
def test_valid_ids(value):
    assert check_id(value, "agency") == value


@pytest.mark.parametrize(
    "value", ["", "..", ".", ".hidden", "../etc", "OECD/../x", "a b", "x?y=1", "a" * 121]
)
def test_invalid_ids_are_rejected(value):
    with pytest.raises(InvalidArgument):
        check_id(value, "agency")


@pytest.mark.parametrize("value", ["FRA+DEU.A.G..", "", "....", "all"])
def test_valid_keys(value):
    check_key(value)


def test_all_and_all_empty_slots_mean_empty_key():
    assert check_key("ALL") == ""
    assert check_key("..") == ""  # never a ".." path segment
    assert check_key(".....") == ""
    assert check_key("..FRA") == "..FRA"


@pytest.mark.parametrize("value", ["FRA/../x", "FRA?format=xml", "FRA DEU", "FRA;DEU"])
def test_keys_cannot_inject_path_or_query(value):
    with pytest.raises(InvalidArgument):
        check_key(value)


@pytest.mark.parametrize(
    "value", ["2020", "2020-Q1", "2020-S2", "2020-01", "2020-W05", "2020-01-31"]
)
def test_valid_periods(value):
    assert check_period(value, "start_period") == value


@pytest.mark.parametrize("value", ["20", "2020-Q5", "last year", "2020/01"])
def test_invalid_periods(value):
    with pytest.raises(InvalidArgument):
        check_period(value, "start_period")


def test_empty_period_is_none():
    assert check_period("  ", "start_period") is None
    assert check_period(None, "start_period") is None


def test_versions():
    assert check_version("") == "latest"
    assert check_version("1.3") == "1.3"
    with pytest.raises(InvalidArgument):
        check_version("v1")


def test_range_and_query():
    assert check_range(5, "limit", 1, 10) == 5
    with pytest.raises(InvalidArgument):
        check_range(0, "limit", 1, 10)
    assert check_query("  R&D   spending ") == "R&D spending"
    with pytest.raises(InvalidArgument):
        check_query("   ")
    with pytest.raises(InvalidArgument):
        check_query("x" * 301)
