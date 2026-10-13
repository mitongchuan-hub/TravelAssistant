"""Archive selected immutable run evidence, excluding nested source snapshots."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    source = Path(sys.argv[1]).resolve()
    target = ROOT/'docs/evaluations/seven-optimizations-20261013'
    if target.exists():
        raise SystemExit('Archive already exists; do not overwrite evidence')
    target.mkdir(parents=True)
    from dotenv import dotenv_values
    secrets = [value for key, value in {**dotenv_values(ROOT/'.env'), **os.environ}.items()
               if isinstance(value, str) and len(value) > 8 and any(word in key for word in ('KEY', 'TOKEN', 'PASSWORD'))]
    manifest = {}
    runs = ['display-historical', 'acceptance', 'display-candidate', 'display-object-candidate',
            'display-plain-candidate', 'display-yaml-candidate', 'quote-transport',
            'final-acceptance', 'followup-acceptance', 'edit-final']
    for directory in runs:
        run = source/directory
        if not run.exists():
            continue
        for file in run.rglob('*'):
            if not file.is_file() or any(part in {'source', 'source_snapshot', '__pycache__', '.pytest_cache'} for part in file.relative_to(run).parts):
                continue
            if file.suffix not in {'.json', '.txt', '.log', '.md', '.csv', '.html'}:
                continue
            relative = file.relative_to(source)
            text = file.read_text(encoding='utf-8', errors='replace')
            for secret in secrets:
                text = text.replace(secret, '[redacted]')
            destination = target/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding='utf-8')
            manifest[relative.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
    for file in source.glob('*.log'):
        text = file.read_text(encoding='utf-8', errors='replace')
        for secret in secrets:
            text = text.replace(secret, '[redacted]')
        (target/file.name).write_text(text, encoding='utf-8')
        manifest[file.name] = hashlib.sha256((target/file.name).read_bytes()).hexdigest()
    current = target/'final-source'
    paths = [ROOT/'.env.example', ROOT/'requirements.txt'] + list((ROOT/'app').rglob('*.py')) + list((ROOT/'app/prompts').rglob('*.txt')) + list((ROOT/'app/schemas').glob('*.json'))
    for file in paths:
        relative = file.relative_to(ROOT)
        destination = current/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(file.read_bytes())
        manifest[destination.relative_to(target).as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (target/'sha256.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(target), 'files': len(manifest)}))


if __name__ == '__main__':
    main()
