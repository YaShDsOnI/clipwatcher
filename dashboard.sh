#!/usr/bin/env bash
# clip-watcher dashboard: manage streamers and watch clips in your browser.
#   ./dashboard.sh             opens http://localhost:8765
#   ./dashboard.sh --port 9000 --no-browser
set -euo pipefail
cd "$(dirname "$0")"

for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1 &&
     "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    exec "$py" -u -m clipwatcher dashboard "$@"
  fi
done
echo "dashboard.sh: Python 3.10+ not found on PATH" >&2
exit 1
