"""remote_regions: remote roles limited to chosen regions (here US, Canada, EU)."""
import apply

P = {"remote_regions": ["us", "ca", "eu"]}


def test_allowed_regions():
    for loc in ("Remote - United States", "Remote - Remote (Canada) / Canada", "Remote - Spain - Barcelona / ES",
                "Remote - Hungary - Budapest / HU", "Remote Ireland", "Remote", "Remote - Worldwide", "Remote - EMEA",
                "SF, NYC, Seattle, Remote", "Remote - AZ", "Remote - AZ; Remote - CA; Remote - TX"):
        assert apply.remote_region_ok(loc, P), loc


def test_blocked_regions():
    for loc in ("Remote - United Kingdom - London / GB", "Remote - Colombia", "Remote - India - Hyderabad / IN",
                "Remote - San Jose, Costa Rica / CR", "UK Remote"):
        assert not apply.remote_region_ok(loc, P), loc


def test_default_without_setting_is_us_only():
    assert apply.remote_region_ok("Remote - United States", {"remote_anywhere": False})
    assert not apply.remote_region_ok("Remote - Germany", {"remote_anywhere": False})


def test_uk_region_opt_in():
    uk = {"remote_regions": ["us", "ca", "eu", "uk"]}
    for loc in ("Remote - United Kingdom - London / GB", "Cardiff, London or Remote (UK)", "UK Remote"):
        assert apply.remote_region_ok(loc, uk), loc
    assert not apply.remote_region_ok("Remote - Colombia", uk)
