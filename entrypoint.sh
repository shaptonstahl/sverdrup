#!/bin/sh

# Sverdrup Container Entrypoint
# Starts both crond (for scheduled jobs) and the Go dashboard server

set -e

# Set defaults
export COLLECTOR_INTERVAL=${COLLECTOR_INTERVAL:-15}
export PROCESSOR_INTERVAL=${PROCESSOR_INTERVAL:-60}
export DASHBOARD_PORT=${DASHBOARD_PORT:-8080}
export DB_PATH=${DB_PATH:-/data/sverdrup.db}

echo "Sverdrup: Starting container"
echo "  NEXTDNS_API_URL: ${NEXTDNS_API_URL}"
echo "  COLLECTOR_INTERVAL: ${COLLECTOR_INTERVAL}m"
echo "  PROCESSOR_INTERVAL: ${PROCESSOR_INTERVAL}m"
echo "  DASHBOARD_PORT: ${DASHBOARD_PORT}"
echo "  DB_PATH: ${DB_PATH}"

# Create crontab for scheduled jobs
cat > /etc/crontabs/root << EOF
*/${COLLECTOR_INTERVAL} * * * * cd /app && python collector/collect.py >> /var/log/sverdrup-collect.log 2>&1
0 */${PROCESSOR_INTERVAL} * * * cd /app && python processor/process.py >> /var/log/sverdrup-process.log 2>&1
EOF

# Start crond in background
echo "Sverdrup: Starting scheduler (crond)"
crond -f -l 2 &
CROND_PID=$!

# Start Go server in foreground
echo "Sverdrup: Starting dashboard server on port ${DASHBOARD_PORT}"
exec sverdrup-server
