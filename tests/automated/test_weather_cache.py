"""Reuse Open-Meteo data across dates within one turn, never across fresh turns."""
from copy import deepcopy

from app.tools import weather
from app.tools.registry import run_tool


def install(monkeypatch):
    calls = []
    forecast = {'timezone': 'Asia/Shanghai', 'daily': {
        'time': ['2026-10-19', '2026-10-20'], 'weather_code': [3, 51],
        'temperature_2m_max': [26, 24], 'temperature_2m_min': [16, 18],
        'precipitation_probability_max': [27, 23]}}
    def fetch(url, params):
        calls.append((url, deepcopy(params)))
        if url == weather._GEOCODING_URL:
            return {'results': [{'name': params['name'], 'latitude': 39.63, 'longitude': 118.18}]}
        return deepcopy(forecast)
    monkeypatch.setattr(weather, '_fetch_json', fetch)
    return calls, fetch


def test_two_dates_use_two_http_requests_and_return_distinct_days(monkeypatch):
    calls, _ = install(monkeypatch)
    cache = {}
    first = run_tool('query_weather', {'city': '唐山', 'date': '2026-10-19'}, route_cache=cache)
    second = run_tool('query_weather', {'city': '唐山', 'date': '2026-10-20'}, route_cache=cache)
    assert len(calls) == 2
    assert first['temperature_max_c'] == 26 and second['temperature_max_c'] == 24
    assert first['requested_date'] != second['requested_date']
    assert first['source'] == second['source'] == 'Open-Meteo'
    second['temperature_max_c'] = -99
    assert run_tool('query_weather', {'city': '唐山', 'date': '2026-10-20'}, route_cache=cache)['temperature_max_c'] == 24
    assert len(calls) == 2


def test_city_and_new_turn_have_independent_forecasts(monkeypatch):
    calls, _ = install(monkeypatch)
    cache = {}
    for city in ('唐山', '杭州'):
        run_tool('query_weather', {'city': city, 'date': '2026-10-19'}, route_cache=cache)
    assert len(calls) == 4
    run_tool('query_weather', {'city': '唐山', 'date': '2026-10-19'}, route_cache={})
    assert len(calls) == 6


def test_out_of_range_and_preview_reuse_forecast_without_inventing_weather(monkeypatch):
    calls, _ = install(monkeypatch)
    cache = {}
    unknown = run_tool('query_weather', {'city': '唐山', 'date': '2026-11-01'}, route_cache=cache)
    assert unknown['status'] == 'unavailable' and 'temperature_max_c' not in unknown
    preview = run_tool('query_weather', {'city': '唐山'}, route_cache=cache)
    assert preview['status'] == 'available' and len(preview['forecast_preview']) == 2
    assert len(calls) == 2


def test_failed_forecast_is_not_cached_and_successful_location_is_reused(monkeypatch):
    calls, fetch = install(monkeypatch)
    failures = [True]
    def flaky(url, params):
        if url == weather._FORECAST_URL and failures[0]:
            failures[0] = False
            calls.append((url, deepcopy(params)))
            raise TimeoutError('temporary failure')
        return fetch(url, params)
    monkeypatch.setattr(weather, '_fetch_json', flaky)
    cache = {}
    assert run_tool('query_weather', {'city': '唐山', 'date': '2026-10-19'}, route_cache=cache)['status'] == 'unavailable'
    assert run_tool('query_weather', {'city': '唐山', 'date': '2026-10-20'}, route_cache=cache)['status'] == 'available'
    assert len(calls) == 3
    assert sum(url == weather._GEOCODING_URL for url, _ in calls) == 1


def test_invalid_date_does_not_make_requests(monkeypatch):
    calls, _ = install(monkeypatch)
    assert run_tool('query_weather', {'city': '唐山', 'date': 'not-a-date'}, route_cache={})['status'] == 'unavailable'
    assert not calls
