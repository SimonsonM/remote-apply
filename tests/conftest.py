"""Shared test setup."""

# Tests run against a fictional home (Columbus, OH), never the user's profile.json location.
import pytest as _pytest
import apply as _apply

_HOME = (39.9612, -82.9988)


@_pytest.fixture(autouse=True)
def fictional_home(monkeypatch):
    monkeypatch.setattr(_apply, "HOME", _HOME)
    monkeypatch.setattr(_apply, "HOME_STATES", ["oh", "ohio"])
    monkeypatch.setattr(_apply, "LOCAL_PLACES", {"columbus": _HOME, "dublin": (40.0992, -83.1141),
                                                 "westerville": (40.1262, -82.9291), "delaware": (40.2987, -83.0680), "dayton": (39.7589, -84.1916)})
    monkeypatch.setattr(_apply, "_LOC", {"zip": "43215"})
    monkeypatch.setattr(_apply, "METRO_CENTER", None)
    monkeypatch.setattr(_apply, "METRO_PLACES", {})
    monkeypatch.setattr(_apply, "METRO_STATES", [])
