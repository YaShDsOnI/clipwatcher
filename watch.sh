#!/usr/bin/env bash
# clip-watcher: record a Twitch stream and auto-clip chat spikes.
#   ./watch.sh <twitch_channel> [--min N] [--ratio 1.7] [--model base] ...   (--help for all options)
set -euo pipefail
cd "$(dirname "$0")"

for py in python3 python py; do
  # skip missing interpreters and the Windows Store "python3" stub
  if command -v "$py" >/dev/null 2>&1 &&
     "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    exec "$py" -u -m clipwatcher watch "$@"
  fi
done
echo "watch.sh: Python 3.10+ not found on PATH" >&2
exit 1
