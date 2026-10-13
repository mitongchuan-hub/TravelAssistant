"""Read-only logical deployment fingerprint; stdout contains no user content."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('before', 'after'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = ROOT/'.cache/travelassistant.sqlite3'
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as connection:
        snapshot = json.loads(connection.execute('SELECT payload FROM app_snapshots WHERE key=?', ('demo_store_v1',)).fetchone()[0])
    value = {'schema_version': snapshot['schema_version'], 'trip_count': len(snapshot['trips']),
             'message_count': sum(map(len, snapshot['messages'].values())),
             'active_tasks': sum(t['status'] in {'queued', 'running'} for t in snapshot.get('tasks', {}).values()),
             'active_display_jobs': sum(t['status'] in {'queued', 'running', 'waiting_publish'} for t in snapshot.get('display_jobs', {}).values()),
             'hashes': {key: hashlib.sha256(json.dumps(snapshot.get(key), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                        for key in ('users', 'trips', 'messages', 'plans', 'idea_cards')}}
    if args.phase == 'before':
        if value['active_tasks'] or value['active_display_jobs']:
            raise SystemExit('Active business/display jobs; postpone restart')
        if (output/'before.json').exists():
            raise SystemExit('Use a new deployment baseline directory')
        (output/'before.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in value.items() if k != 'hashes'}))
    else:
        before = json.loads((output/'before.json').read_text(encoding='utf-8'))
        checks = {key: before[key] == value[key] for key in ('schema_version', 'trip_count', 'message_count', 'hashes')}
        (output/'after.json').write_text(json.dumps({'value': value, 'preserved': checks}, indent=2), encoding='utf-8')
        print(json.dumps({'preserved': checks, 'active_tasks': value['active_tasks'], 'active_display_jobs': value['active_display_jobs']}))
        if not all(checks.values()):
            raise SystemExit('Business content changed; inspect before claiming preservation')


if __name__ == '__main__':
    main()
