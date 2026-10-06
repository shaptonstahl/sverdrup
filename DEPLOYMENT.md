# Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose
- NextDNS account with API access enabled
- NextDNS profile ID and API key

### Environment Setup

Create a `.env` file (not committed to git):

```
NEXTDNS_API_KEY=your_api_key_here
NEXTDNS_PROFILE_ID=your_profile_id_here
NEXTDNS_API_URL=https://api.nextdns.io
COLLECTOR_INTERVAL=15
PROCESSOR_INTERVAL=60
DASHBOARD_PORT=8080
DB_PATH=/data/sverdrup.db
```

### Run with Docker Compose

```bash
git clone https://github.com/shaptonstahl/sverdrup.git
cd sverdrup
docker-compose up -d
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

2. Set environment variables:
```bash
export NEXTDNS_API_KEY="your_key"
export NEXTDNS_PROFILE_ID="your_profile"
```

3. Build and run:
```bash
# Build Go server
cd server
go build -o sverdrup-server main.go

# Install Python dependencies
cd ../collector
pip install -r requirements.txt

# Start server (background)
cd ..
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

# Add these lines:
*/15 * * * * cd /path/to/sverdrup && python collector/collect.py
0 * * * * cd /path/to/sverdrup && python processor/process.py
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

# View server logs specifically
docker-compose logs -f server
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
- Check NextDNS API credentials in `.env`
- Verify collector script ran: check logs
- Ensure SQLite database file has write permissions

### High memory usage
- Go server should be <50MB at idle
- Python jobs should exit cleanly after running
- Check for stuck processes: `ps aux | grep sverdrup`
