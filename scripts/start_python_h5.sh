#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache
pid=""
if command -v ss >/dev/null 2>&1; then
  pid=$(ss -ltnp 2>/dev/null | sed -n 's/.*:8765 .*pid=\([0-9][0-9]*\).*/\1/p' | head -1 || true)
fi
if [ -n "$pid" ]; then
  kill "$pid" 2>/dev/null || true
  sleep 1
fi
setsid python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8765}" > .cache/python_h5.stdout.log 2> .cache/python_h5.stderr.log < /dev/null &
echo $! > .cache/python_h5.pid
printf 'TravelAssistant started: pid=%s port=%s\n' "$(cat .cache/python_h5.pid)" "${PORT:-8765}"
