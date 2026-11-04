#!/bin/sh
# Démarrage en production (Render, Railway, VPS…)
set -e
exec gunicorn main:app \
  --workers "${WEB_CONCURRENCY:-2}" \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT:-8000}" \
  --forwarded-allow-ips="*" \
  --timeout 60 \
  --access-logfile -
