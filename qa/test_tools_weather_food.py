"""Regression tests for agent tool helpers: weather and food.

These pure/edge helpers are not directly unit-tested elsewhere. The weather
network layer is mocked (_fetch_json); only deterministic parsing/advice logic
is asserted. No real map or weather service is contacted.
"""
import pytest

import app.agents.tools.food as food
import app.agents.tools.weather as weather


GEO = {'results': [{'name': '杭州', 'latitude': 30, 'longitude': 120, 'country': 'CN'}]}
FORECAST = {
    'timezone': 'Asia/Shanghai',
    'daily': {
        'time': ['2026-10-01', '2026-10-02'],
        'weather_code': [0, 61],
        'temperature_2m_max': [30, 20],
        'temperature_2m_min': [20, 10],
        'precipitation_probability_max': [10, 80],
    },
}


@pytest.fixture
def mock_fetch(monkeypatch):
    monkeypatch.setattr(weather, '_fetch_json',
                        lambda url, params: GEO if 'geocoding' in url else FORECAST)


# --- weather: date handling -------------------------------------------------

def test_weather_in_range_date_returns_condition(mock_fetch):
    result = weather.query_weather({'city': '杭州', 'date': '2026-10-02'})
    assert result['status'] == 'available'
    assert result['condition'] == '小雨'
    assert result['clothing_advice']


def test_weather_out_of_range_date_is_unavailable(mock_fetch):
    result = weather.query_weather({'city': '杭州', 'date': '2026-12-01'})
    assert result['status'] == 'unavailable'
    assert '超出' in result['reason']


def test_weather_bad_date_format_is_unavailable(mock_fetch):
    result = weather.query_weather({'city': '杭州', 'date': 'bad-date'})
    assert result['status'] == 'unavailable'
    assert 'YYYY-MM-DD' in result['reason']


def test_weather_without_date_returns_preview(mock_fetch):
    result = weather.query_weather({'city': '杭州'})
    assert result['status'] == 'available'
    assert len(result['forecast_preview']) == 2


def test_weather_missing_city_is_unavailable(mock_fetch):
    assert weather.query_weather({'city': '   '})['status'] == 'unavailable'


def test_weather_no_results_is_unavailable(monkeypatch):
    monkeypatch.setattr(weather, '_fetch_json', lambda url, params: {'results': []})
    assert weather.query_weather({'city': '杭州'})['status'] == 'unavailable'


# --- weather: clothing advice ----------------------------------------------

@pytest.mark.parametrize('mx,mn,prec,needle', [
    (40, 30, 0, '遮阳'),
    (35, 25, 0, '遮阳'),
    (29, 20, 0, '短袖'),
    (15, 5, 0, '防寒'),
    (25, 20, 60, '折叠伞'),
    (None, None, None, '分层穿衣'),
])
def test_clothing_advice_branches(mx, mn, prec, needle):
    assert needle in weather._clothing_advice(mx, mn, prec)


def test_clothing_advice_adds_umbrella_only_at_high_probability():
    assert '折叠伞' not in weather._clothing_advice(25, 20, 49)
    assert '折叠伞' in weather._clothing_advice(25, 20, 50)


# --- food: helpers ----------------------------------------------------------

@pytest.mark.parametrize('value,expected', [
    (None, 5), ('', 5), ('abc', 5), (0, 1), (1, 1), (5, 5), (6, 5), (-1, 1), (2.5, 2), ('3', 3),
])
def test_clamp_max_results(value, expected):
    assert food._clamp_max_results(value) == expected


@pytest.mark.parametrize('value,expected', [
    (None, None), ('', None), ('3.5', 3.5), ('abc', None), (-1, None),
    (0, 0.0), (float('nan'), None), (float('inf'), None), (5, 5.0),
])
def test_to_float(value, expected):
    assert food._to_float(value) == expected


def test_summarize_poi_normalizes_and_defaults():
    poi = {'name': '  测试餐厅 ', 'rating': '4.5', 'cost': '80',
           'address': '测试路', 'adname': '西湖区', 'biz_ext': {}}
    summary = food._summarize_poi(poi)
    assert summary['name'] == '测试餐厅'
    assert summary['rating'] == '4.5'
    assert summary['cost_per_person_cny'] == 80.0
    assert summary['district'] == '西湖区'
    assert summary['tel'] is None


def test_summarize_poi_falls_back_to_biz_ext():
    poi = {'name': '店', 'biz_ext': {'rating': '4.0', 'cost': '60'}}
    summary = food._summarize_poi(poi)
    assert summary['rating'] == '4.0'
    assert summary['cost_per_person_cny'] == 60.0


def test_search_food_requires_city(monkeypatch):
    monkeypatch.setattr(food.amap_mcp, 'configured', lambda: True)
    result = food.search_food({'city': '   '})
    assert result['status'] == 'unavailable'


def test_search_food_unconfigured(monkeypatch):
    monkeypatch.setattr(food.amap_mcp, 'configured', lambda: False)
    result = food.search_food({'city': '杭州'})
    assert result['status'] == 'unavailable'
    assert '未配置' in result['reason']
