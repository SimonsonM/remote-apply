"""Short-commute local roles use a lower pay floor than remote ones."""
import apply

P = {"min_salary": 80000, "local_min_salary": 60000, "short_commute_miles": 40}


def test_local_short_commute_gets_lower_floor(monkeypatch):
    monkeypatch.setattr(apply, "local_ok", lambda loc, radius=40: "Madison" in loc)
    assert apply.pay_floor("Madison, WI", P) == 60000
    assert apply.pay_floor("Remote - Madison, WI", P) == 80000      # remote keeps the remote floor
    assert apply.pay_floor("Chicago, IL", P) == 80000               # long commute
    assert apply.pay_floor("Madison, WI", {"min_salary": 80000}) == 80000   # no setting: one floor
