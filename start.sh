#!/bin/bash

# Terminate all background processes if this script exits or gets terminated
trap 'kill $(jobs -p) 2>/dev/null' EXIT

# Start FastAPI web server on port 7860
echo "=== Starting FastAPI Web Server ==="
uvicorn main:app --host 0.0.0.0 --port 7860 &
WEB_PID=$!

# Start RQ worker on gitlab_reviews queue
echo "=== Starting RQ Background Worker ==="
rq worker --url "$UPSTASH_REDIS_URL" gitlab_reviews &
WORKER_PID=$!

echo "=== Processes started. Monitoring status... ==="

# Monitor both PIDs
while true; do
  ps -p $WEB_PID > /dev/null
  WEB_STATUS=$?
  
  ps -p $WORKER_PID > /dev/null
  WORKER_STATUS=$?

  if [ $WEB_STATUS -ne 0 ]; then
    echo "ERROR: FastAPI web server has stopped. Exiting container."
    exit 1
  fi

  if [ $WORKER_STATUS -ne 0 ]; then
    echo "ERROR: RQ background worker has stopped. Exiting container."
    exit 1
  fi

  sleep 5
done
