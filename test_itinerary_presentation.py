import pytest

from app.presentation import day_date_label, timeline_time_parts


@pytest.mark.parametrize('value,expected', [
    ('2026-10-02', '10月2日'),
    ('2026-12-31', '12月31日'),
    ('2027-01-01', '1月1日'),
    ('2026-09-31', '2026-09-31'),
    ('出发日', '出发日'),
    ('', '日期待定'),
])
def test_day_label_does_not_invent_unknown_dates(value, expected):
    assert day_date_label(value) == expected


@pytest.mark.parametrize('value,expected', [
    ('09:00-12:30', ('09:00', '12:30')),
    ('9:00 至 12:30', ('9:00', '12:30')),
    ('16:20—17:30', ('16:20', '17:30')),
    ('09：00～12：30', ('09：00', '12：30')),
    ('上午', ('上午', '')),
    ('19:30之后', ('19:30之后', '')),
    ('24:60-25:00', ('24:60-25:00', '')),
    ('', ('时间待定', '')),
])
def test_time_rail_only_splits_explicit_valid_time_ranges(value, expected):
    assert timeline_time_parts(value) == expected
