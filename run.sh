#!/usr/bin/env bash
# Start SoundMatch on http://127.0.0.1:8420
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8420
