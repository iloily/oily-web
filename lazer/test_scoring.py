"""Run with: python -m pytest lazer/test_scoring.py (no database or pp calculator needed)."""
import scoring


def test_default_speeds():
    assert scoring.clock_rate([{"acronym": "DT"}]) == 1.5
    assert scoring.clock_rate([{"acronym": "NC"}]) == 1.5
    assert scoring.clock_rate([{"acronym": "HT"}]) == 0.75
    assert scoring.clock_rate([{"acronym": "HD"}]) is None
    assert scoring.clock_rate([]) is None


def test_custom_speeds():
    assert scoring.clock_rate([{"acronym": "DT", "settings": {"speed_change": 1.3}}]) == 1.3
    assert scoring.clock_rate([{"acronym": "HT", "settings": {"speed_change": 0.9}}]) == 0.9
    assert scoring.clock_rate([{"acronym": "DT", "settings": {"speed_change": 9}}]) == 2.0
    assert scoring.clock_rate([{"acronym": "DT", "settings": {"speed_change": "bad"}}]) == 1.5


def test_difficulty_adjust():
    mods = [{"acronym": "DA", "settings": {"approach_rate": 10.5, "circle_size": 4, "overall_difficulty": 99}}]
    assert scoring.difficulty_overrides(mods) == {"ar": 10.5, "cs": 4.0, "od": 11.0}
    assert scoring.difficulty_overrides([{"acronym": "DA"}]) == {}
    assert scoring.difficulty_overrides([{"acronym": "HR"}]) == {}


def test_custom_speed_goes_to_mods_board():
    assert scoring.board_for([{"acronym": "DT", "settings": {"speed_change": 1.3}}], 0) == scoring.BOARD_MODS
    assert scoring.board_for([{"acronym": "RX"}, {"acronym": "DT"}], 0) == scoring.BOARD_RELAX
