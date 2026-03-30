#!/bin/bash
set -a
source /home/orangepi/gh-review/.env
set +a
exec /home/orangepi/.local/bin/uv run --with fastapi --with httpx uvicorn webhook_server:app --host 127.0.0.1 --port 9876
