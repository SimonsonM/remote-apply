"""Salary floor planning and unverified research claims stay advisory."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

import income
import state
import apply


def sample_inventory(**extra):
    return {'skills': [], 'assets': [], 'equipment': [], 'weekly_hours': None,
            'budget': 0, 'gpu_constraint': False, 'platforms': [], **extra}


def test_inventory_preserves_known_floors_and_ignores_unknown_keys(tmp_path):
    floors = {'part_time_remote_annual': 60000, 'full_time_quick_hire_annual': None,
              'future_floor': 'ignored'}
    income.save_inventory(tmp_path, sample_inventory(salary_floors=floors))
    assert income.load_inventory(tmp_path)['salary_floors'] == {
        'part_time_remote_annual': 60000, 'full_time_quick_hire_annual': None}
    assert income.offers(tmp_path)  # unknown weekly hours is allowed
    answers = iter(['', '', '', '', '0', 'no', ''])
    income.inventory(tmp_path, read=lambda _: next(answers))
    assert income.load_inventory(tmp_path)['salary_floors']['part_time_remote_annual'] == 60000


@pytest.mark.parametrize('floors', [[], 4, '60000', {'part_time_remote_annual': 0},
    {'part_time_remote_annual': -1}, {'part_time_remote_annual': '60000'},
    {'part_time_remote_annual': True}, {'part_time_remote_annual': float('inf')}])
def test_bad_floor_input_has_clear_error(tmp_path, floors):
    with pytest.raises(ValueError, match='salary_floors'):
        income.save_inventory(tmp_path, sample_inventory(salary_floors=floors))
    path = tmp_path / 'income' / 'inventory.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(sample_inventory(salary_floors=floors)))
    with pytest.raises(ValueError, match='salary_floors'):
        income.load_inventory(tmp_path)


def test_invalid_json_has_clear_error(tmp_path):
    path = tmp_path / 'income' / 'inventory.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text('{bad')
    with pytest.raises(ValueError, match='Invalid inventory JSON'):
        income.load_inventory(tmp_path)


def test_annualization_and_soft_labels(tmp_path):
    income.save_inventory(tmp_path, sample_inventory(salary_floors={
        'part_time_remote_annual': 60000, 'full_time_quick_hire_annual': 90000}))
    score = income.score_job(tmp_path, 45, 50, 'hourly', 'full_time')
    assert (score['annual_min'], score['annual_max'], score['label'], score['score']) == (93600, 104000, 'meets_floor', 1)
    score = income.score_job(tmp_path, 20, 25, 'hourly', 'part_time', 20)
    assert (score['annual_min'], score['annual_max'], score['label'], score['score']) == (20800, 26000, 'below_floor', -1)
    assert income.score_job(tmp_path, 60000, None, 'annual', 'part_time')['label'] == 'meets_floor'
    assert income.score_job(tmp_path, None, 89000, 'annual', 'full_time')['label'] == 'below_floor'


def test_missing_and_uncertain_salary_is_neutral(tmp_path):
    income.save_inventory(tmp_path, sample_inventory(salary_floors={
        'part_time_remote_annual': 60000, 'full_time_quick_hire_annual': 90000}))
    assert income.score_job(tmp_path, employment_type='full_time')['label'] == 'unknown_salary'
    assert income.score_job(tmp_path, employment_type='full_time')['score'] == 0
    missing_hours = income.score_job(tmp_path, 60, 70, 'hourly', 'part_time')
    assert missing_hours['label'] == 'unknown_salary'
    assert 'without hours per week' in missing_hours['note']
    assert missing_hours['annual_min'] is None
    assert income.score_job(tmp_path, 85000, 95000, 'annual', 'full_time')['label'] == 'unknown_salary'
    assert income.score_job(tmp_path, 100000, 110000, 'annual', 'unknown')['label'] == 'unknown_salary'
    assert income.score_job(tmp_path, 50, 60, 'hourly', 'unknown')['annual_min'] is None


@pytest.mark.parametrize('kwargs', [dict(minimum=-1), dict(minimum=20, maximum=10),
    dict(minimum=float('nan')), dict(hours_per_week=169), dict(unit='weekly')])
def test_bad_posting_inputs(tmp_path, kwargs):
    with pytest.raises(ValueError):
        income.score_job(tmp_path, **kwargs)


def test_cli_listing_and_scoring_are_read_only(tmp_path, monkeypatch, capsys):
    income.save_inventory(tmp_path, sample_inventory(salary_floors={
        'part_time_remote_annual': 60000, 'full_time_quick_hire_annual': 90000}))
    before = (tmp_path / 'income' / 'inventory.json').read_bytes()
    monkeypatch.setattr(apply, 'ROOT', tmp_path)
    apply.income_cmd(SimpleNamespace(income_action='floors'))
    assert 'part_time_remote_annual: 60000' in capsys.readouterr().out
    apply.income_cmd(SimpleNamespace(income_action='score-job', minimum=30, maximum=40,
        unit='hourly', employment_type='part_time', hours=None))
    output = capsys.readouterr().out
    assert 'unknown_salary | score 0' in output
    assert 'without hours per week' in output
    assert (tmp_path / 'income' / 'inventory.json').read_bytes() == before
    assert not (tmp_path / 'applications.sqlite3').exists()


def test_missing_source_or_unmatched_quote_downgrades_fact(tmp_path):
    conn = sqlite3.connect(tmp_path / 'test.sqlite3')
    state.migrate(conn)
    try:
        ident = income.new(conn, 'Test', 'Maybe useful', 'Stop', 1)
        source_path = income.private_write(tmp_path / 'source.txt', 'Stored fetched text')
        source_id = conn.execute(
            'INSERT INTO income_sources(experiment_id,url,title,retrieved_at,http_status,text_sha256,text_path,excerpt) VALUES (?,?,?,?,?,?,?,?)',
            (ident, 'https://example.test', 'test', state.now(), 200,
             hashlib.sha256(source_path.read_bytes()).hexdigest(), str(source_path), 'Stored fetched text')).lastrowid
        for cited_source, quote in ((999999, 'Stored fetched text'), (source_id, 'Absent passage')):
            claim_id = income.claim(conn, ident, 'Unsupported assertion', 'fact', cited_source, quote)
            row = conn.execute('SELECT type,status,source_id,quote,flag FROM income_claims WHERE id=?', (claim_id,)).fetchone()
            assert row[:4] == ('hypothesis', 'unverified', None, None)
            assert 'Unverified fact' in row[4]
    finally:
        conn.close()
