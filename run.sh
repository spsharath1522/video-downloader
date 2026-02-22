#!/usr/bin/env bash
# Run Media Downloader locally. From project root:
#   ./run.sh
# Or: bash run.sh
set -e
cd "$(dirname "$0")"
if [ -d "venv" ]; then
  source venv/bin/activate
fi
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
