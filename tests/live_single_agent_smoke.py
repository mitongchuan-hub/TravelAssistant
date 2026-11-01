"""Opt-in, one-shot paid smoke. Never uses the application's formal SQLite DB.

Run with the project's conda interpreter and --run-live. Credentials come only
from the configured .env provider. Full model traces stay in the isolated DB;
only a sanitized summary is written beside it. No display worker is started.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='Authorize one bounded real-provider run')
    args = parser.parse_args()
    if not args.run_live:
        parser.error('--run-live is required; this script spends real model/tool quota')
    if 'pytest' in sys.modules:
        raise RuntimeError('Run standalone, not under pytest')
    started = monotonic()
    directory = Path(tempfile.mkdtemp(prefix='single-agent-live-', dir=ROOT / '.cache')).resolve()
    db = directory / 'isolated.sqlite3'
    report_path = directory / 'report.json'
    # Must precede every app import, including modules that load .env.
    os.environ['CITY_DB_PATH'] = str(db)
    os.environ['TRAVEL_PERSISTENCE'] = '1'
    os.environ.pop('PYTEST_CURRENT_TEST', None)
    os.environ.pop('TRAVEL_AGENT_DISABLE_LLM', None)
    sys.path.insert(0, str(ROOT / 'backend'))
    logging.disable(logging.CRITICAL)  # Never print upstream transport URLs/keys.
    report = {'isolated_db': str(db), 'report': str(report_path), 'html_tested': False,
              'limits': {'model_calls': 16, 'runtime_seconds': 360, 'wall_seconds': 465},
              'production_db_used': False, 'stages': [], 'runs': [], 'amap_phases': []}

    def scrub(value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        for key, secret in os.environ.items():
            if any(part in key.upper() for part in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')) and len(secret) >= 6:
                text = text.replace(secret, '[redacted]')
        return json.loads(text)

    def save():
        report['elapsed_seconds'] = round(monotonic() - started, 3)
        report_path.write_text(json.dumps(scrub(report), ensure_ascii=False, indent=2), encoding='utf-8')

    def watchdog():
        report['failure'] = {'type': 'WallTimeout', 'reason': 'One-shot wall deadline exceeded; no retry'}
        save()
        os._exit(124)

    timer = threading.Timer(345, watchdog)
    timer.daemon = True
    timer.start()
    save()
    print(json.dumps({'report': str(report_path)}, ensure_ascii=False), flush=True)
    try:
        from app.core import config, resources
        config.load_environment()
        # .env must not override the isolated fixture or the explicit live opt-in.
        os.environ.pop('PYTEST_CURRENT_TEST', None)
        os.environ.pop('TRAVEL_AGENT_DISABLE_LLM', None)
        os.environ['CITY_DB_PATH'] = str(db)
        os.environ['TRAVEL_PERSISTENCE'] = '1'
        config.validate_configuration(scope='system')
        with resources.client_scope():
            from app.agents import agent_client, budget as budget_module
            from app.storage.store import DemoStore
            from app.services.city_outings import city_requirements
            from app.agents.tools.registry import run_tool
            from app.agents.tools import amap_mcp
            from app.agents.tools.weather import budget_scope as weather_scope
            assert agent_client.is_llm_enabled(), 'Real model unavailable; no stub permitted'
            assert amap_mcp.configured(), 'AMap credentials unavailable'
            report['real_model_enabled'] = True
            report['model'] = config.model_name()
            report['wire_api'] = config.wire_api()
            preflight_budget = budget_module.TurnBudget(max_models=0, seconds=60)
            travel_date = (date.today() + timedelta(days=2)).isoformat()
            with amap_mcp.observe_phases(lambda row: report['amap_phases'].append(row)):
                with amap_mcp.budget_scope(preflight_budget):
                    hotels = run_tool('search_places', {'city': '杭州', 'keywords': '西湖附近如家酒店'})
            report['stages'].append({'stage': 'real_hotel_search', 'status': hotels.get('status'),
                                     'candidate_count': len(hotels.get('candidates', [])),
                                     'reason': hotels.get('reason')})
            save()
            assert hotels.get('status') == 'available' and hotels.get('candidates'), 'Real AMap hotel search failed'
            hotel = next((row for row in hotels['candidates'] if '如家' in row['name']), hotels['candidates'][0])
            report['selected_hotel'] = hotel
            with weather_scope(preflight_budget):
                weather = run_tool('query_weather', {'city': hotel['city'], 'date': travel_date})
            report['stages'].append({'stage': 'real_weather_preflight', 'result': weather})
            store = DemoStore(db_path=str(db), persistence_enabled=True, display_worker_enabled=False)
            # Remove demo artifacts only in this new isolated fixture.
            store.trips.clear()
            store.messages.clear()
            store.plans.clear()
            store.idea_cards.clear()
            store.tasks.clear()
            changes = city_requirements({'lodging_address': hotel['name'], 'start_date': travel_date,
                'day_count': '1', 'travelers': '2', 'transport_mode': 'driving',
                'pace': '轻松，少走路', 'dietary': '不吃辣', 'outing_start_time': '10:00',
                'outing_end_time': '17:00'}, city=hotel['city'])
            trip = store.create_trip('杭州真实隔离冒烟', travel_date, '', '轻松', '',
                                     requirements=changes, lodging=hotel)
            trip_id = trip.id
            assert not store.idea_cards[trip_id] and not store.plans[trip_id].has_content
            assert store.db_path == os.environ['CITY_DB_PATH'] == str(db)
            report['trip_id'] = trip_id
            report['fixture_requirements'] = asdict(trip.requirements)
            save()

            # Instrument only the smoke harness; the production real-client function,
            # runtime, validators, submit_task/run_task and commit path remain intact.
            base_budget = budget_module.TurnBudget
            budgets = []
            runtime_deadline = monotonic() + min(240, 335 - (monotonic() - started))

            class SmokeBudget(base_budget):
                def __init__(self, *a, **kw):
                    kw['max_models'] = min(kw.get('max_models', 16), 16 - sum(b.model_calls for b in budgets))
                    kw['seconds'] = min(kw.get('seconds', 240), max(0.01, runtime_deadline - monotonic()))
                    super().__init__(*a, **kw)
                    budgets.append(self)

            budget_module.TurnBudget = SmokeBudget
            original_generate = agent_client.generate_task_result
            active = {}

            def generate(context, execution, **kw):
                proposal = original_generate(context, execution, **kw)
                active['proposal'] = {'failed': proposal.failed, 'reason': proposal.reason,
                    'has_changes': proposal.changes is not None,
                    'change_operations': list(proposal.changes['args']) if proposal.changes else []}
                return proposal

            agent_client.generate_task_result = generate
            original_progress = store.record_task_progress

            def progress(task_id, title, value):
                row = active.get('summary')
                if row is not None and isinstance(value, dict):
                    if title == '模型请求':
                        row['model_requests'].append({k: value.get(k) for k in ('model', 'agent_role', 'wire_api', 'reasoning')})
                    elif title == '模型响应':
                        row['model_responses'].append({'role': value.get('agent_role'), 'seconds': value.get('seconds'),
                            'usage': value.get('usage'), 'tool_batch_size': len(value.get('tool_calls', [])),
                            'tools': [x.get('name') for x in value.get('tool_calls', [])][:20]})
                    elif title == '工具执行':
                        result = value.get('result')
                        row['tools'].append({'name': value.get('name'), 'seconds': value.get('seconds'),
                            'error_type': value.get('error_type'), 'arguments': value.get('arguments'),
                            'status': result.get('status') if isinstance(result, dict) else None,
                            'reason': result.get('reason') if isinstance(result, dict) else None})
                    elif title in {'Task Runtime预算', 'Task Runtime耗时', 'Task Runtime异常', 'Task Execution结果'}:
                        row.setdefault('runtime_events', []).append({'title': title, 'value': value})
                original_progress(task_id, title, value)

            store.record_task_progress = progress

            def run_once(label, body):
                summary = {'label': label, 'body': body, 'model_requests': [], 'model_responses': [], 'tools': []}
                report['runs'].append(summary)
                active['summary'] = summary
                active.pop('proposal', None)
                task, created = store.submit_task(trip_id, 'chat', {'body': body}, 'live-' + label)
                assert created
                t = monotonic()
                with amap_mcp.observe_phases(lambda row: report['amap_phases'].append(row)):
                    store.run_task(task['id'])
                summary['seconds'] = round(monotonic() - t, 3)
                summary['proposal'] = active.get('proposal')
                saved_task = store.get_task(task['id'])
                summary['task'] = {k: saved_task.get(k) for k in ('id', 'status', 'message', 'committed_effect_kinds', 'receipt')}
                restored = DemoStore(db_path=str(db), persistence_enabled=True, display_worker_enabled=False)
                plan = store.plans[trip_id]
                summary['committed_state'] = {'plan_id': plan.id, 'text': plan.text,
                    'requirements': asdict(store.trips[trip_id].requirements), 'evidence': plan.evidence,
                    'display_status': plan.display_status}
                summary['reload'] = {'plan_equal': restored.plans[trip_id] == plan,
                    'requirements_equal': restored.trips[trip_id].requirements == store.trips[trip_id].requirements,
                    'task_equal': restored.tasks[task['id']] == store.tasks[task['id']],
                    'idea_count': len(restored.idea_cards[trip_id])}
                save()
                return saved_task, summary

            body = (f'请生成{travel_date}杭州同城一日轻松行程，2人，不吃辣。'
                    f'从已确认的{hotel["name"]}上午10点出发，下午17点前返回同一住宿。'
                    '仅安排西湖湖滨附近一个主要游览点和简单午餐，避免赶路。'
                    '请实际查询当天杭州天气、游览点及住宿往返路线，优先短距离接驳；'
                    '无法确认的营业、价格或餐厅信息明确写待核实，不要编造。生成后保存文字行程即可。')
            task, generation = run_once('generation', body)
            # Explicit checks avoid treating an empty needs-input reply as proof.
            success = (task['status'] == 'succeeded' and store.plans[trip_id].has_content
                       and bool(generation.get('proposal')) and not generation['proposal']['failed']
                       and generation['reload']['plan_equal'] and generation['reload']['requirements_equal']
                       and generation['reload']['task_equal'])
            report['generation_success'] = bool(success)
            if success and runtime_deadline - monotonic() >= 45 and sum(b.model_calls for b in budgets) <= 14:
                before_text = store.plans[trip_id].text
                before_basis = deepcopy(store.plans[trip_id].requirements_snapshot)
                follow_task, follow = run_once('requirements-only', '只把饮食要求再加上“不吃香菜”，不要修改、重新生成或清空已保存的行程。')
                report['followup_success'] = (follow_task['status'] == 'succeeded'
                    and '不吃香菜' in store.trips[trip_id].requirements.dietary
                    and store.plans[trip_id].text == before_text
                    and store.plans[trip_id].requirements_snapshot == before_basis
                    and follow['reload']['requirements_equal'] and follow['reload']['plan_equal'])
            else:
                report['followup_skipped'] = 'Generation failed or insufficient remaining budget; no paid retry'
            report['total_runtime_usage'] = {key: sum(b.usage()[key] for b in budgets) for key in base_budget().usage()}
            import sqlite3
            with sqlite3.connect(db) as connection:
                report['sqlite_tables'] = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                report['isolated_trace_rows'] = connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0]
            assert report['total_runtime_usage']['model_calls'] <= 16
            report['completed'] = True
    except Exception as exc:
        report['failure'] = {'type': type(exc).__name__, 'reason': str(exc)[:1000]}
    finally:
        timer.cancel()
        save()
    print(json.dumps(scrub({'report': str(report_path), 'completed': report.get('completed', False),
        'generation_success': report.get('generation_success'), 'followup_success': report.get('followup_success'),
        'failure': report.get('failure'), 'elapsed_seconds': report['elapsed_seconds']}), ensure_ascii=False), flush=True)
    return 0 if report.get('generation_success') and not report.get('failure') else 1


if __name__ == '__main__':
    raise SystemExit(main())
