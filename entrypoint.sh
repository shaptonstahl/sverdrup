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
echo "  NEXTDNS_API_URL: ${NEXTDNS_API_URL:-https://api.nextdns.io}"
echo "  SVERDRUP_ACCOUNTS: ${SVERDRUP_ACCOUNTS}"
echo "  COLLECTOR_INTERVAL: ${COLLECTOR_INTERVAL}m"
echo "  PROCESSOR_INTERVAL: ${PROCESSOR_INTERVAL}m"
echo "  DASHBOARD_PORT: ${DASHBOARD_PORT}"
echo "  DB_PATH: ${DB_PATH}"

# Turn an interval in minutes into a cron schedule: under an hour runs every
# N minutes and N must divide 60; an hour or more runs on the hour and must be
# a whole number of hours that divides 24. Anything else cron cannot run evenly.
cron_schedule() {
    case "$2" in
        '' | *[!0-9]*)
            echo "Sverdrup: $1 must be a whole number of minutes, got '$2'" >&2
            exit 1
            ;;
    esac
    if [ "$2" -lt 1 ]; then
        echo "Sverdrup: $1 must be at least 1" >&2
        exit 1
    elif [ "$2" -lt 60 ] && [ $((60 % $2)) -ne 0 ]; then
        echo "Sverdrup: $1 under 60 must divide 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30), got $2" >&2
        exit 1
    elif [ "$2" -ge 60 ] && { [ $(($2 % 60)) -ne 0 ] || [ $((24 % ($2 / 60))) -ne 0 ]; }; then
        echo "Sverdrup: $1 of 60 or more must be a whole number of hours dividing 24 (60, 120, 180, 240, 360, 480, 720, 1440), got $2" >&2
        exit 1
    elif [ "$2" -lt 60 ]; then
        echo "*/$2 * * * *"
    elif [ "$2" -eq 1440 ]; then
        echo "0 0 * * *"
    else
        echo "0 */$(($2 / 60)) * * *"
    fi
}
COLLECT_SCHEDULE=$(cron_schedule COLLECTOR_INTERVAL "$COLLECTOR_INTERVAL")
PROCESS_SCHEDULE=$(cron_schedule PROCESSOR_INTERVAL "$PROCESSOR_INTERVAL")

# Create crontab for scheduled jobs
cat > /etc/crontabs/root << EOF
${COLLECT_SCHEDULE} cd /app && python3 collector/collect.py >> /var/log/sverdrup-collect.log 2>&1
${PROCESS_SCHEDULE} cd /app && python3 processor/process.py >> /var/log/sverdrup-process.log 2>&1
EOF

# Start crond in background
echo "Sverdrup: Starting scheduler (crond)"
crond -f -l 2 &
CROND_PID=$!

# Start Go server in foreground
echo "Sverdrup: Starting dashboard server on port ${DASHBOARD_PORT}"
exec sverdrup-server
