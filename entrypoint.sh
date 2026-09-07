#!/bin/sh
set -eu

exec python3 /app/status_service.py "$@"
