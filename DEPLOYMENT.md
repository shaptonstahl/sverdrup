# Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose
- NextDNS account with API access enabled
- For each NextDNS account: its API key, and the IDs of the profiles you want collected

### Environment Setup

All configuration is environment variables in one `.env` file (git-ignored; never commit it). Start from the example:

```bash
cp .env.example .env
```

```
# Short names for your NextDNS accounts (letters, digits, underscores)
SVERDRUP_ACCOUNTS=home,family

# Per account NAME, uppercased: the account's API key and the profiles to collect.
# Profiles the account owns but you leave out are not collected.
NEXTDNS_HOME_API_KEY=your_home_account_api_key
NEXTDNS_HOME_PROFILES=abc123,def456
NEXTDNS_FAMILY_API_KEY=your_family_account_api_key
NEXTDNS_FAMILY_PROFILES=fed321

# Optional (defaults shown)
NEXTDNS_API_URL=https://api.nextdns.io
COLLECTOR_INTERVAL=15
PROCESSOR_INTERVAL=60
SESSION_GAP_MINUTES=15
TZ=UTC
DASHBOARD_PORT=8080
```

`TZ` is an IANA zone such as `America/New_York`; it decides where one day's metrics end. The collector checks this configuration at startup and names every variable that is missing or malformed. The older `NEXTDNS_API_KEY` and `NEXTDNS_PROFILE_ID` variables are no longer read; move them into an account.

### Run with Docker Compose

```bash
git clone https://github.com/shaptonstahl/sverdrup.git
cd sverdrup
cp .env.example .env   # then edit
docker-compose up -d
```

Or without Compose:

```bash
docker build -t sverdrup:latest .
docker run -d --env-file .env -v sverdrup-data:/data -p 8080:8080 sverdrup:latest
```

Navigate to `http://localhost:8080` on any device on your home network.

## Manual Setup (without Docker)

### System Requirements
- Go 1.21+
- Python 3.10+
- SQLite3

### Installation

1. Clone the repository:
```bash
git clone https://github.com/shaptonstahl/sverdrup.git
cd sverdrup
```

2. Set environment variables (or `set -a; . ./.env; set +a` to load your `.env`):
```bash
export SVERDRUP_ACCOUNTS="home"
export NEXTDNS_HOME_API_KEY="your_key"
export NEXTDNS_HOME_PROFILES="your_profile_id"
export DB_PATH="$PWD/sverdrup.db"
```

3. Build and run:
```bash
# Build Go server
cd server
go build -o sverdrup-server main.go

# The collector and processor need only the Python standard library
cd ..

# Start server (background)
./server/sverdrup-server &

# Start scheduler (runs collector/processor on schedule)
# (See Scheduler Setup below)
```

### Scheduler Setup

Use `cron` or `systemd` timers to run the collector and processor scripts:

#### Cron (example)

```bash
# Edit crontab
crontab -e

# Add these lines (cron does not read .env; set the variables in the crontab
# or in a wrapper script):
*/15 * * * * cd /path/to/sverdrup && python3 collector/collect.py
0 * * * * cd /path/to/sverdrup && python3 processor/process.py
```

#### Systemd timers (example)

Create `/etc/systemd/system/sverdrup-collect.service` and `.timer` files to manage scheduling.

## Reverse Proxy Configuration

### Nginx Example

```nginx
upstream sverdrup_backend {
    server 127.0.0.1:8080;
}

server {
    listen 443 ssl http2;
    server_name streaming.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    auth_basic "Sverdrup";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://sverdrup_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Caddy Example

```caddyfile
streaming.example.com {
    basicauth / {
        username hashedpassword
    }
    
    reverse_proxy 127.0.0.1:8080
}
```

## Data Persistence

- SQLite database file is stored at the path specified by `DB_PATH`
- In Docker: use a named volume to persist data across container restarts
- Recommended backup strategy: daily snapshots of the database file

## Monitoring and Logs

### Docker Compose

```bash
# View logs
docker-compose logs -f sverdrup

# Collector and processor logs live inside the container
docker exec sverdrup tail -n 50 /var/log/sverdrup-collect.log
docker exec sverdrup tail -n 50 /var/log/sverdrup-process.log
```

### Manual Setup

- Server logs: stdout/stderr (redirect to a file if desired)
- Collector logs: check Python script output
- Database queries: SQLite provides query logging via `PRAGMA` statements

## Troubleshooting

### Dashboard not accessible
- Check that port 8080 is open and not blocked by a firewall
- Verify the server is running: `curl http://localhost:8080/`

### No data appearing
- Check NextDNS accounts, keys and profile IDs in `.env`; the collector log names any variable it rejects
- Verify collector script ran: check logs
- Ensure SQLite database file has write permissions

### High memory usage
- Go server should be <50MB at idle
- Python jobs should exit cleanly after running
- Check for stuck processes: `ps aux | grep sverdrup`
