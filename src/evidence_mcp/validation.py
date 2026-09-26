"""Input validation for everything a model can send to the server.

Tool arguments come from a language model, so they are treated as untrusted input.
Each value is checked against a strict pattern before it is placed in a URL. That keeps
requests well-formed and makes path or query injection impossible: nothing outside
these character sets ever reaches the upstream API.
"""

from __future__ import annotations

import re

# Agency and dataflow ids, e.g. "OECD.STI.STP" or "DSD_MSTI@DF_MSTI". The first character must
# be a letter or digit, so an id can never be "." or ".." (a path traversal segment).
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_@.\-]{0,119}$")
# Versions, e.g. "1.3", "2.0.1", or the keyword "latest".
_VERSION = re.compile(r"^(latest|\d+(\.\d+){0,2})$", re.ASCII)
# SDMX series keys: codes separated by "." (one slot per dimension), "+" means OR,
# an empty slot means "all codes", e.g. "FRA+DEU.A.G..." or "" for everything.
_KEY = re.compile(r"^[A-Za-z0-9_@\-+.]{0,600}$")
# Periods: 2020, 2020-Q1, 2020-S1, 2020-01, 2020-W05, 2020-01-31.
_PERIOD = re.compile(r"^\d{4}(-(Q[1-4]|S[12]|W\d{2}|\d{2}(-\d{2})?))?$", re.ASCII)


class InvalidArgument(ValueError):
    """Raised when a tool argument fails validation. The message is shown to the model."""


def check_id(value: str, field: str) -> str:
    value = value.strip()
    if not _ID.fullmatch(value):
        raise InvalidArgument(
            f"{field} {value!r} is not a valid SDMX identifier "
            "(letters, digits, '_', '@', '.', '-' only)."
        )
    return value


def check_version(value: str) -> str:
    value = value.strip() or "latest"
    if not _VERSION.fullmatch(value):
        raise InvalidArgument(f"version {value!r} must look like '1.0' or be 'latest'.")
    return value


def check_key(value: str) -> str:
    value = value.strip()
    # "all", and a key made only of dots (every slot empty), both mean "all series". Normalising
    # them to "" also guarantees the last URL segment is never "." or "..".
    if value.lower() == "all" or set(value) <= {"."}:
        return ""
    if not _KEY.fullmatch(value):
        raise InvalidArgument(
            f"key {value!r} is not a valid SDMX series key. Use codes separated by '.', "
            "'+' to combine codes, and leave a slot empty for all codes."
        )
    return value


def check_period(value: str | None, field: str) -> str | None:
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if not _PERIOD.fullmatch(value):
        raise InvalidArgument(f"{field} {value!r} must look like 2020, 2020-Q1 or 2020-01.")
    return value


def check_range(value: int, field: str, low: int, high: int) -> int:
    if not low <= value <= high:
        raise InvalidArgument(f"{field} must be between {low} and {high}, got {value}.")
    return value


def check_query(value: str, field: str = "query", max_len: int = 300) -> str:
    value = " ".join(value.split())
    if not value:
        raise InvalidArgument(f"{field} must not be empty.")
    if len(value) > max_len:
        raise InvalidArgument(f"{field} must be at most {max_len} characters.")
    return value
